"""Orchestration of `story-generate`: model-proposed story findings for eligible pinned snapshots,
recorded only through the unchanged Assignment 06B path.

1. `plan_story_generation` runs offline. It checks the model allowlist and budget format,
   preflights `--out` (never a supplied snapshot or inside one), reads the repository
   preferences once, verifies every snapshot (the existing completion rule, through
   `load_verified_snapshot`) and reads the backlog's existing stories read-only. Snapshots of `off` or unselected repositories are never sent. Each eligible
   snapshot becomes one request with a bounded, line-numbered context (`story_prompt`). No key is
   read and nothing is written.
2. `run_story_generation` asks the provider to count every request's input tokens first (nothing
   is generated), and refuses the whole run if the summed estimated maximum exceeds the budget.
   Then it makes one generation request per snapshot, in priority order (focus, then occasional),
   never retried. Each reply must have ended normally with exactly one text block of strict JSON
   holding nothing but `findings`. The tool adds the 06B header and the 06B strict parser
   (`parse_findings`) must accept the document.
3. Each proposed finding is normalised (`drafting/normalise.py`: narrow or drop, never add) and
   judged by the schema on its own; a broken one is dropped and counted. Each finding must cite
   only lines that were actually sent (`REF_OUTSIDE_SUPPLIED_CONTEXT` otherwise). Citations of
   paths that are not in the snapshot are left to 06B, which stays authoritative for source
   matching.
4. Only after every reply passed steps 2-3 are the findings files published to `--out` (the
   Assignment 02 staged writer), and then `stories.discovery.run_discovery`, unchanged, checks
   and records them in ONE backlog transaction, deduplicating as it always does.

All or nothing: a provider error, a refusal, a truncated or malformed reply, or an over-budget
count stops the run before anything is written or recorded, and no further request is sent.
Replies already received in that run are discarded. Zero findings is a valid reply and records
nothing. Nothing here marks a story offered, approved, scheduled or published; semantic support
stays UNKNOWN. Commit dates are never read.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from shipyap.drafting.errors import CallReport, DraftingError, DraftingErrorCode, Phase
from shipyap.drafting.models import Drafter, ModelReply, ModelRequest
from shipyap.drafting.normalise import Normalised, normalise_finding
from shipyap.drafting.policy import (
    EFFORT,
    MAX_OUTPUT_TOKENS,
    MODELS,
    Estimate,
    charge_estimate,
    counted_estimate,
    offline_estimate,
    parse_budget,
)
from shipyap.drafting.prompt import safe_model_id
from shipyap.drafting.story_prompt import (
    MODEL_KEYS,
    MODEL_OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    ExistingStory,
    StoryContext,
    build_context,
)
from shipyap.editorial.errors import EditorialError
from shipyap.editorial.inputs import sanitized_details
from shipyap.editorial.sources import VerifiedSnapshot, load_verified_snapshot
from shipyap.evidence.report import canonical_json
from shipyap.evidence.snapshot import read_input
from shipyap.evidence.strict_json import StrictJsonError, loads_strict
from shipyap.ingestion.errors import IngestErrorCode, IngestionError
from shipyap.ingestion.writer import preflight_destination, publish_directory
from shipyap.preferences.errors import PreferencesError
from shipyap.preferences.repositories import (
    ELIGIBLE_MODES,
    Mode,
    RepositoryPreferences,
)
from shipyap.stories.backlog import StoryBacklog
from shipyap.stories.discovery import (
    DiscoveryResult,
    SnapshotResult,
    repository_of,
    run_discovery,
)
from shipyap.stories.errors import StoryError, StoryErrorCode
from shipyap.stories.findings import (
    FINDINGS_CAP_BYTES,
    MAX_FINDINGS,
    FindingsDocument,
    StoryFinding,
    parse_findings,
)

_TRUNCATED = frozenset({"max_tokens", "model_context_window_exceeded"})
_MODE_ORDER = {Mode.FOCUS: 0, Mode.OCCASIONAL: 1}
REF_OUTSIDE_SUPPLIED_CONTEXT = "REF_OUTSIDE_SUPPLIED_CONTEXT"


class StoryGenerationErrorCode(StrEnum):
    OUTPUT_EXISTS = "OUTPUT_EXISTS"
    OUTPUT_INSIDE_SNAPSHOT = "OUTPUT_INSIDE_SNAPSHOT"
    OUTPUT_PARENT_INVALID = "OUTPUT_PARENT_INVALID"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"


MESSAGES: dict[StoryGenerationErrorCode, str] = {
    StoryGenerationErrorCode.OUTPUT_EXISTS: "--out already exists; give a new directory",
    StoryGenerationErrorCode.OUTPUT_INSIDE_SNAPSHOT: (
        "--out must not be a --snapshot directory or inside one"
    ),
    StoryGenerationErrorCode.OUTPUT_PARENT_INVALID: (
        "--out must be a new directory inside an existing real directory"
    ),
    StoryGenerationErrorCode.OUTPUT_WRITE_FAILED: "the findings files could not be written",
}


class StoryGenerationError(Exception):
    def __init__(self, code: StoryGenerationErrorCode) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code


Cause = DraftingError | StoryError | EditorialError | PreferencesError | StoryGenerationError


class StoryGenerationFailed(Exception):
    """Generation requests were sent, then the run stopped.

    `sent` generation requests were made (each may have been billed); `calls` are the ones that
    returned a reply. `target` is the snapshot whose request or reply failed, if any. When
    `findings_written` is true the findings files are complete under --out but the backlog
    transaction did not commit, so `story-discover` can record them later without new requests.
    Otherwise nothing was written. Nothing was recorded either way.
    """

    def __init__(
        self,
        cause: Cause,
        *,
        sent: int,
        calls: tuple[CallReport, ...],
        target: StoryContext | None = None,
        findings_written: bool = False,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.sent = sent
        self.calls = calls
        self.target = target
        self.findings_written = findings_written


class SkipReason(StrEnum):
    EXCLUDED = "EXCLUDED"  # off or never selected: not sent
    NO_SNAPSHOT = "NO_SNAPSHOT"  # eligible, but no --snapshot of it was supplied
    NO_CONTEXT = "NO_CONTEXT"  # not one source line fits the context limit: not sent


@dataclass(frozen=True)
class Skipped:
    repo_id: str
    revision_sha: str | None
    mode: Mode | None
    reason: SkipReason


@dataclass(frozen=True)
class Target:
    context: StoryContext
    request: ModelRequest
    request_bytes: int  # UTF-8 bytes of the system prompt, the context and the output schema


@dataclass(frozen=True)
class GenerationPlan:
    model: str
    budget_usd: Decimal
    targets: tuple[Target, ...]  # in request order: focus, then occasional
    skipped: tuple[Skipped, ...]
    offline: Estimate | None  # summed over the targets; None when there is none


@dataclass(frozen=True)
class ContextRefusal:
    story_key: str
    detail: str  # the cited location, never finding text


@dataclass(frozen=True)
class TargetOutcome:
    context: StoryContext
    call: CallReport
    proposed: int
    context_refusals: tuple[ContextRefusal, ...]
    findings_file: str
    recorded: SnapshotResult
    schema_rejected: int = 0  # proposed by the model, dropped for breaking the schema
    # Kept findings the normaliser changed, by story key; its review questions are not stored.
    normalised: tuple[tuple[str, Normalised], ...] = ()


@dataclass(frozen=True)
class GenerationResult:
    preflight: Estimate
    outcomes: tuple[TargetOutcome, ...]
    discovery: DiscoveryResult

    @property
    def refused(self) -> int:
        return self.discovery.refused + sum(
            len(o.context_refusals) + o.schema_rejected for o in self.outcomes
        )


def plan_story_generation(
    preferences_path: Path,
    backlog_path: Path,
    snapshot_dirs: list[Path],
    out: Path,
    *,
    model: str,
    budget: str,
) -> GenerationPlan:
    if model not in MODELS:
        raise DraftingError(DraftingErrorCode.MODEL_UNKNOWN)
    budget_usd = parse_budget(budget)
    if budget_usd is None:
        raise DraftingError(DraftingErrorCode.BUDGET_INVALID)
    _refuse_output_inside(out, snapshot_dirs)
    _preflight(out)
    with RepositoryPreferences.open(preferences_path, create=False) as preferences:
        modes = {saved.repo_id: saved.mode for saved in preferences.list()}  # read once

    snapshots: dict[str, tuple[Path, VerifiedSnapshot, str]] = {}  # by manifest, first wins
    for directory in snapshot_dirs:
        snapshot = load_verified_snapshot(directory)
        snapshots.setdefault(
            snapshot.manifest_sha256, (directory, snapshot, repository_of(snapshot))
        )
    existing = _existing_stories(backlog_path)

    targets: list[Target] = []
    skipped: list[Skipped] = []
    for directory, snapshot, repo_id in snapshots.values():
        mode = modes.get(repo_id)
        if mode not in ELIGIBLE_MODES:
            skipped.append(Skipped(repo_id, snapshot.revision_sha, mode, SkipReason.EXCLUDED))
            continue
        context = build_context(directory, snapshot, repo_id, mode, existing.get(repo_id, ()))
        if not context.supplied_any:
            skipped.append(Skipped(repo_id, snapshot.revision_sha, mode, SkipReason.NO_CONTEXT))
            continue
        targets.append(_target(model, context))
    seen = {repo_id for _, _, repo_id in snapshots.values()}
    skipped.extend(
        Skipped(repo_id, None, mode, SkipReason.NO_SNAPSHOT)
        for repo_id, mode in sorted(
            modes.items(), key=lambda item: (_MODE_ORDER.get(item[1], 2), item[0])
        )
        if mode in ELIGIBLE_MODES and repo_id not in seen
    )
    targets.sort(
        key=lambda t: (
            _MODE_ORDER[t.context.mode],
            t.context.repo_id,
            t.context.snapshot.revision_sha,
            t.context.snapshot.manifest_sha256,
        )
    )
    offline = (
        _total("offline", [offline_estimate(model, t.request_bytes) for t in targets])
        if targets
        else None
    )
    return GenerationPlan(model, budget_usd, tuple(targets), tuple(skipped), offline)


def run_story_generation(
    plan: GenerationPlan,
    drafter: Drafter,
    preferences_path: Path,
    backlog_path: Path,
    out: Path,
    now: datetime,
) -> GenerationResult:
    if not plan.targets:
        raise ValueError("nothing to generate")  # programming error: the CLI checks first
    counted = [
        counted_estimate(plan.model, drafter.count_input_tokens(t.request)) for t in plan.targets
    ]
    preflight = _total("counted", counted)
    if preflight.max_cost_usd > plan.budget_usd:
        raise DraftingError(
            DraftingErrorCode.BUDGET_EXCEEDED, phase=Phase.COUNT, estimate=preflight
        )

    calls: list[CallReport] = []
    accepted: list[tuple] = []
    for sent, target in enumerate(plan.targets, start=1):
        try:
            reply = drafter.generate(target.request)
        except DraftingError as exc:
            raise StoryGenerationFailed(
                exc, sent=sent, calls=tuple(calls), target=target.context
            ) from None
        calls.append(
            CallReport(
                model=safe_model_id(reply.model) or plan.model,
                request_id=reply.request_id,
                usage=reply.usage,
                estimated_charge_usd=charge_estimate(plan.model, reply.usage),
            )
        )
        try:
            document, schema_rejected, changed = _reply_document(reply, target.context)
        except (DraftingError, StoryError) as exc:
            raise StoryGenerationFailed(
                exc, sent=sent, calls=tuple(calls), target=target.context
            ) from None
        kept, refusals = _within_supplied_context(document, target.context)
        accepted.append((kept, len(document.findings), refusals, schema_rejected, changed))

    names = [f"findings-{index:02d}.json" for index in range(1, len(accepted) + 1)]
    files = {
        name: canonical_json(document.model_dump(mode="json")).encode("utf-8")
        for name, (document, *_) in zip(names, accepted, strict=True)
    }
    sent = len(plan.targets)
    try:
        _publish(out, files)
    except StoryGenerationError as exc:
        raise StoryGenerationFailed(exc, sent=sent, calls=tuple(calls)) from None
    try:
        discovery = run_discovery(
            preferences_path,
            backlog_path,
            [t.context.directory for t in plan.targets],
            [out / name for name in names],
            now,
        )
    except (StoryError, EditorialError, PreferencesError) as exc:
        raise StoryGenerationFailed(
            exc, sent=sent, calls=tuple(calls), findings_written=True
        ) from None
    outcomes = tuple(
        TargetOutcome(target.context, call, proposed, refusals, name, recorded, rejected, changed)
        for target, call, (_, proposed, refusals, rejected, changed), name, recorded in zip(
            plan.targets, calls, accepted, names, discovery.snapshots, strict=True
        )
    )
    return GenerationResult(preflight=preflight, outcomes=outcomes, discovery=discovery)


def _target(model: str, context: StoryContext) -> Target:
    request = ModelRequest(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_text=context.user_text,
        output_schema=MODEL_OUTPUT_SCHEMA,
        max_tokens=MAX_OUTPUT_TOKENS,
        effort=EFFORT,
    )
    request_bytes = sum(
        len(part.encode("utf-8"))
        for part in (SYSTEM_PROMPT, context.user_text, canonical_json(MODEL_OUTPUT_SCHEMA))
    )
    return Target(context=context, request=request, request_bytes=request_bytes)


def _existing_stories(backlog_path: Path) -> dict[str, tuple[ExistingStory, ...]]:
    """Every recorded story (any state) per repository, so a model can reuse a key. A missing
    backlog is an empty one; an unusable file is refused, as `story-discover` would."""
    if not backlog_path.exists():
        return {}
    with StoryBacklog.open(backlog_path, create=False) as backlog:
        stories = backlog.stories()
    grouped: dict[str, list[ExistingStory]] = {}
    for story in stories:
        grouped.setdefault(story.repo_id, []).append(
            ExistingStory(story.story_key, story.latest.angle)
        )
    return {repo_id: tuple(items) for repo_id, items in grouped.items()}


def reply_candidates(reply: ModelReply) -> list[object]:
    """The proposed findings of a story-generate reply, or a fixed-code refusal of the whole
    reply (refused, truncated or malformed; not strict JSON holding exactly `findings`)."""

    def malformed(code: DraftingErrorCode) -> DraftingError:
        return DraftingError(code, phase=Phase.GENERATE, request_id=reply.request_id)

    if reply.stop_reason == "refusal":
        raise malformed(DraftingErrorCode.MODEL_REFUSED)
    if reply.stop_reason in _TRUNCATED:
        raise malformed(DraftingErrorCode.MODEL_TRUNCATED)
    if (
        reply.stop_reason != "end_turn"
        or len(reply.texts) != 1
        or safe_model_id(reply.model) is None
    ):
        raise malformed(DraftingErrorCode.MODEL_REPLY_MALFORMED)
    text = reply.texts[0].encode("utf-8", "surrogatepass")
    if len(text) > FINDINGS_CAP_BYTES:
        raise StoryError(StoryErrorCode.FINDINGS_TOO_LARGE)
    try:
        value = loads_strict(text)
    except StrictJsonError:
        raise StoryError(StoryErrorCode.FINDINGS_INVALID_JSON) from None
    if not isinstance(value, dict):
        raise StoryError(StoryErrorCode.FINDINGS_INVALID, ("<root>: expected an object",))
    if set(value) != set(MODEL_KEYS):
        # Tool-owned keys, or no `findings` at all: an empty object is not "zero findings".
        raise StoryError(StoryErrorCode.FINDINGS_INVALID, ("<root>: expected exactly findings",))
    candidates = value["findings"]
    if not isinstance(candidates, list) or len(candidates) > MAX_FINDINGS:
        raise StoryError(StoryErrorCode.FINDINGS_INVALID, ("findings: expected a list",))
    return candidates


@dataclass(frozen=True)
class Screened:
    """One proposed finding after the normaliser and the findings schema. `finding` is None when
    the schema refused it; `reason` then names the failing fields, never their values."""

    normalised: Normalised
    finding: StoryFinding | None
    reason: str | None


DUPLICATE_STORY_KEY = "DUPLICATE_STORY_KEY"
# The angle's first sentence (the finding's main claim) held a number no quote holds: passing
# the rest would offer an angle without its claim.
CORE_CLAIM_DROPPED = "CORE_CLAIM_DROPPED"


def screen_candidates(candidates: list[object], context: StoryContext) -> list[Screened]:
    """Each candidate normalised against the lines that were sent, then judged by the schema
    one at a time: a broken finding is refused alone, and a later duplicate key is refused."""
    files = {source.path: source.lines for source in context.snapshot.sources}
    supplied = {file.path: file.supplied_lines for file in context.coverage}
    screened: list[Screened] = []
    keys: set[str] = set()
    for candidate in candidates:
        normalised = normalise_finding(candidate, files, supplied)
        if normalised.core_claim_dropped:
            screened.append(Screened(normalised, None, CORE_CLAIM_DROPPED))
            continue
        try:
            finding = StoryFinding.model_validate(normalised.candidate)
        except ValidationError as exc:
            reason = "SCHEMA " + "; ".join(sanitized_details(exc))
            screened.append(Screened(normalised, None, reason))
            continue
        if finding.story_key in keys:
            screened.append(Screened(normalised, None, DUPLICATE_STORY_KEY))
            continue
        keys.add(finding.story_key)
        screened.append(Screened(normalised, finding, None))
    return screened


def _reply_document(
    reply: ModelReply, context: StoryContext
) -> tuple[FindingsDocument, int, tuple[tuple[str, Normalised], ...]]:
    """The reply as a 06B findings document for the context's snapshot, the number of proposed
    findings the schema refused, and what the normaliser changed in each kept finding; or a
    fixed-code refusal. Files supplied to story-discover keep the strict whole-document parse."""
    snapshot = context.snapshot
    candidates = reply_candidates(reply)
    screened = screen_candidates(candidates, context)
    kept = [item for item in screened if item.finding is not None]
    if candidates and not kept:
        # Not "nothing worth a story": every proposal was unusable.
        raise StoryError(
            StoryErrorCode.FINDINGS_INVALID, ("findings: every proposed finding was invalid",)
        )
    document = canonical_json(
        {
            "findings": [item.normalised.candidate for item in kept],
            "findings_version": "1",
            "workspace_id": snapshot.workspace_id,
            "snapshot_manifest_sha256": snapshot.manifest_sha256,
        }
    ).encode("utf-8")
    if len(document) > FINDINGS_CAP_BYTES:  # canonical form escapes non-ASCII and can grow
        raise StoryError(StoryErrorCode.FINDINGS_TOO_LARGE)
    changed = tuple(
        (item.finding.story_key, item.normalised)
        for item in kept
        if item.finding is not None and item.normalised.changes
    )
    return parse_findings(document), len(candidates) - len(kept), changed


def _within_supplied_context(
    document: FindingsDocument, context: StoryContext
) -> tuple[FindingsDocument, tuple[ContextRefusal, ...]]:
    """Refuse a finding that cites lines of a snapshot file that exist but were not sent. Every
    other reference is left for 06B, which stays authoritative: a path the snapshot does not hold
    (REF_PATH_NOT_IN_SNAPSHOT) and a range past the end of the file (REF_RANGE_INVALID)."""
    totals = {file.path: file.total_lines for file in context.coverage}
    kept, refusals = [], []
    for finding in document.findings:
        outside = next(
            (
                ref
                for ref in finding.source_refs
                if ref.path in totals
                and context.supplied_through(ref.path) < ref.line_end <= totals[ref.path]
            ),
            None,
        )
        if outside is None:
            kept.append(finding)
        else:
            label = f"{outside.path}:{outside.line_start}-{outside.line_end}"
            refusals.append(ContextRefusal(finding.story_key, label))
    return document.model_copy(update={"findings": tuple(kept)}), tuple(refusals)


def _total(basis: Literal["offline", "counted"], estimates: list[Estimate]) -> Estimate:
    return Estimate(
        basis=basis,
        input_tokens=sum(e.input_tokens for e in estimates),
        max_input_tokens=sum(e.max_input_tokens for e in estimates),
        max_output_tokens=sum(e.max_output_tokens for e in estimates),
        max_cost_usd=sum((e.max_cost_usd for e in estimates), Decimal(0)),
    )


_OUTPUT_CODES = {
    IngestErrorCode.OUTPUT_EXISTS: StoryGenerationErrorCode.OUTPUT_EXISTS,
    IngestErrorCode.OUTPUT_PARENT_INVALID: StoryGenerationErrorCode.OUTPUT_PARENT_INVALID,
}


def _output_error(exc: IngestionError) -> StoryGenerationError:
    return StoryGenerationError(
        _OUTPUT_CODES.get(exc.code, StoryGenerationErrorCode.OUTPUT_WRITE_FAILED)
    )


def _refuse_output_inside(out: Path, snapshot_dirs: list[Path]) -> None:
    """Publishing under any supplied snapshot, sent or not, would break its completion rule.
    Resolved paths, so a link or junction above --out cannot hide the relationship."""
    try:
        resolved = out.resolve()
        inputs = [directory.resolve() for directory in snapshot_dirs]
    except (OSError, ValueError, RuntimeError):
        raise StoryGenerationError(StoryGenerationErrorCode.OUTPUT_PARENT_INVALID) from None
    if any(resolved.is_relative_to(directory) for directory in inputs):
        raise StoryGenerationError(StoryGenerationErrorCode.OUTPUT_INSIDE_SNAPSHOT)


def _preflight(out: Path) -> None:
    try:
        preflight_destination(out)
    except IngestionError as exc:
        raise _output_error(exc) from None


def _publish(out: Path, files: dict[str, bytes]) -> None:
    def verify(directory: Path) -> None:
        if {entry.name for entry in directory.iterdir()} != set(files):
            raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT)
        for name, expected in files.items():
            data = read_input(directory / name, FINDINGS_CAP_BYTES)
            if data != expected:
                raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT)
            try:
                parse_findings(data)
            except StoryError:
                raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT) from None

    try:
        publish_directory(out, list(files.items()), verify)
    except IngestionError as exc:
        raise _output_error(exc) from None
