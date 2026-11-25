"""`pros factcheck`: re-run only the second-model fact check on an existing draft bundle.

The owner's tool for measuring the fact check itself: the draft's stored brief and response are
checked again with the current mechanical checks (so C1's CODE_MEANING_INCOMPLETE lines are
found even for a draft written before C1), and one fact-check request is built exactly as a run
builds it, with those lines sent beside their sentence. Without `--allow-paid` nothing is sent:
the command prints what would be checked and the request's cost ceiling.

With `--allow-paid` it sends that one request through the run's budget (counted, capped by
`max_cost_usd`, recorded as `fact_check` spending; never a weekly draft slot), never retried,
and stores the request identity, the raw reply and the parsed verdicts in a new numbered file
under `drafts/<story id>-recheck/`. It changes nothing else: the draft, its bundle, the stored
fact check that the review page uses (`automation/fact-checks/`), the backlog and the review
ledger are not written. The STOP file and a usage-limit hold stop it before any request.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from shipyap.automation import factcheck
from shipyap.automation.config import AutomationError, Config
from shipyap.automation.engine import STOP_FILE, RunResult, _Budget, _utc, request_hash
from shipyap.automation.state import State, run_lock
from shipyap.drafting.models import Drafter, ModelRequest
from shipyap.drafting.policy import OFFLINE_BYTES_PER_TOKEN, counted_estimate
from shipyap.drafting.service import checked_input
from shipyap.editorial.grounding import check_draft, closing_id
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.sources import VerifiedSnapshot, load_verified_snapshot

RECHECK_SUFFIX = "-recheck"
RECORD_VERSION = "1"
_DRAFT_ID = re.compile(r"[0-9a-f]{8,64}")


@dataclass(frozen=True)
class Prepared:
    story_id: str
    draft_dir: Path
    request: ModelRequest
    segment_ids: tuple[str, ...]
    supplied: dict[str, tuple[str, ...]]  # segment id -> C1 lines sent beside it
    checks: dict[str, tuple[str, ...]]  # segment id -> "SEVERITY CODE" of today's checks
    ceiling_usd: Decimal  # offline estimate of the request's maximum cost


@dataclass(frozen=True)
class Rechecked:
    prepared: Prepared
    verdicts: tuple[factcheck.Verdict, ...]
    path: Path
    reserved_usd: Decimal
    charged_usd: Decimal
    error: str | None = None  # the reply was stored but could not be parsed


def find_draft(drafts: Path, draft_id: str) -> Path:
    """The one `<story id>-draft` bundle whose id starts with `draft_id` (8 to 64 hex)."""
    if not _DRAFT_ID.fullmatch(draft_id or ""):
        raise AutomationError("DRAFT_ID_INVALID: give 8 to 64 lowercase hex characters")
    found = sorted(
        p
        for p in (drafts.iterdir() if drafts.is_dir() else ())
        if p.is_dir() and p.name.endswith("-draft") and p.name.startswith(draft_id)
    )
    if not found:
        raise AutomationError(f"DRAFT_NOT_FOUND: no {draft_id}*-draft bundle in {drafts}")
    if len(found) > 1:
        raise AutomationError(f"DRAFT_AMBIGUOUS: {len(found)} bundles start with {draft_id}")
    return found[0]


def _snapshot(config: Config, brief: bytes) -> VerifiedSnapshot:
    try:
        manifest = json.loads(brief)["snapshot"]["manifest_sha256"]
    except (ValueError, KeyError, TypeError):
        raise AutomationError("BRIEF_UNREADABLE") from None
    for directory in sorted(config.snapshots.iterdir()) if config.snapshots.is_dir() else ():
        if (directory / "manifest.json").is_file():
            snapshot = load_verified_snapshot(directory)  # hashes verified on load
            if snapshot.manifest_sha256 == manifest:
                return snapshot
    raise AutomationError("SNAPSHOT_NOT_FOUND: the draft's pinned snapshot is not in snapshots/")


def prepare(config: Config, draft_id: str) -> Prepared:
    """The request a run would send for this draft today. Reads only; sends nothing."""
    draft_dir = find_draft(config.drafts, draft_id)
    story_id = draft_dir.name.removesuffix("-draft")
    brief_bytes = (draft_dir / "brief.json").read_bytes()
    brief = parse_brief(brief_bytes)
    checked = checked_input((draft_dir / "response.json").read_bytes(), brief)
    response = parse_response(checked.data)
    amap = check_draft(
        brief,
        response,
        hashlib.sha256(brief_bytes).hexdigest(),
        hashlib.sha256(checked.data).hexdigest(),
    )
    if not response.segments:
        raise AutomationError("DRAFT_EMPTY: the draft has no sentences to check")
    supplied = factcheck.meaning_lines(amap.model_dump(mode="json")["segments"])
    snapshot = _snapshot(config, brief_bytes)
    closing = closing_id(brief, response)  # the tool's link line is never fact-checked
    judged = [s for s in response.segments if s.id != closing]
    segments = [{"id": s.id, "text": s.text} for s in judged]
    request = factcheck.build_request(config.model, snapshot, segments, supplied=supplied)
    size = sum(
        len(part.encode("utf-8"))
        for part in (
            request.system_prompt,
            request.user_text,
            json.dumps(request.output_schema),
        )
    )
    ceiling = counted_estimate(
        request.model, math.ceil(size / OFFLINE_BYTES_PER_TOKEN), request.max_tokens
    ).max_cost_usd
    checks = {s.id: tuple(f"{f.severity} {f.code}" for f in s.findings) for s in amap.segments}
    return Prepared(
        story_id,
        draft_dir,
        request,
        tuple(s.id for s in judged),
        supplied,
        checks,
        ceiling,
    )


def _record_path(config: Config, story_id: str) -> Path:
    directory = config.drafts / f"{story_id}{RECHECK_SUFFIX}"
    directory.mkdir(exist_ok=True)
    for n in range(1, 1000):
        path = directory / f"fact-check-{n:02d}.json"
        if not path.exists():
            return path
    raise AutomationError("RECHECK_FULL")


def run(
    config: Config,
    draft_id: str,
    factory: Callable[[], Drafter],
    now: datetime | None = None,
) -> Rechecked:
    """One paid fact-check request for an existing draft. Raises AutomationError (STOPPED, a
    hold, the draft or snapshot missing), DraftingError (budget, provider) before anything is
    stored; once a reply exists it is always stored, even when it cannot be parsed."""
    now = now or datetime.now(UTC)
    if (config.workspace_root / STOP_FILE).exists():
        raise AutomationError("STOPPED: remove the STOP file in your workspace to run again")
    prepared = prepare(config, draft_id)
    config.state.mkdir(parents=True, exist_ok=True)
    with run_lock(config.state / "run-lock.sqlite"):
        state = State(config.state / "automation.sqlite")
        try:
            held = state.holding(_utc(now))
            if held is not None:
                raise AutomationError(
                    f"USAGE_LIMIT_WAIT: paid calls wait until {held['until']} (provider hold)"
                )
            result = RunResult()
            budget = _Budget(config, state, factory, result)
            budget.kind = "fact_check"
            request = prepared.request
            budget.count_input_tokens(request)
            reply = budget.generate(request)
        finally:
            state.close()
    error = None
    try:
        verdicts = factcheck.parse_reply(
            reply.texts, reply.stop_reason, list(prepared.segment_ids), prepared.supplied
        )
    except factcheck.FactCheckError as exc:
        verdicts, error = [], exc.code
    path = _record_path(config, prepared.story_id)
    record = {
        "record_version": RECORD_VERSION,
        "kind": "fact_check_rerun",
        "draft": prepared.draft_dir.name,
        "created_at": now.astimezone(UTC).isoformat(),
        "model": request.model,
        "prompt_version": factcheck.PROMPT_VERSION,
        "request_hash": request_hash(request),
        "supplied": {sid: list(labels) for sid, labels in prepared.supplied.items()},
        "checks": {sid: list(codes) for sid, codes in prepared.checks.items()},
        "reserved_usd": str(result.estimated_usd),
        "charged_usd": str(result.charged_usd),
        "error": error,
        "verdicts": [v.as_dict() for v in verdicts],
        "reply": asdict(reply),
    }
    with path.open("x", encoding="utf-8") as handle:  # never overwrites an earlier paid result
        json.dump(record, handle, indent=2, ensure_ascii=False)
    return Rechecked(
        prepared, tuple(verdicts), path, result.estimated_usd, result.charged_usd, error
    )


def preview_lines(prepared: Prepared) -> list[str]:
    lines = [f"Draft {prepared.story_id[:16]}: {len(prepared.segment_ids)} sentences"]
    for sid in prepared.segment_ids:
        extra = prepared.supplied.get(sid)
        block = ", ".join(prepared.checks.get(sid, ())) or "no finding"
        lines.append(f"  {sid}: {block}" + (f"; also sent: {', '.join(extra)}" if extra else ""))
    lines.append(
        f"Request ceiling: ${prepared.ceiling_usd:.4f} (offline estimate; the provider's count "
        "is checked against max_cost_usd before sending)"
    )
    return lines


def result_lines(done: Rechecked) -> list[str]:
    lines = []
    if done.error:
        lines.append(f"NEEDS_ATTENTION: {done.error}; the raw reply is stored")
    for v in done.verdicts:
        extra = f" [judged with {', '.join(v.supplied)}]" if v.supplied else ""
        lines.append(f"  {v.segment_id} {v.verdict}{extra}: {v.reason}")
    lines += [
        f"Saved: {done.path}",
        f"Spend: ${done.reserved_usd:.4f} reserved (a ceiling), "
        f"${done.charged_usd:.4f} at list price for the usage reported",
        "Nothing else changed: the review page still uses the draft's stored fact check.",
    ]
    return lines
