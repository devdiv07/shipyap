"""`pros eval`: replay every stored model reply through the current checks, offline and for free.

Two kinds of case:
- a story case is one stored story-generate reply with the snapshot it was asked about and the
  lines that were sent. It is replayed through the normaliser, the findings schema, the
  supplied-context check and the 06B finding check, exactly as `story-generate` judges it,
  without writing a file or touching a backlog. Duplicate angles are a backlog decision and are
  not judged here.
- a draft case is one stored draft response with its brief, replayed through `check_draft`,
  the supersession check against the later records of every committed snapshot of the same
  revision, and the approval gate (`review.gate`) with the case's stored fact check, if any. A
  control is a draft known to be right (owner-approved, or every fact-check verdict SUPPORTED or
  UNVERIFIABLE): each sentence the gate blocks in a control is a false alarm.
- every draft case is also replayed through auto-repair (`automation.repair`): free repairs
  always; model rounds only when the case names a repair fixture (hand-written stand-ins for the
  repair and re-check calls, replayed by a fake provider; never a real model). Without one, a
  draft that needs a model round reports SHELVED.
- a decision case is one owner decision (Approve, Edit, Reject) that `review-serve` saved under
  the workspace's `automation/eval-cases/`; it is read and counted, never changed.

Cases come from a committed case directory (`evals/`, used by CI) or straight from a workspace's
`automation/automation.sqlite`, opened read-only, including archived attempts. Nothing here
opens a network connection, calls a model, reads the environment or writes anything.

The gate audit is independent of the checks it audits: every number in an accepted finding's
angle and support must appear in one of its quotes, or in the cited lines of a reference without
a quote.
"""

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from shipyap.automation import repair as auto_repair
from shipyap.drafting.errors import DraftingError, DraftingErrorCode
from shipyap.drafting.models import ModelReply, ModelRequest, Usage
from shipyap.drafting.normalise import Change, ChangeKind
from shipyap.drafting.service import checked_input
from shipyap.drafting.story_prompt import FileCoverage, StoryContext
from shipyap.drafting.story_service import (
    REF_OUTSIDE_SUPPLIED_CONTEXT,
    reply_candidates,
    screen_candidates,
)
from shipyap.editorial.errors import EditorialError
from shipyap.editorial.grounding import (
    check_draft,
    claim_numbers,
    labels,
    number_tokens,
)
from shipyap.editorial.inputs import parse_brief, parse_response
from shipyap.editorial.models import Severity
from shipyap.editorial.sources import VerifiedSnapshot, load_verified_snapshot
from shipyap.editorial.supersession import Record, check_superseded
from shipyap.preferences.repositories import Mode
from shipyap.review.gate import judge
from shipyap.stories.errors import StoryError
from shipyap.stories.findings import StoryFinding, check_finding, snapshot_labels

CASE_VERSION = "1"
_COVERAGE = re.compile(r'^- ("(?:[^"\\]|\\.)*")(?: role=\w+)?$', re.MULTILINE)
_LABEL = re.compile(r"^(.*) (?:lines 1-(\d+)|no lines) of (\d+) \((?:complete|PARTIAL)\)$")
_MODE = re.compile(r"\(publishing mode: (\w+)\)")
_MANIFEST = re.compile(r"Snapshot manifest sha256: ([0-9a-f]{64})")


class CaseError(Exception):
    """A stored case cannot be replayed (missing snapshot, unreadable file, bad case)."""


@dataclass(frozen=True)
class StoryCase:
    name: str
    repo_id: str
    revision: str
    snapshot: VerifiedSnapshot
    directory: Path
    mode: Mode
    supplied: dict[str, int]
    reply: ModelReply


@dataclass(frozen=True)
class DraftCase:
    name: str
    brief: bytes
    response: bytes
    records: tuple[Record, ...] = ()  # later records' candidates: same-revision snapshot files
    fact_check: dict | None = None
    control: bool = False
    repair_fixture: tuple[dict, ...] | None = None  # stand-in replies, in call order
    snapshot: VerifiedSnapshot | None = None  # the brief's own snapshot, for re-check requests
    require_fact_check: bool = False
    known_gap: str = ""


@dataclass(frozen=True)
class DecisionCase:
    name: str
    decision: str
    segments: int
    edited: int
    approvable_before: bool


@dataclass(frozen=True)
class FindingResult:
    story_key: str
    accepted: bool
    reason: str | None  # why it was refused; never finding text
    changes: tuple[Change, ...]
    numbers_outside_quote: tuple[str, ...] = ()  # gate audit, accepted findings only


@dataclass(frozen=True)
class StoryReport:
    name: str
    proposed: int
    findings: tuple[FindingResult, ...]
    reply_error: str | None = None

    @property
    def accepted(self) -> int:
        return sum(f.accepted for f in self.findings)

    @property
    def outside_quote(self) -> int:
        return sum(bool(f.numbers_outside_quote) for f in self.findings)


@dataclass(frozen=True)
class SegmentResult:
    segment_id: str
    blocking: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    segment_id: str
    blocks: tuple[str, ...]  # "CODE detail [refs]"
    warnings: tuple[str, ...]
    outdated_by: tuple[str, ...] = ()  # record paths of an OUTDATED block


@dataclass(frozen=True)
class RepairReport:
    status: str
    free: tuple[str, ...]
    rounds: tuple[dict, ...]
    reasons: tuple[str, ...]
    approvable: bool  # the repaired draft's gate
    blocks: int
    texts: dict[str, str]  # the repaired draft's segments
    cited: tuple[str, ...]  # every "path:start-end" the repaired draft cites
    fixture: bool
    reserved_usd: str
    charged_usd: str
    # What a CLEAN repaired draft's fact check rests on, as the review page judges it: COMPLETE
    # (the record covers every sentence), INHERITED (the origin's, every change explained) or
    # REQUIRED (Not fact-checked). None unless CLEAN.
    fact_check: str | None = None


@dataclass(frozen=True)
class DraftReport:
    name: str
    status: str
    segments: tuple[SegmentResult, ...]
    document_findings: tuple[str, ...] = ()
    error: str | None = None
    approvable: bool = False
    gate: tuple[GateResult, ...] = ()
    control: bool = False
    repair: RepairReport | None = None
    repaired: tuple[str, ...] = ()  # what checked_input changed before the parser
    known_gap: str = ""

    @property
    def false_alarms(self) -> int:
        """Blocked sentences of a control (a post-level block counts once)."""
        if not self.control:
            return 0
        blocked = sum(bool(g.blocks) for g in self.gate)
        return blocked or int(not self.approvable)


@dataclass
class EvalReport:
    stories: list[StoryReport] = field(default_factory=list)
    drafts: list[DraftReport] = field(default_factory=list)
    decisions: list[DecisionCase] = field(default_factory=list)


# --- loading -----------------------------------------------------------------------------------


def load_cases(directory: Path) -> tuple[list[StoryCase], list[DraftCase]]:
    """The committed cases: `stories/*.json` (snapshots under `snapshots/`) and `drafts.json`."""
    stories = []
    for path in sorted((directory / "stories").glob("*.json")):
        value = _json(path)
        if value.get("case_version") != CASE_VERSION:
            raise CaseError(f"{path.name}: unknown case_version")
        snapshot_dir = directory / "snapshots" / value["snapshot"]
        snapshot = _snapshot(snapshot_dir)
        stories.append(
            StoryCase(
                name=path.stem,
                repo_id=value["repo"],
                revision=value["revision"],
                snapshot=snapshot,
                directory=snapshot_dir,
                mode=Mode(value["mode"]),
                supplied={str(k): int(v) for k, v in value["supplied"].items()},
                reply=_reply(value["reply"]),
            )
        )
    drafts = []
    listing = directory / "drafts.json"
    if listing.is_file():
        snapshots = [
            _snapshot(d) for d in sorted((directory / "snapshots").glob("*")) if d.is_dir()
        ]
        for item in _json(listing)["cases"]:
            brief = _read(directory / item["brief"])
            check = item.get("fact_check")
            fixture = item.get("repair_fixture")
            replies = tuple(_json(directory / fixture)["replies"]) if fixture else None
            drafts.append(
                DraftCase(
                    name=item["name"],
                    brief=brief,
                    response=_read(directory / item["response"]),
                    records=_same_revision(brief, snapshots),
                    fact_check=_json(directory / check) if check else None,
                    control=item.get("control") is True,
                    repair_fixture=replies,
                    snapshot=_own_snapshot(brief, snapshots),
                    require_fact_check=item.get("require_fact_check") is True,
                    known_gap=str(item.get("known_gap", "")),
                )
            )
    return stories, drafts


def load_decisions(workspace: Path) -> list[DecisionCase]:
    """The owner's saved decisions under `automation/eval-cases/` (none: an empty list)."""
    cases = []
    for path in sorted((workspace / "automation" / "eval-cases").glob("*.json")):
        value = _json(path)
        segments = value.get("segments")
        if value.get("case_version") != "1" or not isinstance(segments, list):
            raise CaseError(f"{path.name}: not a decision case")
        cases.append(
            DecisionCase(
                name=path.stem,
                decision=str(value.get("decision")),
                segments=len(segments),
                edited=sum(bool(s.get("edited")) for s in segments if isinstance(s, dict)),
                approvable_before=value.get("approvable_before") is True,
            )
        )
    return cases


def _own_snapshot(brief: bytes, snapshots: list[VerifiedSnapshot]) -> VerifiedSnapshot | None:
    try:
        manifest = json.loads(brief)["snapshot"]["manifest_sha256"]
    except (ValueError, KeyError, TypeError):
        return None
    return next((s for s in snapshots if s.manifest_sha256 == manifest), None)


def _same_revision(brief: bytes, snapshots: list[VerifiedSnapshot]) -> tuple[Record, ...]:
    try:
        identity = json.loads(brief)["snapshot"]
    except (ValueError, KeyError, TypeError):
        return ()
    return tuple(
        Record(source.path, tuple(source.lines))
        for snapshot in snapshots
        if snapshot.source_url == identity.get("source_url")
        and snapshot.revision_sha == identity.get("revision_sha")
        for source in snapshot.sources
    )


def load_workspace(workspace: Path) -> list[StoryCase]:
    """Every stored generation reply in a workspace, archived attempts included. The database is
    opened read-only; snapshots are matched by the manifest digest named in the stored request."""
    database = workspace / "automation" / "automation.sqlite"
    if not database.is_file():
        raise CaseError("no automation/automation.sqlite in the workspace")
    snapshots: dict[str, tuple[Path, VerifiedSnapshot]] = {}
    root = workspace / "snapshots"
    for directory in sorted(root.iterdir()) if root.is_dir() else ():
        if (directory / "manifest.json").is_file():
            snapshot = _snapshot(directory)
            snapshots.setdefault(snapshot.manifest_sha256, (directory, snapshot))
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT repo, revision, request_json, reply_json FROM generation_attempts "
            "WHERE reply_json IS NOT NULL ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    cases = []
    for repo, revision, request_json, reply_json in rows:
        text = json.loads(request_json)["user_text"]
        manifest, mode, supplied = request_identity(text)
        if manifest not in snapshots:
            raise CaseError(f"{repo}@{revision}: its snapshot is not in the workspace")
        directory, snapshot = snapshots[manifest]
        cases.append(
            StoryCase(
                # Prefixed: a workspace reply is never held to a committed case's floor.
                name="workspace:" + case_name(repo, revision),
                repo_id=repo,
                revision=revision,
                snapshot=snapshot,
                directory=directory,
                mode=mode,
                supplied=supplied,
                reply=_reply(json.loads(reply_json)),
            )
        )
    return cases


def request_identity(user_text: str) -> tuple[str, Mode, dict[str, int]]:
    """The snapshot manifest digest, publishing mode and lines sent per file, read from the
    header that `story_prompt.build_context` writes into every request."""
    manifest = _MANIFEST.search(user_text)
    mode = _MODE.search(user_text)
    header = user_text.split("<<<SOURCE", 1)[0]
    supplied = {}
    for quoted in _COVERAGE.findall(header):
        label = _LABEL.match(json.loads(quoted))
        if label is None:
            raise CaseError("unreadable coverage line in a stored request")
        supplied[label.group(1)] = int(label.group(2) or 0)
    if manifest is None or mode is None or not supplied:
        raise CaseError("a stored request has no snapshot header")
    return manifest.group(1), Mode(mode.group(1)), supplied


def case_name(repo: str, revision: str) -> str:
    name = repo.split("/")[-1]
    sha, _, archived = revision.partition("@")
    return f"{name}-{sha[:7]}" + (f"-{archived}" if archived else "")


# --- replay ------------------------------------------------------------------------------------


def replay_story(case: StoryCase) -> StoryReport:
    coverage = tuple(
        FileCoverage(
            source.path,
            len(source.lines),
            case.supplied.get(source.path, 0),
        )
        for source in case.snapshot.sources
    )
    context = StoryContext(
        directory=case.directory,
        repo_id=case.repo_id,
        mode=case.mode,
        snapshot=case.snapshot,
        coverage=coverage,
        existing=(),
        existing_omitted=0,
        user_text="",
    )
    try:
        candidates = reply_candidates(case.reply)
    except (DraftingError, StoryError) as exc:
        return StoryReport(case.name, 0, (), reply_error=str(exc.code))
    results = []
    for index, item in enumerate(screen_candidates(candidates, context), start=1):
        key = _key(item.normalised.candidate, index)
        changes = item.normalised.changes
        if item.finding is None:
            results.append(FindingResult(key, False, item.reason, changes))
            continue
        outside = next(
            (
                ref
                for ref in item.finding.source_refs
                if context.supplied_through(ref.path) < ref.line_end and ref.path in case.supplied
            ),
            None,
        )
        if outside is not None:
            label = f"{outside.path}:{outside.line_start}-{outside.line_end}"
            results.append(
                FindingResult(key, False, f"{REF_OUTSIDE_SUPPLIED_CONTEXT} {label}", changes)
            )
            continue
        check = check_finding(item.finding, case.snapshot)
        if check.refusal is not None:
            detail = f" {check.detail}" if check.detail else ""
            results.append(FindingResult(key, False, f"{check.refusal}{detail}", changes))
            continue
        missing = numbers_outside_quote(item.finding, case.snapshot)
        results.append(FindingResult(key, True, None, changes, missing))
    return StoryReport(case.name, len(candidates), tuple(results))


def numbers_outside_quote(finding: StoryFinding, snapshot: VerifiedSnapshot) -> tuple[str, ...]:
    """The gate audit: numbers of the angle and support that no quote holds (for a reference
    without a quote, its cited lines count as the quote)."""
    files = {source.path: source.lines for source in snapshot.sources}
    held: set[str] = set()
    for ref in finding.source_refs:
        if ref.quote is not None:
            held.update(number_tokens(ref.quote))
        else:
            held.update(
                number_tokens("\n".join(files[ref.path][ref.line_start - 1 : ref.line_end]))
            )
    text = f"{finding.angle}\n{finding.support}"
    known = snapshot_labels(snapshot)
    claimed = [*claim_numbers(text), *(t for w, t in labels(text) if (w, t) not in known)]
    return tuple(dict.fromkeys(token for token in claimed if token not in held))


def replay_draft(case: DraftCase) -> DraftReport:
    try:
        brief = parse_brief(case.brief)
        checked = checked_input(case.response, brief)  # exactly what a run builds the draft from
        response = parse_response(checked.data)
    except EditorialError as exc:
        return DraftReport(case.name, "UNREADABLE", (), error=str(exc.code))
    amap = check_draft(brief, response, _sha256(case.brief), _sha256(checked.data))
    segments = tuple(
        SegmentResult(
            segment.id,
            tuple(_code(f) for f in segment.findings if f.severity == Severity.BLOCKING),
            tuple(_code(f) for f in segment.findings if f.severity == Severity.WARNING),
        )
        for segment in amap.segments
    )
    document = tuple(f"{f.severity} {_code(f)}" for f in amap.document_findings)
    superseded = check_superseded(brief, response, case.records)
    gate = judge(amap, response, superseded, case.fact_check)
    if case.require_fact_check:
        from shipyap.review.required import completed_fact_check

        if not completed_fact_check(case.fact_check, response):
            from dataclasses import replace

            from shipyap.review.gate import Note

            gate = replace(
                gate,
                document_blocks=(
                    *gate.document_blocks,
                    Note("FACT_CHECK_REQUIRED", "Not fact-checked - cannot approve"),
                ),
            )
    outdated = {s.segment_id: s.records for s in superseded}
    results = tuple(
        GateResult(
            s.segment_id,
            tuple(_note(note) for note in s.blocks),
            tuple(_note(note) for note in s.warnings),
            tuple(label.rsplit(":", 1)[0] for label in outdated.get(s.segment_id, ())),
        )
        for s in gate.segments
    )
    return DraftReport(
        case.name,
        str(amap.draft_status),
        segments,
        document,
        approvable=gate.approvable,
        gate=results,
        control=case.control,
        repair=replay_repair(case, brief, response),
        repaired=checked.changes,
        known_gap=case.known_gap,
    )


class FixtureDrafter:
    """A fake provider for `pros eval`: token counts estimated offline (4 bytes a token), replies
    taken from a case's repair fixture in call order. Never a network call."""

    def __init__(self, replies: tuple[dict, ...]) -> None:
        self.replies = list(replies)
        self.requests: list[ModelRequest] = []

    def count_input_tokens(self, request: ModelRequest) -> int:
        size = len(request.system_prompt) + len(request.user_text)
        return (size + len(json.dumps(request.output_schema))) // 4

    def generate(self, request: ModelRequest) -> ModelReply:
        if not self.replies:
            raise DraftingError(DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
        self.requests.append(request)
        reply = self.replies.pop(0)
        texts = tuple(reply["texts"])
        return ModelReply(
            model=request.model,
            request_id=None,
            stop_reason=reply["stop_reason"],
            texts=texts,
            usage=Usage(self.count_input_tokens(request), sum(len(t) for t in texts) // 4),
        )


EVAL_MODEL = "claude-sonnet-5-5"


def replay_repair(case: DraftCase, brief, response) -> RepairReport | None:
    if not response.segments:
        return None
    fixture = case.repair_fixture is not None
    if fixture and case.snapshot is None:
        raise CaseError(f"{case.name}: a repair fixture needs the brief's snapshot")
    drafter = FixtureDrafter(case.repair_fixture) if fixture else None
    outcome = auto_repair.repair(
        brief, response, case.records, case.fact_check, case.snapshot, drafter, EVAL_MODEL
    )
    after = auto_repair.assess(brief, outcome.response, case.records, outcome.fact_check)
    return RepairReport(
        status=outcome.status,
        free=tuple(outcome.free),
        rounds=tuple(r.as_dict() for r in outcome.rounds),
        reasons=tuple(outcome.reasons),
        approvable=after.gate.approvable,
        blocks=sum(bool(s.blocks) for s in after.gate.segments) + len(after.gate.document_blocks),
        texts={s.id: s.text for s in outcome.response.segments},
        cited=tuple(
            f"{r.path}:{r.line_start}-{r.line_end}"
            for s in outcome.response.segments
            for r in s.source_refs
        ),
        fixture=fixture,
        reserved_usd=str(outcome.reserved_usd),
        charged_usd=str(outcome.charged_usd),
        fact_check=_repaired_fact_check(case, brief, response, outcome),
    )


def _repaired_fact_check(case: DraftCase, brief, response, outcome) -> str | None:
    if outcome.status != auto_repair.CLEAN:
        return None
    from shipyap.review import required

    if required.completed_fact_check(outcome.fact_check, outcome.response):
        return required.COMPLETE
    amap = check_draft(brief, response, _sha256(case.brief), _sha256(auto_repair.raw(response)))
    inherited = required.inherited_fact_check(
        brief,
        response,
        amap,
        case.fact_check,
        outcome.response,
        outcome.record("origin", "repaired"),
        "origin",
        "repaired",
    )
    return required.INHERITED if inherited is not None else required.REQUIRED


def evaluate(
    stories: list[StoryCase],
    drafts: list[DraftCase],
    decisions: list[DecisionCase] | None = None,
) -> EvalReport:
    return EvalReport(
        stories=[replay_story(case) for case in stories],
        drafts=[replay_draft(case) for case in drafts],
        decisions=list(decisions or ()),
    )


def gate_failures(report: EvalReport, expected: dict) -> list[str]:
    """What breaks the committed expectations: an accepted finding with a number outside its
    quote anywhere, fewer accepted findings than a case's floor, a draft status change, or an
    expected case that is not in the report at all (a missing case must never pass silently)."""
    failures = []
    stories = {story.name for story in report.stories}
    for name in expected.get("stories", {}):
        if name not in stories:
            failures.append(f"{name}: expected story case missing from the report")
    drafts = {draft.name for draft in report.drafts}
    for name, want in expected.get("drafts", {}).items():
        if name not in drafts:
            kind = "repair case" if want.get("repair") else "draft case"
            failures.append(f"{name}: expected {kind} missing from the report")
    for story in report.stories:
        if story.outside_quote:
            failures.append(f"{story.name}: {story.outside_quote} accepted with a number outside")
        floor = expected.get("stories", {}).get(story.name, {}).get("min_accepted")
        if floor is not None and story.accepted < floor:
            failures.append(f"{story.name}: accepted {story.accepted} < {floor}")
    for draft in report.drafts:
        want = expected.get("drafts", {}).get(draft.name, {})
        status = want.get("status")
        if status is not None and draft.status != status:
            failures.append(f"{draft.name}: status {draft.status}, expected {status}")
        approvable = want.get("approvable")
        if approvable is not None and draft.approvable != approvable:
            failures.append(f"{draft.name}: approvable {draft.approvable}, expected {approvable}")
        gates = {g.segment_id: g for g in draft.gate}
        for segment_id, record in (want.get("outdated") or {}).items():
            gate = gates.get(segment_id)
            if gate is None or record not in gate.outdated_by:
                failures.append(f"{draft.name}: {segment_id} not blocked as OUTDATED by {record}")
        for segment_id in want.get("unblocked") or ():
            gate = gates.get(segment_id)
            if gate is None or gate.blocks:
                failures.append(f"{draft.name}: {segment_id} is blocked")
        for segment_id, code in (want.get("blocked") or {}).items():
            gate = gates.get(segment_id)
            if gate is None or not any(note.split(" ", 1)[0] == code for note in gate.blocks):
                failures.append(f"{draft.name}: {segment_id} not blocked by {code}")
        failures += _repair_failures(draft, want.get("repair"))
    floor = expected.get("repair_totals", {}).get("min_clean")
    clean = _repairs(report, auto_repair.CLEAN)
    if floor is not None and clean < floor:
        failures.append(f"repaired clean {clean} < {floor}")
    allowed = expected.get("max_false_alarms")
    alarms = sum(draft.false_alarms for draft in report.drafts)
    if allowed is not None and alarms > allowed:
        failures.append(f"false alarms {alarms} > {allowed}")
    return failures


def _repair_failures(draft: DraftReport, want: dict | None) -> list[str]:
    if not want:
        return []
    got = draft.repair
    if got is None:
        return [f"{draft.name}: no repair replay"]
    failures = []
    if got.status != want.get("status", got.status):
        failures.append(f"{draft.name}: repair {got.status}, expected {want['status']}")
    if got.approvable != want.get("approvable", got.approvable):
        failures.append(f"{draft.name}: repaired approvable {got.approvable}")
    rounds = want.get("max_model_rounds")
    if rounds is not None and len(got.rounds) > rounds:
        failures.append(f"{draft.name}: {len(got.rounds)} model rounds > {rounds}")
    for ref in want.get("cites") or ():
        if ref not in got.cited:
            failures.append(f"{draft.name}: repaired draft does not cite {ref}")
    for segment_id, text in (want.get("texts") or {}).items():
        if got.texts.get(segment_id) != text:
            failures.append(f"{draft.name}: repaired {segment_id} is not the expected text")
    for segment_id in want.get("dropped") or ():
        if segment_id in got.texts:
            failures.append(f"{draft.name}: repair kept {segment_id}, expected it dropped")
    if "fact_check" in want and got.fact_check != want["fact_check"]:
        failures.append(
            f"{draft.name}: repaired fact check {got.fact_check}, expected {want['fact_check']}"
        )
    return failures


# --- printing ----------------------------------------------------------------------------------


def render(report: EvalReport) -> list[str]:
    lines = []
    for story in report.stories:
        if story.reply_error:
            lines.append(f"STORY {story.name}: reply refused {story.reply_error}")
            continue
        changed = sum(bool(f.changes) for f in story.findings)
        lines.append(
            f"STORY {story.name}: proposed={story.proposed} accepted={story.accepted} "
            f"refused={story.proposed - story.accepted} normalised={changed} "
            f"numbers_outside_quote={story.outside_quote}"
        )
        for result in story.findings:
            verdict = "ACCEPTED" if result.accepted else f"REFUSED {result.reason}"
            lines.append(f"  {verdict} {result.story_key}")
            lines += [f"      {_change(change)}" for change in result.changes]
            if result.numbers_outside_quote:
                lines.append(f"      AUDIT number outside quote: {result.numbers_outside_quote}")
    for draft in report.drafts:
        if draft.error:
            lines.append(f"DRAFT {draft.name}: unreadable {draft.error}")
            continue
        blocked = [s for s in draft.segments if s.blocking]
        lines.append(
            f"DRAFT {draft.name}: status={draft.status} segments={len(draft.segments)} "
            f"accepted={len(draft.segments) - len(blocked)} refused={len(blocked)}"
        )
        lines += [f"  PARSE REPAIR (deterministic) {change}" for change in draft.repaired]
        for segment in draft.segments:
            if segment.blocking:
                lines.append(f"  REFUSED {segment.segment_id} {', '.join(segment.blocking)}")
            if segment.warnings:
                lines.append(f"  WARNING {segment.segment_id} {', '.join(segment.warnings)}")
        lines += [f"  DOCUMENT {finding}" for finding in draft.document_findings]
        blocked = [g for g in draft.gate if g.blocks]
        lines.append(
            f"  GATE {'OPEN' if draft.approvable else 'BLOCKED'}"
            f"{' (control)' if draft.control else ''}: blocked sentences={len(blocked)}"
            + (f" false_alarms={draft.false_alarms}" if draft.control else "")
        )
        if draft.known_gap:
            lines.append(
                f"  known gap (Step 10): assistant code review, owner unconfirmed: "
                f"not approvable as written; automated gate {'OPEN' if draft.approvable else 'BLOCKED'}; "
                f"{draft.known_gap}"
            )
        for gate in draft.gate:
            lines += [f"    BLOCKED {gate.segment_id} {_shown(note)}" for note in gate.blocks]
            lines += [
                f"    WARNING {gate.segment_id} {_shown(note)}"
                for note in gate.warnings
                if note.startswith(("FACT_CHECK_", "NOT_RECHECKED", "FIRST_PERSON"))
            ]
        if draft.repair is not None and draft.repair.status != auto_repair.NOT_NEEDED:
            fix = draft.repair
            source = "fixture replies, not a model" if fix.fixture else "no model"
            lines.append(
                f"  REPAIR {fix.status}: model rounds={len(fix.rounds)} ({source}); after: "
                f"blocks={fix.blocks} approvable={fix.approvable}; reserved ${fix.reserved_usd}, "
                f"list price ${fix.charged_usd} (offline token estimate)"
            )
            if fix.fact_check is not None:
                lines.append(f"    FACT CHECK of the repaired draft: {fix.fact_check}")
            lines += [f"    FREE (deterministic) {_shown(line)}" for line in fix.free]
            for r in fix.rounds:
                proof = "fixture-only" if fix.fixture else "model"
                for sid, text in r["rewrites"].items():
                    lines.append(f"    MODEL round {r['round']} ({proof}) {sid}: {_shown(text)}")
                if r["failed"]:
                    lines.append(f"    MODEL round {r['round']} failed: {_shown(r['failed'])}")
            lines += [f"    SHELVED {_shown(reason)}" for reason in fix.reasons]
    for decision in report.decisions:
        lines.append(
            f"DECISION {decision.name}: {decision.decision} segments={decision.segments} "
            f"edited={decision.edited} approvable_before={decision.approvable_before}"
        )
    stories = [s for s in report.stories if not s.reply_error]
    lines.append(
        f"TOTAL stories={len(report.stories)} findings proposed="
        f"{sum(s.proposed for s in stories)} accepted={sum(s.accepted for s in stories)} "
        f"numbers_outside_quote={sum(s.outside_quote for s in stories)}; drafts="
        f"{len(report.drafts)} ready="
        f"{sum(d.status == 'READY_FOR_REVIEW' for d in report.drafts)} approvable="
        f"{sum(d.approvable for d in report.drafts)}; controls="
        f"{sum(d.control for d in report.drafts)} false_alarms="
        f"{sum(d.false_alarms for d in report.drafts)}; decisions={len(report.decisions)}; "
        f"repair clean={_repairs(report, auto_repair.CLEAN)} "
        f"shelved={_repairs(report, auto_repair.SHELVED)} "
        f"not_needed={_repairs(report, auto_repair.NOT_NEEDED)}"
    )
    return lines


def _repairs(report: EvalReport, status: str) -> int:
    return sum(d.repair is not None and d.repair.status == status for d in report.drafts)


def _change(change: Change) -> str:
    if change.kind == ChangeKind.SENTENCE_DROPPED:
        return (
            f"- {change.before}: {_shown(change.sentence or '')!r} "
            f"(not in a quote: {', '.join(change.numbers)})"
        )
    arrow = "x" if change.kind == ChangeKind.REF_DROPPED else "->"
    return f"{change.kind:<9} {change.before} {arrow} {change.after}"


def _shown(text: str, limit: int = 240) -> str:
    """Model text for a terminal: control and format characters escaped, length capped."""
    safe = "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in ("Cc", "Cf", "Cs") else c for c in text
    )
    return safe if len(safe) <= limit else safe[: limit - 3] + "..."


def _key(candidate: object, index: int) -> str:
    key = candidate.get("story_key") if isinstance(candidate, dict) else None
    if isinstance(key, str) and re.fullmatch(r"[a-z0-9-]{1,64}", key):
        return key
    return f"<finding {index}>"


def _note(note) -> str:
    """A gate note for a terminal: its records first, so a long reason cannot hide them."""
    where = f" [{', '.join(note.refs)}]" if note.refs else ""
    return f"{note.code}{where} {note.detail}".strip()


def _code(finding) -> str:
    return f"{finding.code}" + (f" ({finding.detail})" if finding.detail else "")


def _reply(value: dict) -> ModelReply:
    return ModelReply(
        model=value["model"],
        request_id=value.get("request_id"),
        stop_reason=value["stop_reason"],
        texts=tuple(value["texts"]),
        usage=Usage(**value["usage"]),
    )


def _snapshot(directory: Path) -> VerifiedSnapshot:
    try:
        return load_verified_snapshot(directory)
    except EditorialError as exc:
        raise CaseError(f"snapshot {directory.name}: {exc.code}") from None


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CaseError(f"{path.name}: unreadable") from None
    if not isinstance(value, dict):
        raise CaseError(f"{path.name}: expected an object")
    return value


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise CaseError(f"{path.name}: unreadable") from None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
