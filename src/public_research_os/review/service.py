"""The review desk behind `pros review`: which 06D drafts can be reviewed, what an edit becomes,
and how approval and rejection are recorded. No network, no model, no environment access.

What is reviewable. A story whose `<id>-link` exists under `--drafts`, re-verified on every read:
the story is OFFERED in the backlog with the evidence the link names; the brief and draft bundles
verify as COMPLETE; story-link.json equals the bytes 06D's `link_document` produces from them; the
draft's assertion map equals what the unchanged `check_draft` produces now from its brief and
response; and every review decision recorded for the story names this link and this 06D draft.
Anything else is listed as refused with a fixed reason and cannot be edited, approved or rejected.

What an edit is. The editorial checker validates segments against their declared basis and
citations, so arbitrary free text cannot be validated. An edit replaces the text of existing
segments only: ids, order, paragraph breaks, basis, citations and verification questions are
kept. The edited response goes through the unchanged schema (`parse_response`) and checker
(`check_draft`); saving publishes it as a new draft bundle through the unchanged `draft-build`
path (`prepare_build` re-verifies the snapshot, `build_from_response` writes) under
`<id>-review-<sha>`, and records a REVISED decision. A saved edit is UNAPPROVED, and it withdraws
any earlier approval for the story.

Citation scope. A quoted source reference is checked against the quoted span only; an unquoted
one against its whole cited range (the checker's unchanged semantics). An edit may also select
"use full cited range" for a quoted reference of the current draft: that reference's quote
becomes null and nothing else of it changes (path and lines stay). This is a revision like any
other, even when the text is identical: new response, unchanged checks and build, new bundle.

Approval and rejection. Approval loads the current draft bundle with the publisher's unchanged
`load_candidate` (COMPLETE, READY_FOR_REVIEW, no blocking finding), requires the same typed phrase
inside the service, and records the ledger approval and its APPROVED decision in one
transaction. Before that, the gate (`review.gate`) must pass: no sentence OUTDATED by a later
record of the same revision (from the supplied snapshots) and no CONTRADICTED or OUTDATED verdict
of the stored fact check on words it actually read. Rejection needs a typed phrase too and is final for the story. Approving sends
nothing; the scheduler refuses any approval a later decision withdrew.

Scheduling (06F) is a separate owner action on an APPROVED draft, and only when the caller passes
a provider factory (enabled by pros review; this package
never reads either gate). It re-verifies the item, rebuilds the candidate with the unchanged
`load_candidate` from the approved channel and time, and calls the unchanged `submit` once: its
approval, withdrawal, duplicate and send-intent rules apply as they do for the scheduler. The
intent `submit` persists is shown instead of the action, so nothing is ever offered or sent twice.

Stale drafts. A complete, unmodified bundle whose stored checks an older version of the editorial
rules wrote (`checked_under_other_rules`) is STALE, not refused: the page shows what the current
rules say, Approve and Edit are refused ("made under old rules; reject or regenerate"), and
Reject is allowed, so the owner's decision still becomes an eval case. Every other disagreement
between a bundle's parts is still DRAFT_INCONSISTENT.

Auto-repair. A story with a CLEAN repair record (`automation.repair`) is reviewed from its
repaired draft bundle instead of the 06D draft, with the record's re-checked verdicts; the
origin and its link are still verified as before. A SHELVED record makes the story unreviewable.
The queue offers only drafts with zero blocks: a blocked, undecided, non-stale draft is listed as
HELD_BACK, never offered for approval.

Eval cases. With `eval_cases` set, every recorded Approve, Edit and Reject also writes one case
file there (never overwritten): each sentence before and after, its evidence references, the
gate's blocks and warnings and the fact check's verdict, so `pros eval` grows from the owner's
real decisions. The decision is recorded first; a case that cannot be written is reported.

X (Step 6). With an X channel configured, the desk derives the X text of the draft under review
(`review.x_post`: whole opening sentences plus its scope line, at most 280 weighted characters,
the free mechanical checks re-run; or nothing, with the reason). The approval sheet shows it
beside the LinkedIn text, the approve form carries its digest, and one Approve records both
approvals in one transaction; a sheet whose X text no longer matches is refused (REVIEW_STALE).
Scheduling sends LinkedIn first; the X post goes in the same slot only after Buffer accepted the
LinkedIn post, and only while its approval stands (`submit(..., lead_approval_id=...)`). If the X
request is refused before it is sent, the same schedule action later sends only the X post.

A confirmation in a local page is a barrier against accidents, not authentication, and an
approval is the owner's decision to post, never a certification that the claims are true.
"""

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from public_research_os.automation.repair import (
    CLEAN,
    REPAIRED_INFIX,
    SHELVED,
    load_record,
)
from public_research_os.drafting.story_draft import LINK_CAP_BYTES, LINK_FILE, link_document
from public_research_os.drafting.story_draft import story_paths as _story_paths
from public_research_os.editorial.bundle import BundleIncomplete, verify_bundle
from public_research_os.editorial.errors import EditorialError
from public_research_os.editorial.grounding import (
    check_draft,
    checked_under_other_rules,
    render_post,
)
from public_research_os.editorial.inputs import parse_brief, parse_response
from public_research_os.editorial.models import (
    AssertionMap,
    Brief,
    BundleKind,
    BundleRecord,
    DraftResponse,
    GeneratorKind,
)
from public_research_os.editorial.service import build_from_response, prepare_build
from public_research_os.editorial.sources import VerifiedSnapshot, load_verified_snapshot
from public_research_os.editorial.supersession import Record, check_superseded
from public_research_os.evidence.report import canonical_json
from public_research_os.evidence.snapshot import read_input
from public_research_os.evidence.strict_json import StrictJsonError, loads_strict
from public_research_os.publication.approval import Approval, Candidate
from public_research_os.publication.errors import PublicationError, PublicationErrorCode
from public_research_os.publication.ledger import (
    LEAD_ACCEPTED,
    Intent,
    Ledger,
    ReviewDecision,
    ReviewKind,
    ReviewSubject,
)
from public_research_os.publication.port import SchedulingProvider
from public_research_os.publication.service import (
    SubmitResult,
    X,
    confirm,
    load_candidate,
    submit,
)
from public_research_os.review.gate import Gate, Note, judge
from public_research_os.review.x_post import XPost, x_post
from public_research_os.stories.backlog import Story, StoryBacklog, StoryState

REVISION_INFIX = "-review-"
STALE_MESSAGE = (
    "made under old rules; reject or regenerate: this draft's stored checks were written by an "
    "older version of the editorial rules, so it cannot be approved or edited"
)
CASE_VERSION = "1"
EDIT_LABEL = "Owner edit in pros review of an earlier draft"
_LINK_DIR = re.compile(r"([0-9a-f]{64})-link")
X_OFF = "no X channel is configured"
X_CONFLICT = "the same X text and time is already approved from another draft; choose another time"


class ReviewState(StrEnum):
    UNAPPROVED = "UNAPPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Refusal(StrEnum):
    """Why a linked draft is not reviewable. Each is a fixed code; nothing from a file is echoed."""

    LINK_UNREADABLE = "LINK_UNREADABLE"
    STORY_UNKNOWN = "STORY_UNKNOWN"
    STORY_NOT_OFFERED = "STORY_NOT_OFFERED"
    EVIDENCE_CHANGED = "EVIDENCE_CHANGED"
    BRIEF_INCOMPLETE = "BRIEF_INCOMPLETE"
    DRAFT_INCOMPLETE = "DRAFT_INCOMPLETE"
    DRAFT_INCONSISTENT = "DRAFT_INCONSISTENT"
    LINK_MISMATCH = "LINK_MISMATCH"
    DECISION_MISMATCH = "DECISION_MISMATCH"
    REVISION_INVALID = "REVISION_INVALID"
    SHELVED = "SHELVED"
    HELD_BACK = "HELD_BACK"
    REPAIR_INVALID = "REPAIR_INVALID"


REFUSALS: dict[Refusal, str] = {
    Refusal.LINK_UNREADABLE: "story-link.json is missing, not a single regular file, or not JSON",
    Refusal.STORY_UNKNOWN: "the backlog has no story with this id",
    Refusal.STORY_NOT_OFFERED: "the backlog does not record this story as offered to drafting",
    Refusal.EVIDENCE_CHANGED: (
        "the backlog recorded newer evidence for this story after the draft was made"
    ),
    Refusal.BRIEF_INCOMPLETE: "the brief bundle is not complete and unmodified",
    Refusal.DRAFT_INCOMPLETE: "the 06D draft bundle is not complete and unmodified",
    Refusal.DRAFT_INCONSISTENT: (
        "the draft's text, response, assertion map and record disagree, or the map is not what "
        "the editorial checker produces from them"
    ),
    Refusal.LINK_MISMATCH: "story-link.json does not match the backlog story and its bundles",
    Refusal.DECISION_MISMATCH: (
        "a recorded review decision names a different story link or 06D draft"
    ),
    Refusal.REVISION_INVALID: (
        "the draft revision named by the latest review decision is missing, modified or not "
        "built from this story's brief"
    ),
    Refusal.SHELVED: (
        "auto-repair could not make this draft clean; it is shelved and never offered for "
        "approval (the run's notification lists why)"
    ),
    Refusal.HELD_BACK: (
        "held back: the draft still has a block, so it is not offered for approval"
    ),
    Refusal.REPAIR_INVALID: (
        "the auto-repair record or its repaired draft is unreadable, modified or not built from "
        "this story's draft"
    ),
}


class ReviewErrorCode(StrEnum):
    DRAFTS_DIR_INVALID = "DRAFTS_DIR_INVALID"
    OUTPUT_INSIDE_SNAPSHOT = "OUTPUT_INSIDE_SNAPSHOT"
    STORY_NOT_FOUND = "STORY_NOT_FOUND"
    NOT_REVIEWABLE = "NOT_REVIEWABLE"
    NOT_EDITABLE = "NOT_EDITABLE"
    EDIT_INVALID = "EDIT_INVALID"
    EDIT_UNCHANGED = "EDIT_UNCHANGED"
    SCOPE_INVALID = "SCOPE_INVALID"
    EVIDENCE_SNAPSHOT_NOT_SUPPLIED = "EVIDENCE_SNAPSHOT_NOT_SUPPLIED"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    REJECT_NOT_CONFIRMED = "REJECT_NOT_CONFIRMED"
    SCHEDULING_DISABLED = "SCHEDULING_DISABLED"
    NOT_APPROVED = "NOT_APPROVED"
    SCHEDULE_NOT_CONFIRMED = "SCHEDULE_NOT_CONFIRMED"
    APPROVAL_BLOCKED = "APPROVAL_BLOCKED"
    STALE_DRAFT = "STALE_DRAFT"
    EVAL_CASE_NOT_SAVED = "EVAL_CASE_NOT_SAVED"


MESSAGES: dict[ReviewErrorCode, str] = {
    ReviewErrorCode.DRAFTS_DIR_INVALID: "--drafts must be an existing real directory",
    ReviewErrorCode.OUTPUT_INSIDE_SNAPSHOT: (
        "--drafts must not be a --snapshot directory or inside one"
    ),
    ReviewErrorCode.STORY_NOT_FOUND: "no linked draft with this story id exists under --drafts",
    ReviewErrorCode.NOT_REVIEWABLE: "this draft is refused for review; see the queue for why",
    ReviewErrorCode.NOT_EDITABLE: "this draft has no post segments to edit (NO_POST)",
    ReviewErrorCode.EDIT_INVALID: (
        "the edited text does not fit the draft response schema (one line per segment, no "
        "control or invisible characters, no outer whitespace, at most 700 characters)"
    ),
    ReviewErrorCode.EDIT_UNCHANGED: (
        "the edited text and citation scope are the current ones; nothing was saved"
    ),
    ReviewErrorCode.SCOPE_INVALID: (
        "a 'use full cited range' selection does not name a quoted source reference of the "
        "current draft"
    ),
    ReviewErrorCode.EVIDENCE_SNAPSHOT_NOT_SUPPLIED: (
        "The saved evidence is unavailable. Restore the matching snapshot in your configured snapshots directory before editing."
    ),
    ReviewErrorCode.REVISION_CONFLICT: (
        "a revision directory for this edit exists and is not the complete bundle of this edit"
    ),
    ReviewErrorCode.REJECT_NOT_CONFIRMED: "the rejection phrase was not typed exactly",
    ReviewErrorCode.SCHEDULING_DISABLED: (
        "Scheduling is off. Configure Buffer and restart shipyap review to enable scheduling."
    ),
    ReviewErrorCode.NOT_APPROVED: "only the current approval of an APPROVED draft can be scheduled",
    ReviewErrorCode.SCHEDULE_NOT_CONFIRMED: "the scheduling phrase was not typed exactly",
    ReviewErrorCode.APPROVAL_BLOCKED: (
        "approval is refused: a sentence is contradicted or outdated by the files, or fails a "
        "mechanical check; fix it with an edit or reject the draft"
    ),
    ReviewErrorCode.STALE_DRAFT: STALE_MESSAGE,
    ReviewErrorCode.EVAL_CASE_NOT_SAVED: (
        "the decision was recorded, but its eval case file could not be written"
    ),
}


class ReviewError(Exception):
    def __init__(self, code: ReviewErrorCode, details: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code
        self.details = details


class _Refused(Exception):
    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class Draft:
    """One verified draft bundle, parsed; every part re-checked against the others."""

    directory: Path
    record: BundleRecord
    record_sha256: str
    brief: Brief
    brief_json: bytes
    response: DraftResponse
    amap: AssertionMap  # what the current rules say (for a stale draft, not the stored map)
    text: str  # the post exactly as draft.txt holds it, without its terminating newline
    stale: bool = False  # the stored checks were written under older rules


@dataclass(frozen=True)
class ReviewItem:
    story: Story
    link: dict
    link_sha256: str
    origin: Draft  # the 06D draft bundle
    current: Draft  # the draft under review: the base or the latest saved revision
    decisions: tuple[ReviewDecision, ...]
    approval: Approval | None  # the current approval, when the state is APPROVED
    # The send intent of every approval this story's review named, in decision order, whichever
    # command made it. One for an approval that is no longer current means a request was made
    # before a later decision withdrew it locally.
    intents: tuple[Intent, ...] = ()
    repaired: Draft | None = None  # the auto-repaired draft offered instead of the origin
    repair: dict | None = None  # its repair record (CLEAN)

    @property
    def base(self) -> Draft:
        """What the pipeline offered: the auto-repaired draft, or the 06D draft."""
        return self.repaired or self.origin

    @property
    def intent(self) -> Intent | None:
        """The send intent of the current approval: if present, it was submitted already."""
        if self.approval is None:
            return None
        return next((i for i in self.intents if i.approval_id == self.approval.approval_id), None)

    @property
    def withdrawn_intents(self) -> tuple[Intent, ...]:
        current = self.approval.approval_id if self.approval else None
        return tuple(i for i in self.intents if i.approval_id != current)

    @property
    def state(self) -> ReviewState:
        if not self.decisions or self.decisions[-1].decision == ReviewKind.REVISED:
            return ReviewState.UNAPPROVED
        return ReviewState(self.decisions[-1].decision.value)

    @property
    def seq(self) -> int:
        """The current decision's seq (0: none). Actions carry it to refuse a stale page."""
        return self.decisions[-1].seq if self.decisions else 0

    @property
    def stale(self) -> bool:
        return self.current.stale

    @property
    def edited(self) -> bool:
        return self.current.directory != self.base.directory

    def subject(self) -> ReviewSubject:
        return ReviewSubject(
            story_id=self.story.story_id,
            draft_bundle=self.current.directory.name,
            draft_record_sha256=self.current.record_sha256,
            origin_record_sha256=self.origin.record_sha256,
            link_sha256=self.link_sha256,
        )


@dataclass(frozen=True)
class Unreviewable:
    story_id: str
    reason: Refusal


@dataclass(frozen=True)
class Preview:
    """An edit checked in memory; nothing written."""

    raw: bytes
    response: DraftResponse
    amap: AssertionMap
    text: str
    changed: bool
    gate: Gate | None = None  # what Approve would face after saving this edit


@dataclass(frozen=True)
class XState:
    """The X side of a story: the X text its current draft gives now (or why none), and the X
    approval and send intent recorded with its current LinkedIn approval."""

    post: XPost
    approval: Approval | None = None
    intent: Intent | None = None


def x_digest(post: XPost) -> str:
    """What the approve form carries, so Approve binds the X text the owner was shown."""
    return "none" if post.text is None else hashlib.sha256(post.text.encode("utf-8")).hexdigest()


def scope_key(segment_id: str, index: int) -> str:
    """Names one source reference of a segment (1-based) in a 'use full cited range' choice."""
    return f"{segment_id}.{index}"


def reject_phrase(story_id: str) -> str:
    return f"REJECT {story_id[:12]}"


def schedule_phrase(approval_id: str) -> str:
    return f"SCHEDULE {approval_id[:12]}"


class ReviewDesk:
    def __init__(
        self,
        drafts: Path,
        backlog: Path,
        ledger: Path,
        snapshots: tuple[Path, ...] = (),
        *,
        fact_checks: Callable[[str], dict | None] | None = None,
        eval_cases: Path | None = None,
        x_channel_id: str = "",
        demo_mode: bool = False,
    ) -> None:
        if not _is_real_directory(drafts):
            raise ReviewError(ReviewErrorCode.DRAFTS_DIR_INVALID)
        try:
            resolved = drafts.resolve()
            inputs = [directory.resolve() for directory in snapshots]
        except (OSError, ValueError, RuntimeError):
            raise ReviewError(ReviewErrorCode.DRAFTS_DIR_INVALID) from None
        if any(resolved.is_relative_to(directory) for directory in inputs):
            raise ReviewError(ReviewErrorCode.OUTPUT_INSIDE_SNAPSHOT)
        self.drafts = drafts
        self.backlog_path = backlog
        self.ledger_path = ledger
        self.snapshots = snapshots
        self.fact_checks = fact_checks  # story id -> the stored fact check document, if any
        self.eval_cases = eval_cases
        self.x_channel_id = x_channel_id  # "": X is off
        self._demo_mode = demo_mode
        self._verified: dict[Path, VerifiedSnapshot | None] = {}
        # Fail at start, not at the first click: both stores must open. The ledger may be new.
        StoryBacklog.open(backlog, create=False).close()
        Ledger.open(ledger, create=True).close()

    @property
    def demo_mode(self) -> bool:
        return self._demo_mode

    # --- reading ---------------------------------------------------------------------------

    def queue(self) -> tuple[list[ReviewItem], list[Unreviewable]]:
        try:
            names = sorted(entry.name for entry in os.scandir(self.drafts))
        except OSError:
            raise ReviewError(ReviewErrorCode.DRAFTS_DIR_INVALID) from None
        items, refused = [], []
        with self._stores() as (backlog, ledger):
            for name in names:
                match = _LINK_DIR.fullmatch(name)
                if match is None:
                    continue
                try:
                    items.append(self._load(match.group(1), backlog, ledger))
                except _Refused as exc:
                    refused.append(Unreviewable(match.group(1), exc.reason))
        # Only drafts with zero blocks are offered. One the owner already acted on stays listed,
        # and so does a stale one: it can never be approved, only rejected (an eval case).
        held = [
            item
            for item in items
            if not item.decisions
            and not item.stale
            and not self.gate(item).approvable
            and not any(n.code == "FACT_CHECK_REQUIRED" for n in self.gate(item).document_blocks)
        ]
        refused += [Unreviewable(item.story.story_id, Refusal.HELD_BACK) for item in held]
        items = [item for item in items if item not in held]
        order = {ReviewState.UNAPPROVED: 0, ReviewState.APPROVED: 1, ReviewState.REJECTED: 2}
        items.sort(key=lambda item: (order[item.state], item.story.offered_at or "", item.seq))
        return items, refused

    def item(self, story_id: str) -> ReviewItem:
        if not re.fullmatch(r"[0-9a-f]{64}", story_id) or not _exists(
            _story_paths(self.drafts, story_id).link
        ):
            raise ReviewError(ReviewErrorCode.STORY_NOT_FOUND)
        with self._stores() as (backlog, ledger):
            try:
                return self._load(story_id, backlog, ledger)
            except _Refused:
                raise ReviewError(ReviewErrorCode.NOT_REVIEWABLE) from None

    def preview(
        self, item: ReviewItem, texts: dict[str, str], full_range: frozenset[str] = frozenset()
    ) -> Preview:
        """Check edited segment texts, and any quoted references switched to their full cited
        range (`scope_key`s), with the unchanged schema and checker; write nothing."""
        current = item.current
        if not current.response.segments:
            raise ReviewError(ReviewErrorCode.NOT_EDITABLE)
        if set(texts) != {segment.id for segment in current.response.segments}:
            raise ReviewError(ReviewErrorCode.EDIT_INVALID)
        quoted = {
            scope_key(segment.id, index)
            for segment in current.response.segments
            for index, ref in enumerate(segment.source_refs, start=1)
            if ref.quote is not None
        }
        if not full_range <= quoted:
            raise ReviewError(ReviewErrorCode.SCOPE_INVALID)
        data = current.response.model_dump(mode="json")
        for segment in data["segments"]:
            segment["text"] = texts[segment["id"]]
            for index, ref in enumerate(segment["source_refs"], start=1):
                if scope_key(segment["id"], index) in full_range:
                    ref["quote"] = None  # path and lines are kept exactly
        data["generator"] = {"kind": GeneratorKind.HUMAN.value, "label": EDIT_LABEL}
        raw = canonical_json(data).encode("utf-8")
        try:
            response = parse_response(raw)
        except EditorialError as exc:
            raise ReviewError(ReviewErrorCode.EDIT_INVALID, exc.details) from None
        amap = check_draft(current.brief, response, current.record.brief_sha256, _sha256(raw))
        text = render_post(response.segments)
        return Preview(
            raw,
            response,
            amap,
            text,
            changed=text != current.text or bool(full_range),
            gate=self._judge(item, current.brief, response, amap),
        )

    def fact_check(self, item: ReviewItem) -> dict | None:
        """The stored fact check of the draft the pipeline offered (the words it read), if any:
        for an auto-repaired draft, the repair record's verdicts."""
        from public_research_os.review.required import completed_fact_check

        if item.repair is not None:
            check = item.repair.get("fact_check")
            if not isinstance(check, dict) or check.get("draft") != item.base.directory.name:
                return None
            if not self.demo_mode and check.get("label") == "SAMPLE fact check (scripted)":
                return None
            return check if completed_fact_check(check, item.base.response) else None
        if self.fact_checks is None:
            return None
        check = self.fact_checks(item.story.story_id)
        if not isinstance(check, dict) or check.get("draft") != item.origin.directory.name:
            return None
        if not self.demo_mode and check.get("label") == "SAMPLE fact check (scripted)":
            return None
        return check if completed_fact_check(check, item.base.response) else None

    def gate(self, item: ReviewItem) -> Gate:
        """What Approve faces for the saved draft, judged now."""
        current = item.current
        return self._judge(item, current.brief, current.response, current.amap)

    def records(self, brief: Brief) -> tuple[Record, ...]:
        """The files of every supplied snapshot of the brief's repository and revision (the
        supersession check keeps their later records)."""
        found = []
        for directory in self.snapshots:
            if directory not in self._verified:
                try:
                    self._verified[directory] = load_verified_snapshot(directory)
                except (EditorialError, OSError):
                    self._verified[directory] = None
            snapshot = self._verified[directory]
            if (
                snapshot is not None
                and snapshot.source_url == brief.snapshot.source_url
                and snapshot.revision_sha == brief.snapshot.revision_sha
            ):
                found += [Record(s.path, tuple(s.lines)) for s in snapshot.sources]
        return tuple(found)

    def x_post(self, item: ReviewItem) -> XPost:
        """The X text of the draft under review, judged now."""
        if not self.x_channel_id:
            return XPost(None, X_OFF)
        current = item.current
        return x_post(
            current.brief,
            current.response,
            self.records(current.brief),
            current.record.brief_sha256,
        )

    def x_sheet(self, story_id: str, candidate: Candidate) -> XPost:
        """What X gets if `candidate` is approved, for the sheet beside the LinkedIn text."""
        return self._follow_on(self.item(story_id), candidate)[0]

    def x_state(self, item: ReviewItem) -> XState:
        post = self.x_post(item)
        approval = item.approval
        if approval is None or post.text is None:
            return XState(post)
        x_id = self._x_candidate(post, approval).approval_id
        with Ledger.open(self.ledger_path, create=False) as ledger:
            x_approval = ledger.get_approval(x_id)
            intent = ledger.get_intent(x_id) if x_approval is not None else None
        if x_approval is not None and (
            x_approval.bundle_record_sha256 != approval.bundle_record_sha256
        ):
            x_approval, intent = None, None  # approved with another draft: not this one's
        return XState(post, x_approval, intent)

    # --- decisions -------------------------------------------------------------------------

    def save_edit(
        self,
        story_id: str,
        texts: dict[str, str],
        expected_seq: int,
        now: datetime,
        full_range: frozenset[str] = frozenset(),
    ) -> ReviewDecision:
        item = self._open_for_decision(story_id, expected_seq)
        if item.stale:
            raise ReviewError(ReviewErrorCode.STALE_DRAFT)
        preview = self.preview(item, texts, full_range)
        if not preview.changed:
            raise ReviewError(ReviewErrorCode.EDIT_UNCHANGED)
        out = self.drafts / f"{story_id}{REVISION_INFIX}{_sha256(preview.raw)[:16]}"
        if not _exists(out):
            snapshot_dir = self._snapshot(item.origin.record.snapshot_manifest_sha256)
            prepared = prepare_build(snapshot_dir, _story_paths(self.drafts, story_id).brief)
            if prepared.brief_sha256 != item.origin.record.brief_sha256:
                raise ReviewError(ReviewErrorCode.REVISION_CONFLICT)
            build_from_response(prepared, preview.raw, out)
        try:
            revision = _load_draft(out)
        except _Refused:
            raise ReviewError(ReviewErrorCode.REVISION_CONFLICT) from None
        if revision.record.response_sha256 != _sha256(preview.raw) or (
            revision.brief_json != item.origin.brief_json
        ):
            raise ReviewError(ReviewErrorCode.REVISION_CONFLICT)
        subject = ReviewSubject(
            story_id=story_id,
            draft_bundle=out.name,
            draft_record_sha256=revision.record_sha256,
            origin_record_sha256=item.origin.record_sha256,
            link_sha256=item.link_sha256,
        )
        with Ledger.open(self.ledger_path, create=False) as ledger:
            decision = ledger.add_review_decision(
                subject, ReviewKind.REVISED, revision.text, expected_seq, now
            )
        self._save_case(item, decision, item.current, revision)
        return decision

    def approval_candidate(
        self, story_id: str, channel_id: str, due_at: str, now: datetime
    ) -> Candidate:
        """The exact post an approval would bind, for the owner to read before typing."""
        item = self.item(story_id)
        if item.state == ReviewState.REJECTED:
            raise PublicationError(PublicationErrorCode.REVIEW_CLOSED)
        if item.stale:
            raise ReviewError(ReviewErrorCode.STALE_DRAFT)
        candidate = self._candidate(item, channel_id, due_at, now)
        self._require_gate(item)
        return candidate

    def approve(
        self,
        story_id: str,
        channel_id: str,
        due_at: str,
        typed: str,
        expected_seq: int,
        now: datetime,
        x_seen: str | None = None,
    ) -> tuple[Approval, ReviewDecision]:
        """`x_seen` is the `x_digest` of the X text on the sheet the owner approved from; with an
        X channel configured it must match the X text the draft gives now."""
        item = self.item(story_id)
        if item.stale:
            raise ReviewError(ReviewErrorCode.STALE_DRAFT)
        candidate = self._candidate(item, channel_id, due_at, now)
        self._require_gate(item)
        confirm(candidate, typed, now)
        post, follow_on = self._follow_on(item, candidate)
        if self.x_channel_id and x_seen != x_digest(post):
            raise PublicationError(PublicationErrorCode.REVIEW_STALE)
        with Ledger.open(self.ledger_path, create=False) as ledger:
            approval, decision = ledger.add_reviewed_approval(
                item.subject(), candidate, expected_seq, now, follow_on
            )
        self._save_case(item, decision, item.current, item.current)
        return approval, decision

    def reject(self, story_id: str, typed: str, expected_seq: int, now: datetime) -> ReviewDecision:
        item = self.item(story_id)
        if not hmac.compare_digest(typed.encode("utf-8"), reject_phrase(story_id).encode("utf-8")):
            raise ReviewError(ReviewErrorCode.REJECT_NOT_CONFIRMED)
        with Ledger.open(self.ledger_path, create=False) as ledger:
            decision = ledger.add_review_decision(
                item.subject(), ReviewKind.REJECTED, item.current.text, expected_seq, now
            )
        self._save_case(item, decision, item.current, item.current)
        return decision

    def schedule(
        self,
        story_id: str,
        approval_id: str,
        typed: str,
        expected_seq: int,
        provider: Callable[[], SchedulingProvider] | None,
        now: datetime,
    ) -> SubmitResult:
        """Submit the current approval through the unchanged publisher: at most one request.

        `provider` is None unless both scheduling gates were opened at start; then nothing is
        built or sent. The factory is called only after every local check here has passed.
        Refusals from `submit` (withdrawn, other bundle, duplicate, destination) propagate."""
        if self.demo_mode or provider is None:
            raise ReviewError(ReviewErrorCode.SCHEDULING_DISABLED)
        item = self.item(story_id)  # re-verified from the files and the ledger, now
        if item.seq != expected_seq:
            raise PublicationError(PublicationErrorCode.REVIEW_STALE)
        if item.state == ReviewState.REJECTED:
            raise PublicationError(PublicationErrorCode.REVIEW_CLOSED)
        approval = item.approval
        if item.state != ReviewState.APPROVED or approval is None:
            raise ReviewError(ReviewErrorCode.NOT_APPROVED)
        if approval.approval_id != approval_id:
            raise PublicationError(PublicationErrorCode.REVIEW_STALE)
        expected = schedule_phrase(approval.approval_id)
        if not hmac.compare_digest(typed.encode("utf-8"), expected.encode("utf-8")):
            raise ReviewError(ReviewErrorCode.SCHEDULE_NOT_CONFIRMED)
        # The exact approved channel and time, never values from the form.
        candidate = self._candidate(item, approval.channel_id, approval.due_at, now)
        x_state = self.x_state(item)
        with Ledger.open(self.ledger_path, create=False) as ledger:
            result = None
            if ledger.get_intent(approval.approval_id) is None or x_state.approval is None:
                # LinkedIn first (a duplicate is refused here, as before).
                result = submit(ledger, provider(), candidate, approval.approval_id, now)
            lead = ledger.get_intent(approval.approval_id)
            if (
                x_state.approval is None
                or ledger.get_intent(x_state.approval.approval_id) is not None
            ):
                if result is None:
                    raise PublicationError(PublicationErrorCode.DUPLICATE_SUBMISSION)
                return result
            if lead is None or lead.state not in LEAD_ACCEPTED:
                if result is None:
                    raise PublicationError(PublicationErrorCode.LEAD_NOT_ACCEPTED)
                return result  # Buffer did not accept LinkedIn: X gets nothing
            follow_on = self._x_candidate(x_state.post, approval)
            return submit(
                ledger,
                provider(),
                follow_on,
                x_state.approval.approval_id,
                now,
                service=X,
                lead_approval_id=approval.approval_id,
            )

    # --- internals -------------------------------------------------------------------------

    def _judge(
        self, item: ReviewItem, brief: Brief, response: DraftResponse, amap: AssertionMap
    ) -> Gate:
        superseded = check_superseded(brief, response, self.records(brief))
        check = self.fact_check(item)
        from public_research_os.review.required import completed_fact_check

        complete = completed_fact_check(check, item.base.response)
        gate = judge(amap, response, superseded, check if complete else None, item.base.response)
        if not complete:
            gate = replace(
                gate,
                document_blocks=(
                    Note("FACT_CHECK_REQUIRED", "Not fact-checked - cannot approve"),
                    *gate.document_blocks,
                ),
            )
        if item.stale and response == item.current.response:
            stale = Note("STALE", STALE_MESSAGE)
            gate = replace(gate, document_blocks=(stale, *gate.document_blocks))
        return gate

    def _require_gate(self, item: ReviewItem) -> None:
        gate = self.gate(item)
        if not gate.approvable:
            raise ReviewError(ReviewErrorCode.APPROVAL_BLOCKED, gate.reasons())

    def _save_case(
        self, item: ReviewItem, decision: ReviewDecision, before: Draft, after: Draft
    ) -> None:
        """One eval case for one recorded decision: what the owner saw (`before`, with its gate)
        and what they decided (`after`: the saved edit, or the same draft)."""
        if self.eval_cases is None:
            return
        gate = self._judge(item, before.brief, before.response, before.amap)
        notes = {s.segment_id: s for s in gate.segments}
        texts = {s.id: s.text for s in after.response.segments}
        case = {
            "case_version": CASE_VERSION,
            "decision": decision.decision.value,
            "seq": decision.seq,
            "story_id": item.story.story_id,
            "repo_id": item.story.repo_id,
            "revision_sha": before.brief.snapshot.revision_sha,
            "draft_before": before.directory.name,
            "draft_after": after.directory.name,
            "approvable_before": gate.approvable,
            "segments": [
                {
                    "id": segment.id,
                    "text": segment.text,
                    "owner_text": texts.get(segment.id, segment.text),
                    "edited": texts.get(segment.id, segment.text) != segment.text,
                    "evidence": [
                        {
                            "ref": f"{r.path}:{r.line_start}-{r.line_end}",
                            "quote": r.quote,
                        }
                        for r in segment.source_refs
                    ]
                    + [{"ref": ref, "quote": None} for ref in segment.author_refs],
                    "verdict": notes[segment.id].verdict,
                    "blocks": [n.line() for n in notes[segment.id].blocks],
                    "warnings": [n.line() for n in notes[segment.id].warnings],
                }
                for segment in before.response.segments
            ],
        }
        name = f"{item.story.story_id[:16]}-{decision.seq:04d}-{decision.decision.value}.json"
        try:
            self.eval_cases.mkdir(parents=True, exist_ok=True)
            with open(self.eval_cases / name, "x", encoding="utf-8") as handle:
                json.dump(case, handle, indent=2, ensure_ascii=False)
        except OSError:
            raise ReviewError(ReviewErrorCode.EVAL_CASE_NOT_SAVED) from None

    def _candidate(
        self, item: ReviewItem, channel_id: str, due_at: str, now: datetime
    ) -> Candidate:
        # The publisher's own loader: COMPLETE, READY_FOR_REVIEW, no blocking finding.
        candidate = load_candidate(item.current.directory, channel_id, due_at, now)
        if candidate.bundle_record_sha256 != item.current.record_sha256:
            raise PublicationError(PublicationErrorCode.REVIEW_STALE)  # changed while loading
        return candidate

    def _x_candidate(self, post: XPost, lead: Candidate | Approval) -> Candidate:
        assert post.text is not None  # callers check
        return Candidate(
            text=post.text,
            channel_id=self.x_channel_id,
            due_at=lead.due_at,
            workspace_id=lead.workspace_id,
            bundle_record_sha256=lead.bundle_record_sha256,
        )

    def _follow_on(self, item: ReviewItem, candidate: Candidate) -> tuple[XPost, Candidate | None]:
        """The X text and the X candidate an approval of `candidate` records with it."""
        post = self.x_post(item)
        if post.text is None:
            return post, None
        follow_on = self._x_candidate(post, candidate)
        with Ledger.open(self.ledger_path, create=False) as ledger:
            existing = ledger.get_approval(follow_on.approval_id)
        if existing is not None and (
            existing.bundle_record_sha256 != follow_on.bundle_record_sha256
        ):
            return XPost(None, X_CONFLICT), None
        return post, follow_on

    def _open_for_decision(self, story_id: str, expected_seq: int) -> ReviewItem:
        item = self.item(story_id)
        if item.state == ReviewState.REJECTED:
            raise PublicationError(PublicationErrorCode.REVIEW_CLOSED)
        if item.seq != expected_seq:
            raise PublicationError(PublicationErrorCode.REVIEW_STALE)
        return item

    def _snapshot(self, manifest_sha256: str) -> Path:
        for directory in self.snapshots:
            if load_verified_snapshot(directory).manifest_sha256 == manifest_sha256:
                return directory
        raise ReviewError(ReviewErrorCode.EVIDENCE_SNAPSHOT_NOT_SUPPLIED)

    def _stores(self) -> "_Stores":
        return _Stores(self.backlog_path, self.ledger_path)

    def _load(self, story_id: str, backlog: StoryBacklog, ledger: Ledger) -> ReviewItem:
        paths = _story_paths(self.drafts, story_id)
        link_bytes = _read_link(paths.link)
        try:
            link = loads_strict(link_bytes)
        except StrictJsonError:
            raise _Refused(Refusal.LINK_UNREADABLE) from None
        if not isinstance(link, dict):
            raise _Refused(Refusal.LINK_UNREADABLE)
        story = backlog.get(story_id)
        if story is None:
            raise _Refused(Refusal.STORY_UNKNOWN)
        if story.state != StoryState.OFFERED:
            raise _Refused(Refusal.STORY_NOT_OFFERED)
        if link.get("evidence_id") != story.latest.evidence_id:
            raise _Refused(Refusal.EVIDENCE_CHANGED)
        try:
            brief_bundle = verify_bundle(paths.brief, BundleKind.BRIEF)
        except BundleIncomplete:
            raise _Refused(Refusal.BRIEF_INCOMPLETE) from None
        origin = _load_draft(paths.draft, Refusal.DRAFT_INCOMPLETE)
        if origin.brief_json != brief_bundle.files["brief.json"] or link_bytes != link_document(
            story, paths, origin.record_sha256, origin.record
        ):
            raise _Refused(Refusal.LINK_MISMATCH)
        link_sha256 = _sha256(link_bytes)
        decisions = tuple(ledger.review_decisions(story_id))
        if any(
            d.origin_record_sha256 != origin.record_sha256 or d.link_sha256 != link_sha256
            for d in decisions
        ):
            raise _Refused(Refusal.DECISION_MISMATCH)
        repaired, repair = self._repaired(story_id, origin)
        current = repaired or origin
        if decisions:
            current = self._revision(story_id, decisions[-1], origin, repaired)
        approval = None
        if decisions and decisions[-1].decision == ReviewKind.APPROVED:
            approval = ledger.get_approval(decisions[-1].approval_id or "")
            if approval is None or approval.bundle_record_sha256 != current.record_sha256:
                raise _Refused(Refusal.DECISION_MISMATCH)
        named = dict.fromkeys(d.approval_id for d in decisions if d.approval_id)
        intents = tuple(i for i in map(ledger.get_intent, named) if i is not None)
        return ReviewItem(
            story,
            link,
            link_sha256,
            origin,
            current,
            decisions,
            approval,
            intents,
            repaired,
            repair,
        )

    def _repaired(self, story_id: str, origin: Draft) -> tuple[Draft | None, dict | None]:
        """The CLEAN auto-repaired draft and its record; (None, None) without a record."""
        try:
            record = load_record(self.drafts, story_id)
        except (OSError, ValueError, UnicodeDecodeError):
            raise _Refused(Refusal.REPAIR_INVALID) from None
        if record is None:
            return None, None
        if record.get("origin_draft") != origin.directory.name:
            raise _Refused(Refusal.REPAIR_INVALID)
        if record.get("status") == SHELVED:
            raise _Refused(Refusal.SHELVED)
        final = record.get("final_draft")
        if record.get("status") != CLEAN or not isinstance(final, str):
            raise _Refused(Refusal.REPAIR_INVALID)
        if not re.fullmatch(rf"{story_id}{REPAIRED_INFIX}[0-9a-f]{{16}}", final):
            raise _Refused(Refusal.REPAIR_INVALID)
        draft = _load_draft(self.drafts / final, Refusal.REPAIR_INVALID)
        if draft.brief_json != origin.brief_json:
            raise _Refused(Refusal.REPAIR_INVALID)
        return draft, record

    def _revision(
        self, story_id: str, decision: ReviewDecision, origin: Draft, repaired: Draft | None = None
    ) -> Draft:
        """The draft the latest decision names. Never falls back to another draft."""
        name = decision.draft_bundle
        if name == origin.directory.name:
            draft = origin
        elif repaired is not None and name == repaired.directory.name:
            draft = repaired
        elif re.fullmatch(rf"{story_id}{REVISION_INFIX}[0-9a-f]{{16}}", name):
            draft = _load_draft(self.drafts / name, Refusal.REVISION_INVALID)
        else:
            raise _Refused(Refusal.REVISION_INVALID)
        if draft.record_sha256 != decision.draft_record_sha256 or (
            draft.brief_json != origin.brief_json
        ):
            raise _Refused(Refusal.REVISION_INVALID)
        return draft


class _Stores:
    """The backlog and the ledger, opened for one read and closed after it."""

    def __init__(self, backlog: Path, ledger: Path) -> None:
        self._paths = (backlog, ledger)
        self._open: list[StoryBacklog | Ledger] = []

    def __enter__(self) -> tuple[StoryBacklog, Ledger]:
        backlog = StoryBacklog.open(self._paths[0], create=False)
        try:
            ledger = Ledger.open(self._paths[1], create=False)
        except BaseException:
            backlog.close()
            raise
        self._open = [backlog, ledger]
        return backlog, ledger

    def __exit__(self, *_exc: object) -> None:
        for store in self._open:
            store.close()


def _load_draft(directory: Path, refusal: Refusal = Refusal.DRAFT_INCOMPLETE) -> Draft:
    try:
        bundle = verify_bundle(directory, BundleKind.DRAFT)
    except BundleIncomplete:
        raise _Refused(refusal) from None
    files, record = bundle.files, bundle.record
    inconsistent = Refusal.DRAFT_INCONSISTENT if refusal == Refusal.DRAFT_INCOMPLETE else refusal
    try:
        brief = parse_brief(files["brief.json"])
        response = parse_response(files["response.json"])
        amap = AssertionMap.model_validate(loads_strict(files["assertion-map.json"]))
        raw = files["draft.txt"].decode("utf-8")
    except (EditorialError, ValidationError, StrictJsonError, ValueError):
        raise _Refused(inconsistent) from None
    brief_sha256 = _sha256(files["brief.json"])
    response_sha256 = _sha256(files["response.json"])
    post = render_post(response.segments)
    if (
        record.brief_sha256 != brief_sha256
        or record.response_sha256 != response_sha256
        or record.draft_status != amap.draft_status
        or raw != (post + "\n" if post else "")
    ):
        raise _Refused(inconsistent)
    # The map shown to the owner is what the checker says now. A stored map that an older version
    # of the rules wrote for these exact inputs is stale; any other difference is inconsistent.
    current = check_draft(brief, response, brief_sha256, response_sha256)
    stale = current != amap
    if stale and not checked_under_other_rules(amap, current):
        raise _Refused(inconsistent)
    return Draft(
        directory=directory,
        record=record,
        record_sha256=bundle.record_sha256,
        brief=brief,
        brief_json=files["brief.json"],
        response=response,
        amap=current,
        text=post,
        stale=stale,
    )


def _read_link(directory: Path) -> bytes:
    """story-link.json: a real directory holding exactly that one regular file."""
    if not _is_real_directory(directory):
        raise _Refused(Refusal.LINK_UNREADABLE)
    try:
        names = {entry.name for entry in os.scandir(directory)}
    except OSError:
        raise _Refused(Refusal.LINK_UNREADABLE) from None
    data = read_input(directory / LINK_FILE, LINK_CAP_BYTES)
    if names != {LINK_FILE} or not isinstance(data, bytes):
        raise _Refused(Refusal.LINK_UNREADABLE)
    return data


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except (OSError, ValueError):
        return False
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
