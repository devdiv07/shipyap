"""One manual discovery-to-review run, using the existing application services.

Repository evidence progress never gates drafting of AVAILABLE backlog candidates. A recorded
generation response is recovered through story-generate's parser and discovery registration;
neither a malformed response nor an uncertain paid call is treated as successful evidence.
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from shipyap.automation import factcheck, repair
from shipyap.automation.config import AutomationError, Config
from shipyap.automation.selection import select_sources
from shipyap.automation.state import State, run_lock
from shipyap.drafting.errors import (
    DraftingError,
    DraftingErrorCode,
    GenerationRejected,
    Phase,
)
from shipyap.drafting.models import Drafter, ModelReply, ModelRequest, Usage
from shipyap.drafting.policy import charge_estimate, counted_estimate
from shipyap.drafting.prompt import absolute_phrases, ai_phrases
from shipyap.drafting.service import checked_input
from shipyap.drafting.story_draft import (
    OutputState,
    StoryDraftError,
    StoryDraftFailed,
    plan_story_draft,
    recover_story_draft,
    run_story_draft,
)
from shipyap.drafting.story_service import (
    StoryGenerationError,
    StoryGenerationFailed,
    plan_story_generation,
    run_story_generation,
)
from shipyap.editorial.errors import EditorialError
from shipyap.editorial.inputs import parse_response
from shipyap.editorial.models import MAX_SEGMENT_CHARS, MAX_SEGMENTS, DraftStatus
from shipyap.editorial.service import build_from_response, prepare_build
from shipyap.editorial.sources import load_verified_snapshot
from shipyap.editorial.supersession import Record
from shipyap.evidence.report import canonical_json
from shipyap.ingestion.errors import IngestionError
from shipyap.ingestion.github import Transport
from shipyap.ingestion.request import IngestionRequest, parse_allow_repo
from shipyap.ingestion.service import run_ingestion
from shipyap.preferences.errors import PreferencesError
from shipyap.preferences.repositories import Mode, RepositoryPreferences
from shipyap.stories.backlog import RecordOutcome, StoryBacklog, StoryState
from shipyap.stories.discovery import list_backlog, run_discovery
from shipyap.stories.errors import StoryError

_ERRORS = (
    AutomationError,
    DraftingError,
    EditorialError,
    IngestionError,
    PreferencesError,
    StoryError,
    StoryDraftError,
    StoryDraftFailed,
    StoryGenerationError,
    StoryGenerationFailed,
    GenerationRejected,
    sqlite3.Error,
    OSError,
)


def _code(error: Exception) -> str:
    if isinstance(error, AutomationError):
        return str(error)
    if hasattr(error, "cause"):
        return _code(error.cause)
    if isinstance(error, DraftingError):
        return f"{error.code}{error.detail()}"
    return str(error.code) if hasattr(error, "code") else "LOCAL_IO_FAILED"


def request_hash(request: ModelRequest) -> str:
    return hashlib.sha256(canonical_json(asdict(request)).encode()).hexdigest()


@dataclass
class RunResult:
    repositories_inspected: int = 0
    evidence_examined: int = 0
    new_candidates: int = 0
    existing_available: int = 0
    drafts_created: int = 0
    draft_story: str = ""  # story id of the draft created this run, if any
    estimated_usd: Decimal = Decimal(0)  # reserved ceilings, summed
    charged_usd: Decimal = Decimal(0)  # list price of the usage the provider reported
    statuses: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    draft_paths: list[Path] = field(default_factory=list)
    snapshots: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # style flags for the owner; never failures
    repairs: list[str] = field(default_factory=list)  # one line per repaired draft, with its cost
    shelved: list[str] = field(default_factory=list)  # drafts shelved this run, with reasons


STOP_FILE = "STOP"  # create this file in the workspace root to stop every run
STYLE_MAX_CHARS = 1300  # p2 asks for 800 to 1,200; the checker's hard limit stays 3,000


def _paid_drafts_this_week(state, now: datetime) -> int:
    """Paid draft requests recorded since Monday 00:00 UTC of the current ISO week."""
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = state.db.execute("SELECT attempted_at FROM spending WHERE kind = 'draft'").fetchall()
    return sum(1 for (at,) in rows if datetime.fromisoformat(at) >= monday)


def _style_notes(plan, config) -> list[str]:
    """What the owner should look at in review. Nothing here edits or blocks the draft."""
    notes = []
    if plan.story.repo_id not in config.authors:
        notes.append(
            f"draft: no [author] entry for {plan.story.repo_id}, so it is written in the third "
            "person; add one to pros.toml for drafts in your voice"
        )
    text = (plan.paths.draft / "draft.txt").read_text(encoding="utf-8").removesuffix("\n")
    if len(text) > STYLE_MAX_CHARS:
        notes.append(
            f"draft: {len(text)} characters, over the 1,300 target; shorten it or reject it in review"
        )
    absolute = absolute_phrases(text)
    if absolute:
        notes.append(
            f"draft: absolute claim(s): {', '.join(absolute)}; keep only if the cited lines say "
            "exactly that"
        )
    phrases = ai_phrases(text)
    if phrases:
        notes.append(f"draft: machine-sounding phrase(s): {', '.join(phrases)}; edit in review")
    return notes


def _fact_check(config, budget, result, plan) -> None:
    """The second-model check of a new draft against every file of its snapshot. Its failure
    never hides the draft: the owner is told the check did not run."""
    budget.kind = "fact_check"
    try:
        verdicts = factcheck.run_fact_check(budget, config.model, plan.snapshot, plan.paths.draft)
    except factcheck.FactCheckError as exc:
        result.failures.append(f"fact check: {exc.code}; read the draft against the repo yourself")
        return
    except _ERRORS as exc:
        result.failures.append(
            f"fact check: {_code(exc)}; read the draft against the repo yourself"
        )
        return
    factcheck.save(config.state, plan.story.story_id, plan.paths.draft, verdicts)
    flagged = [v for v in verdicts if v.verdict in factcheck.FLAGGED]
    unsure = [v for v in verdicts if v.verdict == "UNVERIFIABLE"]
    for v in flagged:
        result.failures.append(f"fact check: {v.segment_id} {v.verdict}: {v.reason}")
    if unsure:
        result.notes.append(
            "fact check: not verifiable from the files: "
            + ", ".join(v.segment_id for v in unsure)
            + " (confirm these yourself)"
        )
    if not flagged:
        result.notes.append(
            "fact check: no sentence flagged as outdated, contradicted or overstated"
        )


class _Budget:
    """Cumulative counted maximum reservation, including failed/uncertain generation calls."""

    def __init__(self, config: Config, state: State, factory: Callable[[], Drafter], result):
        self.cap = Decimal(config.max_cost_usd)
        self.state, self.factory, self.result = state, factory, result
        self.model: Drafter | None = None
        self.fact_check_hold = Decimal(0)
        self.draft_snapshot = None

    def adapter(self):
        if self.model is None:
            self.model = self.factory()  # the production key is read lazily, only for new work
        return self.model

    def counted(self, request, tokens):
        estimate = counted_estimate(request.model, tokens, request.max_tokens)
        held = self.fact_check_hold if self.kind != "fact_check" else Decimal(0)
        if self.result.estimated_usd + held + estimate.max_cost_usd > self.cap:
            raise DraftingError(
                DraftingErrorCode.BUDGET_EXCEEDED, phase=Phase.COUNT, estimate=estimate
            )
        return estimate

    def count_input_tokens(self, request):
        tokens = self.adapter().count_input_tokens(request)
        if self.kind == "draft" and self.draft_snapshot is not None:
            check = factcheck.build_request(request.model, self.draft_snapshot, [])
            base_tokens = self.adapter().count_input_tokens(check)
            # A JSON-escaped code point takes at most six bytes; reserve one token per byte,
            # with space for every allowed segment's identifier and framing. The real provider
            # count is checked against this hold before the fact-check request is sent.
            segment_envelope = MAX_SEGMENTS * (MAX_SEGMENT_CHARS * 6 + 64)
            self.fact_check_hold = counted_estimate(
                request.model, base_tokens + segment_envelope, check.max_tokens
            ).max_cost_usd
            draft_cost = counted_estimate(request.model, tokens, request.max_tokens).max_cost_usd
            if self.result.estimated_usd + draft_cost + self.fact_check_hold > self.cap:
                required = draft_cost + self.fact_check_hold
                remaining = self.cap - self.result.estimated_usd
                self.fact_check_hold = Decimal(0)
                raise AutomationError(
                    f"DRAFT_AND_FACT_CHECK_BUDGET_EXCEEDED: need ${required:.4f}, "
                    f"remaining ${remaining:.4f}; no draft call made"
                )
        if self.kind == "fact_check" and self.fact_check_hold:
            actual = counted_estimate(request.model, tokens, request.max_tokens).max_cost_usd
            if actual > self.fact_check_hold:
                raise AutomationError("FACT_CHECK_RESERVATION_EXCEEDED")
        self.counted(request, tokens)  # before story-draft writes its attempt marker
        self.count = (request_hash(request), tokens)
        return tokens

    def generate(self, request):
        digest, tokens = self.count
        if digest != request_hash(request):
            raise AutomationError("COUNT_REQUEST_MISMATCH")
        estimate = self.counted(request, tokens).max_cost_usd
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO spending(kind, request_hash, estimated_usd, "
                "attempted_at) VALUES (?, ?, ?, ?)",
                (self.kind, digest, str(estimate), datetime.now(UTC).isoformat()),
            )
        self.result.estimated_usd += estimate  # reserve even if the call raises
        reply = self.adapter().generate(request)
        self.result.charged_usd += charge_estimate(request.model, reply.usage)
        return reply


class _RecordedGeneration:
    def __init__(self, budget: _Budget, state: State, repo: str, revision: str):
        self.budget, self.state, self.repo, self.revision = budget, state, repo, revision

    def count_input_tokens(self, request):
        self.tokens = self.budget.count_input_tokens(request)
        return self.tokens

    def generate(self, request):
        self.state.attempt(
            self.repo,
            self.revision,
            request_hash(request),
            asdict(request),
            self.tokens,
            counted_estimate(request.model, self.tokens, request.max_tokens).max_cost_usd,
        )
        reply = self.budget.generate(request)
        # Raw output is not success. Every recovery runs the original parser/checks again.
        self.state.responded(self.repo, self.revision, asdict(reply))
        return reply


class _RecoveredGeneration:
    def __init__(self, row):
        self.row = row

    def count_input_tokens(self, request):
        self._check(request)
        return self.row["input_tokens"]

    def _check(self, request):
        if request_hash(request) != self.row["request_hash"]:
            raise AutomationError("ATTEMPT_IDENTITY_MISMATCH")

    def generate(self, request):
        self._check(request)
        value = json.loads(self.row["reply_json"])
        value["usage"] = Usage(**value["usage"])
        value["texts"] = tuple(value["texts"])
        return ModelReply(**value)


def _repair(config, budget, result, plan, snapshots) -> None:
    """Auto-repair a new draft (`repair`): free fixes, then at most 2 paid rounds through the
    run's budget. CLEAN publishes the repaired bundle the review page offers instead; SHELVED
    keeps the draft off the page and tells the owner why. Runs once per draft."""
    sid = plan.story.story_id
    if repair.record_dir(config.drafts, sid).exists():
        return
    response = parse_response((plan.paths.draft / "response.json").read_bytes())
    check = factcheck.load(config.state, sid)
    if check is not None and check.get("draft") != plan.paths.draft.name:
        check = None
    records = []
    for directory in snapshots:
        snapshot = load_verified_snapshot(directory)
        if (snapshot.source_url, snapshot.revision_sha) == (
            plan.snapshot.source_url,
            plan.snapshot.revision_sha,
        ):
            records += [Record(s.path, tuple(s.lines)) for s in snapshot.sources]
    budget.kind = "repair"
    before = result.estimated_usd
    outcome = repair.repair(
        plan.brief, response, tuple(records), check, plan.snapshot, budget, config.model
    )
    if outcome.status == repair.NOT_NEEDED:
        return
    final = None
    if outcome.status == repair.CLEAN:
        data = outcome.response.model_dump(mode="json")
        label = data["generator"]["label"]
        data["generator"]["label"] = (label[:80] + " | auto-repaired")[:100]
        raw = repair.raw(data)
        out = repair.repaired_dir(config.drafts, sid, raw)
        if not out.exists():
            build_from_response(prepare_build(plan.snapshot_dir, plan.paths.brief), raw, out)
        final = out.name
        if outcome.fact_check is not None:
            outcome.fact_check["draft"] = final
    repair.save_record(config.drafts, sid, outcome.record(plan.paths.draft.name, final))
    cost = (
        f"reserved ${outcome.reserved_usd:.4f}, list price ${outcome.charged_usd:.4f} "
        f"over {outcome.model_rounds} model round(s)"
    )
    result.repairs.append(
        f"{sid[:12]} {outcome.status}: {'; '.join(outcome.free) or 'no free fix'}; {cost}"
    )
    if outcome.status == repair.CLEAN:
        # The fact check's flags were on words the repair replaced and re-checked.
        flags = [f for f in result.failures if f.startswith("fact check: S")]
        result.failures = [f for f in result.failures if f not in flags]
        if flags:
            result.notes.append(f"repair: fixed {len(flags)} fact-check flag(s) without an edit")
        result.statuses.append("AUTO_REPAIRED")
    if outcome.status == repair.SHELVED:
        reasons = "; ".join(outcome.reasons)
        if any("BUDGET_EXCEEDED" in r for r in outcome.reasons):
            reasons += (
                f" (max_cost_usd ${budget.cap} for this run; ${before:.4f} was already reserved "
                "before repair)"
            )
        result.shelved.append(f"{sid[:12]}: {reasons}")
        result.statuses.append("SHELVED")


def _available(config: Config, set_aside: frozenset = frozenset()):
    return [
        item
        for item in list_backlog(config.preferences, config.backlog)
        if (item.story.story_id, item.story.latest.manifest_sha256) not in set_aside
    ]


# Bumped when source selection changes, so a re-examined commit gets a fresh snapshot directory
# while snapshots that existing drafts point at stay where they are.
SELECTION_VERSION = "2"


def _snapshot_path(config: Config, repo: str, revision: str, paths: tuple[str, ...] = ()) -> Path:
    """One directory per repository, commit and exact file selection: a different selection of
    the same commit never reuses (or overwrites) another selection's snapshot."""
    chosen = "\n".join(sorted(paths))
    key = hashlib.sha256(
        f"{repo}:{revision}:selection-{SELECTION_VERSION}:{chosen}".encode()
    ).hexdigest()
    return config.snapshots / key


def _findings_dir(config: Config, snapshot: Path, fresh: bool) -> Path:
    """Where story-generate writes for `snapshot`: a new directory for a fresh reading (a
    re-examined commit is read again without touching the first reading's files), the latest
    one when recovering a stored reply."""
    base = config.state / (snapshot.name + "-findings")
    existing = [base] + [base.with_name(f"{base.name}-{n}") for n in range(2, 100)]
    if fresh:
        return next(p for p in existing if not p.exists())
    present = [p for p in existing if p.exists()]
    return present[-1] if present else base


def _examine(config, state, budget, result, repo, revision, snapshot, now, allow_paid, row=None):
    if row is None:
        out = _findings_dir(config, snapshot, fresh=True)
        state.set_findings_dir(repo, revision, out)  # recovery uses exactly this directory
    else:
        out = state.findings_dir(repo, revision) or _findings_dir(config, snapshot, fresh=False)
    # A FAILED attempt keeps its complete paid reply, so it is replayed offline exactly like a
    # RESPONDED one (same request hash, same pinned snapshot, no provider request). Only an
    # attempt with no stored reply (UNCERTAIN: the request may or may not have been billed)
    # needs the owner.
    recoverable = row is not None and (
        row["status"] == "RESPONDED" or (row["status"] == "FAILED" and row["reply_json"])
    )
    if row is not None and not recoverable:
        raise AutomationError("GENERATION_" + row["status"] + "_OWNER_INTERVENTION")
    if row is not None and out.exists():
        # Only story-generate publishes this directory, after parsing and context checks.
        discovery = run_discovery(
            config.preferences, config.backlog, [snapshot], [out / "findings-01.json"], now
        )
        proposed = discovery.snapshots[0].findings_supplied
        refused = discovery.refused
    else:
        plan = plan_story_generation(
            config.preferences,
            config.backlog,
            [snapshot],
            out,
            model=config.model,
            budget=config.max_cost_usd,
        )
        if not plan.targets:
            raise AutomationError("NO_ELIGIBLE_CONTEXT")
        if row is not None:
            saved = ModelRequest(**json.loads(row["request_json"]))
            plan = replace(
                plan, model=saved.model, targets=(replace(plan.targets[0], request=saved),)
            )
            drafter = _RecoveredGeneration(row)
            # Already paid: a reduced cap must not prevent offline recovery.
            plan = replace(plan, budget_usd=Decimal(row["estimated_usd"]))
        else:
            if not allow_paid:
                raise AutomationError("PAID_OPT_IN_REQUIRED")
            drafter = _RecordedGeneration(budget, state, repo, revision)
        budget.kind = "discovery"
        try:
            generated = run_story_generation(
                plan, drafter, config.preferences, config.backlog, out, now
            )
        except StoryGenerationFailed as exc:
            if (
                not exc.findings_written
                and isinstance(exc.cause, (DraftingError, StoryError))
                and state.pending(repo)["status"] == "RESPONDED"
            ):
                state.failed(repo, revision)
            raise
        discovery = generated.discovery
        proposed = generated.outcomes[0].proposed
        for key, normalised in generated.outcomes[0].normalised:
            # Not stored with the story (findings v1 is unchanged); the dropped sentence never
            # reaches a draft. `pros eval` shows the sentence itself.
            result.notes += [f"{repo}: story {key}: {q}" for q in normalised.review_questions]
        refused = generated.refused  # includes findings dropped for breaking the schema
    result.new_candidates += discovery.count(RecordOutcome.NEW)
    state.complete(repo, revision, "NO_FINDINGS" if proposed == 0 else "COMPLETED")
    result.evidence_examined += 1
    if refused:
        result.failures.append(f"{repo}: FINDINGS_REFUSED ({refused})")
    if proposed == 0 or not discovery.count(RecordOutcome.NEW):
        result.statuses.append("EXAMINED_NO_STORY")


def run(
    config: Config,
    transport: Transport,
    *,
    allow_paid: bool = False,
    model_factory: Callable[[], Drafter] | None = None,
    now: datetime | None = None,
    reexamine: tuple[str, ...] = (),
) -> RunResult:
    now = now or datetime.now(UTC)
    if (config.workspace_root / STOP_FILE).exists():
        # The owner's off switch: nothing is read, sent or written while it exists.
        return RunResult(statuses=["STOPPED"])
    for directory in (config.state, config.snapshots, config.drafts):
        directory.mkdir(parents=True, exist_ok=True)
    with run_lock(config.state / "run-lock.sqlite"):
        state = State(config.state / "automation.sqlite")
        try:
            return _run_locked(
                config,
                transport,
                state,
                allow_paid,
                model_factory or _production_model,
                now,
                reexamine,
            )
        finally:
            state.close()


def _no_repositories(config: Config, why: str) -> RunResult:
    """A fresh workspace, or every repository off: nothing to read, one thing to do."""
    return RunResult(
        statuses=["NO_REPOSITORIES"],
        failures=[
            f"NO_REPOSITORIES: {why}. Add one: shipyap repo set --repo OWNER/REPO --mode focus "
            f"--db {config.preferences}"
        ],
    )


def _production_model() -> Drafter:
    from shipyap.drafting.provider import AnthropicDrafter, load_api_key

    return AnthropicDrafter(load_api_key())


def _run_locked(config, transport, state, allow_paid, factory, now, reexamine=()):
    drafts_this_week = _paid_drafts_this_week(state, now)
    if config.max_new_drafts and drafts_this_week >= config.weekly_draft_cap:
        # Checked before anything is read or sent: discovery is paid too, and a run that cannot
        # draft must not spend on finding stories it cannot use. (max_new_drafts = 0 is the
        # owner's discovery-only choice and is not stopped here.)
        return RunResult(
            statuses=["WEEKLY_CAP_REACHED"],
            notes=[
                f"draft: weekly cap reached ({drafts_this_week} of {config.weekly_draft_cap} paid "
                "drafts this week); nothing was read, sent or spent. Stories wait in the backlog "
                "until Monday 00:00 UTC, or raise weekly_draft_cap in pros.toml"
            ],
        )
    if not config.preferences.is_file():
        return _no_repositories(config, "no repository is tracked yet")
    with RepositoryPreferences.open(config.preferences, create=False) as preferences:
        repos = sorted(preferences.eligible(), key=lambda r: (r.mode != Mode.FOCUS, r.repo_id))
    if not repos:
        return _no_repositories(config, "no repository is enabled (focus or occasional)")
    with StoryBacklog.open(config.backlog, create=True) as backlog:
        stories = backlog.stories()
    for repo_id in reexamine:
        if repo_id not in {r.repo_id for r in repos}:
            raise AutomationError("REEXAMINE_REPO_NOT_FOCUS_OR_OCCASIONAL")
        if not allow_paid:
            raise AutomationError("REEXAMINE_NEEDS_PAID_OPT_IN")
        waiting = [
            (story.story_id, story.latest.manifest_sha256)
            for story in stories
            if story.repo_id == repo_id and story.state == StoryState.AVAILABLE
        ]
        try:
            state.reexamine(repo_id, waiting, now.strftime("%Y%m%dT%H%M%SZ"))
        except ValueError:
            raise AutomationError("REEXAMINE_BLOCKED_BY_UNFINISHED_ATTEMPT") from None
    set_aside = frozenset(state.set_aside())
    result = RunResult()
    initial_ids = {item.story.story_id for item in _available(config, set_aside)}
    result.existing_available = len(initial_ids)
    budget = _Budget(config, state, factory, result)
    for repo in repos:
        result.repositories_inspected += 1
        try:
            pending = state.pending(repo.repo_id)
            if pending is not None:
                revision = pending["revision"]
                snapshot = state.snapshot_dir(repo.repo_id, revision) or _snapshot_path(
                    config, repo.repo_id, revision
                )
                _examine(
                    config,
                    state,
                    budget,
                    result,
                    repo.repo_id,
                    revision,
                    snapshot,
                    now,
                    allow_paid,
                    pending,
                )
                continue
            selection = select_sources(repo.repo_id, state.cursor(repo.repo_id), transport)
            if selection.unchanged:
                continue
            if not selection.paths:
                state.complete(repo.repo_id, selection.revision)
                result.evidence_examined += 1
                result.statuses.append("EXAMINED_NO_STORY")
                continue
            snapshot = _snapshot_path(config, repo.repo_id, selection.revision, selection.paths)
            if not snapshot.exists():
                allowed = parse_allow_repo(repo.repo_id)
                request = IngestionRequest(
                    request_version="1",
                    workspace_id=config.workspace_id,
                    owner=allowed.owner,
                    repo=allowed.repo,
                    revision_sha=selection.revision,
                    paths=selection.paths,
                )
                run_ingestion(request, allowed, snapshot, transport)
            verified = load_verified_snapshot(snapshot)
            if (
                verified.workspace_id != config.workspace_id
                or verified.revision_sha != selection.revision
                or verified.source_url != f"https://github.com/{repo.repo_id}"
            ):
                raise AutomationError("SNAPSHOT_IDENTITY_MISMATCH")
            state.remember(repo.repo_id, selection.revision, snapshot)
            _examine(
                config,
                state,
                budget,
                result,
                repo.repo_id,
                selection.revision,
                snapshot,
                now,
                allow_paid,
            )
        except _ERRORS as exc:
            result.failures.append(f"{repo.repo_id}: {_code(exc)}")
    # Existing snapshots can be supplied by choosing their parent as config.snapshots.
    snapshots = list(
        dict.fromkeys(
            [
                *state.snapshots(),
                *sorted(
                    p
                    for p in config.snapshots.iterdir()
                    if p.is_dir() and (p / "manifest.json").is_file()
                ),
            ]
        )
    )
    usable = []
    for snapshot in snapshots:
        try:
            if snapshot.resolve().parent != config.snapshots:
                raise AutomationError("SNAPSHOT_OUTSIDE_CONFIGURED_DIRECTORY")
            if load_verified_snapshot(snapshot).workspace_id != config.workspace_id:
                raise AutomationError("SNAPSHOT_WORKSPACE_MISMATCH")
            usable.append(snapshot)
        except _ERRORS as exc:
            result.failures.append(f"snapshot {snapshot.name}: {_code(exc)}")
    snapshots = usable
    result.snapshots = snapshots
    waiting = _available(config, set_aside)
    if config.max_new_drafts and waiting:
        try:
            plan = plan_story_draft(
                config.preferences,
                config.backlog,
                snapshots,
                config.drafts,
                model=config.model,
                budget=config.max_cost_usd,
                authors=config.authors,
                exclude=set_aside,
            )
            if plan is not None:
                if plan.state in (OutputState.FRESH, OutputState.BRIEF_READY):
                    if not allow_paid:
                        raise AutomationError("PAID_OPT_IN_REQUIRED")
                    budget.kind = "draft"
                    budget.draft_snapshot = plan.snapshot
                    drafted = run_story_draft(plan, budget, config.backlog, now)
                    budget.draft_snapshot = None
                else:
                    drafted = recover_story_draft(plan, config.backlog, now)
                kept = plan.paths.response / "response.json"
                if kept.is_file():  # what the parser was given instead of the kept reply
                    repaired = checked_input(kept.read_bytes(), plan.brief).changes
                    result.notes += [f"draft: repaired before checks: {c}" for c in repaired]
                result.drafts_created = int(drafted.draft_status != DraftStatus.NO_POST)
                result.draft_paths.append(plan.paths.draft)
                if result.drafts_created:
                    result.draft_story = plan.story.story_id
                    result.notes.extend(_style_notes(plan, config))
                    if allow_paid:
                        previous_check = factcheck.load(config.state, plan.story.story_id)
                        check_path = (
                            config.state / factcheck.DIRECTORY / f"{plan.story.story_id}.json"
                        )
                        if previous_check is None and not check_path.exists():
                            _fact_check(config, budget, result, plan)
                        else:
                            from shipyap.review.required import completed_fact_check

                            response = parse_response(
                                (plan.paths.draft / "response.json").read_bytes()
                            )
                            if (
                                not isinstance(previous_check, dict)
                                or previous_check.get("draft") != plan.paths.draft.name
                                or not completed_fact_check(previous_check, response)
                            ):
                                result.failures.append(
                                    "fact check: existing record is invalid or belongs to another "
                                    "draft; it was preserved and approval is blocked"
                                )
                    budget.fact_check_hold = Decimal(0)
                    if allow_paid:
                        _repair(config, budget, result, plan, snapshots)
                    result.statuses.append(
                        "BACKLOG_DRAFTED"
                        if plan.story.story_id in initial_ids
                        else "NEW_STORY_DRAFTED"
                    )
                if drafted.draft_status != DraftStatus.READY_FOR_REVIEW:
                    result.statuses.append(drafted.draft_status.value)
        except _ERRORS as exc:
            result.failures.append(f"draft: {_code(exc)}")
    if result.failures:
        result.statuses.append("NEEDS_ATTENTION")
    if not result.statuses:
        result.statuses.append("NEW_STORIES_RECORDED" if result.new_candidates else "NO_CHANGES")
    return result
