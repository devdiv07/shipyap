"""Orchestration of `story-draft`: one available backlog story, through the unchanged editorial brief
and `draft-model` path, into one reviewable LinkedIn draft bundle linked to that story.

1. `plan_story_draft` runs offline. It checks the model allowlist and budget format, reads the
   repository preferences once and selects the first story of the existing ranking
   (`stories.discovery.rank`: available stories of focus, then occasional, repositories, oldest
   first). Off and unselected repositories and offered stories are never selected. No candidate
   means no request at all. The story's latest evidence must be re-located in the supplied
   snapshot with the same manifest digest, by the unchanged 06B check (`check_finding`), with
   every cited file's SHA-256 equal to the recorded one; anything else is refused before any
   request. The brief is built in memory with the unchanged `build_brief`.
2. The contribution is generated, never asked for, and says only what is recorded. The backlog
   holds an angle, its support and located citations; who proposed them is not recorded and
   their semantic support is UNKNOWN. So the author is "not recorded", the relationship is
   `reader` (no ownership is claimed), every item that carries backlog text is labelled
   unverified, `attempted` and `learned` state that nothing is recorded, and `do_not_claim`
   forbids first-person achievements, ownership, measurements, impact and motivations. A text
   that does not fit an item with its label is refused (CONTRIBUTION_UNREPRESENTABLE), never cut.
3. Outputs have fixed names under `--drafts`, derived from the story id, in the order they are
   written: `<id>-brief` (brief bundle), `<id>-attempt` (`attempt.json`: a generation request is
   about to be sent), `<id>-response` (`response.json`: the checked reply, kept), `<id>-draft`
   (draft bundle) and `<id>-link` (`story-link.json`). Each is published by the existing staged
   writers. Every output already there is verified, and each needs the ones before it (the
   response is optional); anything else is refused (OUTPUT_CONFLICT) before any request.
   `--drafts` must not be a supplied snapshot or inside one (OUTPUT_INSIDE_SNAPSHOT, as
   `story-generate --out`).
4. `run_story_draft` publishes the brief (unless the same brief is already there), then runs the
   unchanged `plan_model_draft` / `run_model_draft` against it: one token count, then at most one
   generation request, never retried, checked by `build_from_response`. The attempt is recorded
   before that request and the reply kept after it. Only after the draft bundle is published is
   the link written, and only after the link is published is the story marked offered, in one
   backlog transaction.
5. A rerun never sends a second generation request for the same brief (`recover_story_draft`).
   With a kept response and no draft, it builds the draft from that response; with a draft, it
   completes the link and the offer. With an attempt and nothing after it (the request failed,
   or its reply could not be kept), it is refused (GENERATION_ATTEMPTED): only removing
   `<id>-attempt` allows one new request.

Nothing here approves, schedules or publishes; the draft is UNAPPROVED and every statement's
semantic support stays UNKNOWN.
"""

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from shipyap.drafting.errors import DraftingError, DraftingErrorCode
from shipyap.drafting.models import Drafter, ModelReply, ModelRequest
from shipyap.drafting.policy import (
    MAX_MODEL_BRIEF_BYTES,
    MODELS,
    Estimate,
    offline_estimate,
    parse_budget,
)
from shipyap.drafting.prompt import (
    MODEL_OUTPUT_SCHEMA,
    STORY_PROMPT_VERSION,
    STORY_SYSTEM_PROMPT,
)
from shipyap.drafting.service import (
    ModelDraftResult,
    checked_input,
    plan_model_draft,
    response_bytes,
    run_model_draft,
)
from shipyap.editorial.brief import build_brief, render_brief_json
from shipyap.editorial.bundle import BundleIncomplete, publish_bundle, verify_bundle
from shipyap.editorial.errors import EditorialError
from shipyap.editorial.inputs import RESPONSE_CAP_BYTES
from shipyap.editorial.models import (
    CITED_EVIDENCE_PREFIX,
    NO_PERSONAL_RECORD,
    NOT_ATTEMPTED,
    NOT_LEARNED,
    OWNER_LIMITATION,
    PROVENANCE,
    ROLES_PREFIX,
    UNRECORDED_AUTHOR,
    AuthorContribution,
    Brief,
    BundleKind,
    BundleRecord,
    DraftStatus,
    Relationship,
    SourceRef,
)
from shipyap.editorial.render import render_brief_md
from shipyap.editorial.service import build_from_response
from shipyap.editorial.sources import VerifiedSnapshot, load_verified_snapshot
from shipyap.evidence.report import canonical_json
from shipyap.evidence.snapshot import read_input
from shipyap.ingestion.errors import IngestErrorCode, IngestionError
from shipyap.ingestion.writer import publish_directory
from shipyap.preferences.repositories import RepositoryPreferences
from shipyap.stories.backlog import Story, StoryBacklog
from shipyap.stories.discovery import rank, repository_of
from shipyap.stories.errors import StoryError
from shipyap.stories.findings import StoryFinding, check_finding
from shipyap.stories.roles import role

LINK_FILE = "story-link.json"
LINK_CAP_BYTES = 64 * 1024
ATTEMPT_FILE = "attempt.json"
RESPONSE_FILE = "response.json"

AUTHOR_NAME = UNRECORDED_AUTHOR  # the brief and the checker treat this name as "no author"
AUDIENCE = "Technical readers on LinkedIn interested in public software and research repositories"
ANGLE_LABEL = "Unverified backlog angle: "
SUPPORT_LABEL = "Unverified backlog support: "
# PROVENANCE, NOT_ATTEMPTED, NOT_LEARNED, NO_PERSONAL_RECORD, OWNER_LIMITATION and the cited and
# roles lines live in editorial.models, which marks them as tool text the post may never cite.
DO_NOT_CLAIM = (
    "Any first-person achievement, experience or lesson",
    "Ownership of the repository, the work or any experiment in it",
    "Any performance measurement or result that the cited lines do not state",
    "Any impact, adoption or importance beyond what the cited lines state",
    "Any motivation or intention of the repository's authors",
    "That the angle or its support has been verified",
)
VOICE = "Objective third person. No first person: no author statement is recorded."

# With an author profile for the story's repository, the owner's own statements are recorded:
# first person is allowed for them alone (the checker requires a contributor reference for it).
OWNER_VOICE = (
    "First person singular, plain and specific, like the author talking to technical peers. "
    "Use 'I' only for what the author's own items state; everything else comes from the cited "
    "lines."
)
OWNER_DO_NOT_CLAIM = (
    "Any performance measurement or result that the cited lines do not state",
    "Any result stated as true in general when the cited lines measure it in a specific setting",
    "Any impact, adoption or importance beyond what the cited lines state",
    "That the angle or its support has been verified",
)


@dataclass(frozen=True)
class AuthorProfile:
    """The owner's own, standing statements about one repository, from the automation config.
    They are the owner's words, shown back in review; nothing checks them for truth."""

    name: str
    relationship: Relationship
    statements: tuple[str, ...]  # first-person facts, e.g. "I built this repository to test X."
    learned: tuple[str, ...] = ()
    do_not_claim: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()


class StoryDraftErrorCode(StrEnum):
    DRAFTS_DIR_INVALID = "DRAFTS_DIR_INVALID"
    OUTPUT_INSIDE_SNAPSHOT = "OUTPUT_INSIDE_SNAPSHOT"
    EVIDENCE_SNAPSHOT_NOT_SUPPLIED = "EVIDENCE_SNAPSHOT_NOT_SUPPLIED"
    EVIDENCE_NOT_VERIFIED = "EVIDENCE_NOT_VERIFIED"
    CONTRIBUTION_UNREPRESENTABLE = "CONTRIBUTION_UNREPRESENTABLE"
    OUTPUT_CONFLICT = "OUTPUT_CONFLICT"
    GENERATION_ATTEMPTED = "GENERATION_ATTEMPTED"
    ATTEMPT_WRITE_FAILED = "ATTEMPT_WRITE_FAILED"
    LINK_WRITE_FAILED = "LINK_WRITE_FAILED"


MESSAGES: dict[StoryDraftErrorCode, str] = {
    StoryDraftErrorCode.DRAFTS_DIR_INVALID: "--drafts must be an existing real directory",
    StoryDraftErrorCode.OUTPUT_INSIDE_SNAPSHOT: (
        "--drafts must not be a --snapshot directory or inside one"
    ),
    StoryDraftErrorCode.EVIDENCE_SNAPSHOT_NOT_SUPPLIED: (
        "no --snapshot has the manifest digest of the selected story's evidence"
    ),
    StoryDraftErrorCode.EVIDENCE_NOT_VERIFIED: (
        "the selected story's recorded evidence could not be located unchanged in its snapshot"
    ),
    StoryDraftErrorCode.CONTRIBUTION_UNREPRESENTABLE: (
        "the story's recorded text does not fit the contribution with its provenance label; "
        "nothing was shortened"
    ),
    StoryDraftErrorCode.OUTPUT_CONFLICT: (
        "an output for this story already exists under --drafts and is not the complete, "
        "matching output of this story's current evidence"
    ),
    StoryDraftErrorCode.GENERATION_ATTEMPTED: (
        "a generation request was already sent for this story's brief and left no draft or kept "
        "response; nothing is sent again automatically. Remove the story's -attempt output "
        "under --drafts to allow one new request"
    ),
    StoryDraftErrorCode.ATTEMPT_WRITE_FAILED: (
        "the generation attempt could not be recorded, so the generation request was not sent"
    ),
    StoryDraftErrorCode.LINK_WRITE_FAILED: "the story link record could not be written",
}


class StoryDraftError(Exception):
    def __init__(self, code: StoryDraftErrorCode) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code


class OutputState(StrEnum):
    FRESH = "FRESH"  # nothing written yet for this story
    BRIEF_READY = "BRIEF_READY"  # the same brief bundle is complete; no draft yet
    ATTEMPTED = "ATTEMPTED"  # a generation request was sent; nothing to build a draft from
    RESPONDED = "RESPONDED"  # the reply was kept; the draft bundle is missing
    DRAFTED = "DRAFTED"  # the draft bundle is complete; the link is missing
    LINKED = "LINKED"  # draft and link are complete; only the offer may be missing


@dataclass(frozen=True)
class StoryPaths:
    brief: Path
    attempt: Path
    response: Path
    draft: Path
    link: Path


@dataclass(frozen=True)
class StoryDraftPlan:
    story: Story
    snapshot_dir: Path
    snapshot: VerifiedSnapshot
    brief: Brief
    brief_json: bytes
    brief_md: bytes
    brief_sha256: str
    paths: StoryPaths
    state: OutputState
    response: bytes | None  # the kept response.json, when there is one
    model: str
    budget: str
    offline: Estimate


@dataclass(frozen=True)
class StoryDraftResult:
    story: Story  # as stored after the offer
    draft_status: DraftStatus
    draft_record_sha256: str
    model_run: ModelDraftResult | None  # None: recovered from verified files, no request


class StoryDraftFailed(Exception):
    """The draft bundle was published, then linking or the offer failed. The story stays
    available; a rerun completes the link and the offer without a new request."""

    def __init__(
        self, cause: "StoryDraftError | StoryError", model_run: ModelDraftResult | None
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.model_run = model_run


def story_paths(drafts: Path, sid: str) -> StoryPaths:
    return StoryPaths(
        drafts / f"{sid}-brief",
        drafts / f"{sid}-attempt",
        drafts / f"{sid}-response",
        drafts / f"{sid}-draft",
        drafts / f"{sid}-link",
    )


def plan_story_draft(
    preferences_path: Path,
    backlog_path: Path,
    snapshot_dirs: list[Path],
    drafts: Path,
    *,
    model: str,
    budget: str,
    authors: Mapping[str, AuthorProfile] | None = None,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> StoryDraftPlan | None:
    """None when no story is available: nothing is verified, built or sent. `authors` maps a
    repository id to the owner's profile for it; a repository without one keeps the
    third-person, no-author contribution."""
    if model not in MODELS:
        raise DraftingError(DraftingErrorCode.MODEL_UNKNOWN)
    budget_usd = parse_budget(budget)
    if budget_usd is None:
        raise DraftingError(DraftingErrorCode.BUDGET_INVALID)
    if not _is_real_directory(drafts):
        raise StoryDraftError(StoryDraftErrorCode.DRAFTS_DIR_INVALID)
    _refuse_drafts_inside(drafts, snapshot_dirs)
    with RepositoryPreferences.open(preferences_path, create=False) as preferences:
        modes = {saved.repo_id: saved.mode for saved in preferences.list()}  # read once
    with StoryBacklog.open(backlog_path, create=False) as backlog:
        ranked = [
            item
            for item in rank(backlog.stories(), modes)
            # Set aside by the owner (re-examined repository) for the evidence it was found in.
            if (item.story.story_id, item.story.latest.manifest_sha256) not in exclude
        ]
    if not ranked:
        return None
    story = ranked[0].story

    snapshot_dir, snapshot = _evidence_snapshot(story, snapshot_dirs)
    author = (authors or {}).get(story.repo_id)
    roles = roles_note([source.path for source in snapshot.sources])
    contribution = story_contribution(story, snapshot.workspace_id, author, roles)
    contribution_sha256 = _sha256(canonical_json(contribution.model_dump(mode="json")).encode())
    brief = build_brief(snapshot, contribution, contribution_sha256)
    brief_json = render_brief_json(brief)
    brief_sha256 = _sha256(brief_json)
    brief_md = render_brief_md(brief, brief_sha256).encode("utf-8")
    if len(brief_md) > MAX_MODEL_BRIEF_BYTES:
        raise DraftingError(DraftingErrorCode.BRIEF_TOO_LARGE_FOR_MODEL)
    paths = story_paths(drafts, story.story_id)
    state, response = _output_state(story, paths, brief_json, brief)
    request_bytes = sum(
        len(part)
        for part in (
            STORY_SYSTEM_PROMPT.encode(),
            brief_md,
            json.dumps(MODEL_OUTPUT_SCHEMA).encode(),
        )
    )
    return StoryDraftPlan(
        story=story,
        snapshot_dir=snapshot_dir,
        snapshot=snapshot,
        brief=brief,
        brief_json=brief_json,
        brief_md=brief_md,
        brief_sha256=brief_sha256,
        paths=paths,
        state=state,
        response=response,
        model=model,
        budget=budget,
        offline=offline_estimate(model, request_bytes),
    )


def run_story_draft(
    plan: StoryDraftPlan, drafter: Drafter, backlog_path: Path, now: datetime
) -> StoryDraftResult:
    if plan.state not in (OutputState.FRESH, OutputState.BRIEF_READY):
        raise ValueError("this story already has a draft; recover it instead")  # CLI checks first
    if plan.state == OutputState.FRESH:
        publish_bundle(
            plan.paths.brief,
            BundleKind.BRIEF,
            {"brief.json": plan.brief_json, "brief.md": plan.brief_md},
            workspace_id=plan.brief.workspace_id,
            snapshot_manifest_sha256=plan.snapshot.manifest_sha256,
            brief_sha256=plan.brief_sha256,
        )
    # The unchanged draft-model path: it re-verifies the snapshot and the brief bundle on disk.
    prepared, model_plan = plan_model_draft(
        plan.snapshot_dir,
        plan.paths.brief,
        plan.paths.draft,
        model=plan.model,
        budget=plan.budget,
        system_prompt=STORY_SYSTEM_PROMPT,
    )
    if prepared.brief_sha256 != plan.brief_sha256:
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    recorded = _RecordedDrafter(drafter, plan)
    model_run = run_model_draft(prepared, model_plan, recorded, plan.paths.draft)
    return _link_and_offer(plan, backlog_path, now, model_run)


def recover_story_draft(
    plan: StoryDraftPlan, backlog_path: Path, now: datetime
) -> StoryDraftResult:
    """Complete an interrupted run from its verified outputs. Sends nothing, ever: a generation
    request that left nothing to build from is refused, not repeated."""
    if plan.state in (OutputState.FRESH, OutputState.BRIEF_READY):
        raise ValueError("this story has no generation to recover")  # CLI checks first
    if plan.state == OutputState.ATTEMPTED:
        raise StoryDraftError(StoryDraftErrorCode.GENERATION_ATTEMPTED)
    if plan.state == OutputState.RESPONDED and plan.response is not None:
        # Offline: the same checks and publication as the run that kept this response.
        prepared, _ = plan_model_draft(
            plan.snapshot_dir,
            plan.paths.brief,
            plan.paths.draft,
            model=plan.model,
            budget=plan.budget,
        )
        if prepared.brief_sha256 != plan.brief_sha256:
            raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
        # A response kept before a model-reply fix is rebuilt under the same rules as a new one.
        build_from_response(
            prepared, checked_input(plan.response, prepared.brief).data, plan.paths.draft
        )
    return _link_and_offer(plan, backlog_path, now, None)


def attempt_document(story: Story, brief_sha256: str) -> bytes:
    """The record that one generation request for this brief is about to be sent."""
    return canonical_json(
        {
            "attempt_version": "1",
            "story_id": story.story_id,
            "evidence_id": story.latest.evidence_id,
            "brief_sha256": brief_sha256,
        }
    ).encode("utf-8")


class _RecordedDrafter:
    """The provider, with the generation request bracketed: the attempt is published before it
    is sent (or it is not sent), and the reply's response.json is kept after it, so that a later
    failure never calls for a second request. Counting is passed through untouched."""

    def __init__(self, drafter: Drafter, plan: StoryDraftPlan) -> None:
        self._drafter = drafter
        self._plan = plan

    def count_input_tokens(self, request: ModelRequest) -> int:
        return self._drafter.count_input_tokens(request)

    def generate(self, request: ModelRequest) -> ModelReply:
        plan = self._plan
        _publish_one(
            plan.paths.attempt,
            ATTEMPT_FILE,
            attempt_document(plan.story, plan.brief_sha256),
            LINK_CAP_BYTES,
            StoryDraftErrorCode.ATTEMPT_WRITE_FAILED,
        )
        reply = self._drafter.generate(request)
        try:
            data = response_bytes(reply, plan.brief_sha256, STORY_PROMPT_VERSION)
            _publish_one(
                plan.paths.response,
                RESPONSE_FILE,
                data,
                RESPONSE_CAP_BYTES,
                StoryDraftErrorCode.OUTPUT_CONFLICT,
            )
        except (DraftingError, EditorialError, StoryDraftError):
            # run_model_draft refuses an unusable reply itself. Without a kept response the
            # attempt record alone remains, and a rerun refuses rather than resends.
            pass
        return reply


def roles_note(paths: list[str]) -> str:
    """One limitation line naming each snapshot file's evidence role (at most 600 characters)."""
    head = ROLES_PREFIX
    entries = [f"{PurePosixPath(p).name}={role(p)}" for p in paths]
    while entries and len(head + "; ".join(entries) + ".") > 600:
        entries.pop()
    return head + "; ".join(entries) + "."


def story_contribution(
    story: Story, workspace_id: str, author: AuthorProfile | None = None, roles: str | None = None
) -> AuthorContribution:
    """The contribution for a backlog story: only what the backlog records, each part labelled,
    plus the owner's own statements when `author` is given for the story's repository."""
    evidence = story.latest
    focus = tuple(dict.fromkeys(ref.path for ref in evidence.refs))[:3]
    cited = ", ".join(ref.label() for ref in evidence.refs)
    if author is not None:
        try:
            return AuthorContribution(
                contribution_version="1",
                workspace_id=workspace_id,
                author_name=author.name,
                relationship=author.relationship,
                channel="linkedin",
                audience=AUDIENCE,
                insight=ANGLE_LABEL + evidence.angle,
                attempted=author.statements,
                learned=author.learned or (NOT_LEARNED,),
                why_it_matters=(SUPPORT_LABEL + evidence.support,),
                limitations=(
                    PROVENANCE,
                    f"{CITED_EVIDENCE_PREFIX}{evidence.revision_sha}: {cited}.",
                    OWNER_LIMITATION,
                    *((roles,) if roles else ()),
                ),
                do_not_claim=(*author.do_not_claim, *OWNER_DO_NOT_CLAIM),
                voice_notes=OWNER_VOICE,
                hashtags=author.hashtags,
                focus_paths=focus,
            )
        except ValidationError:
            raise StoryDraftError(StoryDraftErrorCode.CONTRIBUTION_UNREPRESENTABLE) from None
    try:
        return AuthorContribution(
            contribution_version="1",
            workspace_id=workspace_id,
            author_name=AUTHOR_NAME,
            relationship=Relationship.READER,
            channel="linkedin",
            audience=AUDIENCE,
            insight=ANGLE_LABEL + evidence.angle,
            attempted=(NOT_ATTEMPTED,),
            learned=(NOT_LEARNED,),
            why_it_matters=(SUPPORT_LABEL + evidence.support,),
            limitations=(
                PROVENANCE,
                f"{CITED_EVIDENCE_PREFIX}{evidence.revision_sha}: {cited}.",
                NO_PERSONAL_RECORD,
                *((roles,) if roles else ()),
            ),
            do_not_claim=DO_NOT_CLAIM,
            voice_notes=VOICE,
            hashtags=(),
            focus_paths=focus,
        )
    except ValidationError:
        raise StoryDraftError(StoryDraftErrorCode.CONTRIBUTION_UNREPRESENTABLE) from None


def link_document(
    story: Story, paths: StoryPaths, draft_record_sha256: str, record: BundleRecord
) -> bytes:
    """The story-to-draft link: the story's identity and evidence exactly as stored, and the
    digests of the brief and the draft bundle record made from them."""
    evidence = story.latest
    return canonical_json(
        {
            "link_version": "1",
            "review_state": "UNAPPROVED",
            "semantic_support": "UNKNOWN",
            "story_id": story.story_id,
            "repo_id": story.repo_id,
            "story_key": story.story_key,
            "evidence_id": evidence.evidence_id,
            "source_url": evidence.source_url,
            "revision_sha": evidence.revision_sha,
            "manifest_sha256": evidence.manifest_sha256,
            "angle": evidence.angle,
            "support": evidence.support,
            "refs": [
                {
                    "path": ref.path,
                    "line_start": ref.line_start,
                    "line_end": ref.line_end,
                    "quote": ref.quote,
                    "file_sha256": ref.file_sha256,
                }
                for ref in evidence.refs
            ],
            "brief_bundle": paths.brief.name,
            "draft_bundle": paths.draft.name,
            "brief_sha256": record.brief_sha256,
            "response_sha256": record.response_sha256,
            "draft_status": record.draft_status.value,
            "draft_record_sha256": draft_record_sha256,
        }
    ).encode("utf-8")


def _evidence_snapshot(story: Story, snapshot_dirs: list[Path]) -> tuple[Path, VerifiedSnapshot]:
    evidence = story.latest
    for directory in snapshot_dirs:
        snapshot = load_verified_snapshot(directory)
        if snapshot.manifest_sha256 == evidence.manifest_sha256:
            break
    else:
        raise StoryDraftError(StoryDraftErrorCode.EVIDENCE_SNAPSHOT_NOT_SUPPLIED)
    if (
        repository_of(snapshot) != story.repo_id
        or snapshot.source_url != evidence.source_url
        or snapshot.revision_sha != evidence.revision_sha
    ):
        raise StoryDraftError(StoryDraftErrorCode.EVIDENCE_NOT_VERIFIED)
    try:
        finding = StoryFinding(
            story_key=story.story_key,
            angle=evidence.angle,
            support=evidence.support,
            source_refs=tuple(
                SourceRef(
                    path=ref.path,
                    line_start=ref.line_start,
                    line_end=ref.line_end,
                    quote=ref.quote,
                )
                for ref in evidence.refs
            ),
        )
    except ValidationError:
        raise StoryDraftError(StoryDraftErrorCode.EVIDENCE_NOT_VERIFIED) from None
    check = check_finding(finding, snapshot)
    if check.refusal is not None or check.refs != evidence.refs:
        raise StoryDraftError(StoryDraftErrorCode.EVIDENCE_NOT_VERIFIED)
    return directory, snapshot


# Each output is written only after these; the kept response is optional for a draft.
_PREDECESSORS = {"attempt": "brief", "response": "attempt", "draft": "attempt", "link": "draft"}


def _output_state(
    story: Story, paths: StoryPaths, brief_json: bytes, brief: Brief
) -> tuple[OutputState, bytes | None]:
    """Every output present is verified, whichever later ones exist, before any is relied on."""
    present = {name: _exists(path) for name, path in vars(paths).items()}
    if any(present[name] and not present[before] for name, before in _PREDECESSORS.items()):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    if not present["brief"]:
        return OutputState.FRESH, None
    try:
        bundle = verify_bundle(paths.brief, BundleKind.BRIEF)
    except BundleIncomplete:
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT) from None
    if bundle.files["brief.json"] != brief_json:
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    if not present["attempt"]:
        return OutputState.BRIEF_READY, None
    attempt = _read_one(paths.attempt, ATTEMPT_FILE, LINK_CAP_BYTES)
    if attempt != attempt_document(story, _sha256(brief_json)):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    response = None
    if present["response"]:
        response = _read_one(paths.response, RESPONSE_FILE, RESPONSE_CAP_BYTES)
    if not present["draft"]:
        if response is None:
            return OutputState.ATTEMPTED, None
        return OutputState.RESPONDED, response
    record_sha256, record = _verified_draft(paths.draft, brief_json, response, brief)
    if not present["link"]:
        return OutputState.DRAFTED, response
    _require_link(paths.link, story, paths, record_sha256, record)
    return OutputState.LINKED, response


def _verified_draft(
    directory: Path, brief_json: bytes, response: bytes | None = None, brief: Brief | None = None
) -> tuple[str, BundleRecord]:
    """With `response`, the bundle must have been built from the kept response: as kept, or as
    `checked_input` repairs it (the same deterministic result, re-derived here)."""
    try:
        bundle = verify_bundle(directory, BundleKind.DRAFT)
    except BundleIncomplete:
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT) from None
    if bundle.files["brief.json"] != brief_json or bundle.record.brief_sha256 != _sha256(
        brief_json
    ):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    built = bundle.files["response.json"]
    if (
        response is not None
        and built != response
        and (brief is None or built != checked_input(response, brief).data)
    ):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    return bundle.record_sha256, bundle.record


def _require_link(
    link: Path, story: Story, paths: StoryPaths, record_sha256: str, record: BundleRecord
) -> None:
    expected = link_document(story, paths, record_sha256, record)
    if _read_one(link, LINK_FILE, LINK_CAP_BYTES) != expected:
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)


def _read_one(directory: Path, name: str, cap: int) -> bytes:
    """The bytes of a one-file output: a real directory holding exactly `name`, a regular file."""
    if not _is_real_directory(directory):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    try:
        names = {entry.name for entry in os.scandir(directory)}
    except OSError:
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT) from None
    data = read_input(directory / name, cap)
    if names != {name} or not isinstance(data, bytes):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT)
    return data


def _link_and_offer(
    plan: StoryDraftPlan, backlog_path: Path, now: datetime, model_run: ModelDraftResult | None
) -> StoryDraftResult:
    try:
        record_sha256, record = _verified_draft(plan.paths.draft, plan.brief_json)
        if not _exists(plan.paths.link):
            _publish_link(
                plan.paths.link, link_document(plan.story, plan.paths, record_sha256, record)
            )
        _require_link(plan.paths.link, plan.story, plan.paths, record_sha256, record)
        # Only now is the reviewable output durable and locatable from the story id.
        with StoryBacklog.open(backlog_path, create=False) as backlog, backlog.transaction():
            offered = backlog.mark_offered(plan.story.story_id, now)
    except (StoryDraftError, StoryError) as exc:
        raise StoryDraftFailed(exc, model_run) from None
    return StoryDraftResult(offered, record.draft_status, record_sha256, model_run)


def _publish_link(out: Path, data: bytes) -> None:
    _publish_one(out, LINK_FILE, data, LINK_CAP_BYTES, StoryDraftErrorCode.LINK_WRITE_FAILED)


def _publish_one(out: Path, name: str, data: bytes, cap: int, failure: StoryDraftErrorCode) -> None:
    def verify(directory: Path) -> None:
        if {entry.name for entry in directory.iterdir()} != {name}:
            raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT)
        if read_input(directory / name, cap) != data:
            raise IngestionError(IngestErrorCode.INCOMPLETE_SNAPSHOT)

    try:
        publish_directory(out, [(name, data)], verify)
    except IngestionError:
        raise StoryDraftError(failure) from None


def _refuse_drafts_inside(drafts: Path, snapshot_dirs: list[Path]) -> None:
    """As `story-generate --out` (06C): publishing under any supplied snapshot, the story's or
    not, would break its completion rule. Resolved paths, so a link or junction above --drafts
    cannot hide the relationship."""
    try:
        resolved = drafts.resolve()
        inputs = [directory.resolve() for directory in snapshot_dirs]
    except (OSError, ValueError, RuntimeError):
        raise StoryDraftError(StoryDraftErrorCode.DRAFTS_DIR_INVALID) from None
    if any(resolved.is_relative_to(directory) for directory in inputs):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_INSIDE_SNAPSHOT)


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise StoryDraftError(StoryDraftErrorCode.OUTPUT_CONFLICT) from None
    return True


def _is_real_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return False
    reparse = getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not reparse


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
