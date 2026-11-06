"""`review-serve` (Assignment 06E): local owner review of 06D LinkedIn drafts.

Offline throughout. The 06D bundles are made by the real `story-draft` service with `FakeDrafter`;
the publisher is the real `submit` over the scripted Buffer transport; the conftest socket guard
stays on, and the HTTP tests connect only to the review server on 127.0.0.1 through a socket
opened with the connect function saved before the guard is installed. All content is synthetic.
"""

import ast
import http.client
import json
import shutil
import socket
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode

import pytest
from drafting_fake import FakeDrafter, model_text, reply
from editorial_fake import seg
from publication_fake import (
    CHANNEL,
    DUE,
    NOW,
    POST_ID,
    FakeTransport,
    channel_reply,
    create_error,
    create_ok,
    post_reply,
    timeout,
)
from stories_fake import (
    HISTORIC,
    HISTORIC_FILES,
    historic_finding,
    make_snapshot,
    ref,
    write_json,
)
from stories_fake import findings as findings_document

from public_research_os.drafting import provider
from public_research_os.drafting.policy import DEFAULT_MODEL
from public_research_os.drafting.story_draft import plan_story_draft, run_story_draft
from public_research_os.editorial.bundle import verify_bundle
from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.models import BundleKind, DraftStatus, FindingCode, RefCheck
from public_research_os.editorial.service import build_from_response, prepare_build
from public_research_os.evidence.report import canonical_json
from public_research_os.ingestion.assemble import SNAPSHOT_DIR
from public_research_os.preferences.repositories import RepositoryPreferences
from public_research_os.publication import buffer
from public_research_os.publication import ledger as ledger_module
from public_research_os.publication.approval import Candidate, confirmation_phrase
from public_research_os.publication.buffer import (
    BufferPublisher,
    TransportFailure,
    TransportReply,
)
from public_research_os.publication.errors import PublicationError, PublicationErrorCode
from public_research_os.publication.ledger import IntentState, Ledger, ReviewKind
from public_research_os.publication.service import approve, load_candidate, reconcile, submit
from public_research_os.review import server as server_module
from public_research_os.review import service as review_service
from public_research_os.review.pages import (
    INTENT_GUIDANCE,
    NOT_CANCELLED,
    STOP_MESSAGE,
    ApprovalDefaults,
    StoryView,
    story_page,
)
from public_research_os.review.server import HOST, ReviewServer
from public_research_os.review.service import (
    REVISION_INFIX,
    Refusal,
    ReviewDesk,
    ReviewError,
    ReviewErrorCode,
    ReviewState,
    XState,
    reject_phrase,
    schedule_phrase,
    x_digest,
)
from public_research_os.review.x_post import XPost
from public_research_os.stories.discovery import run_discovery

E = PublicationErrorCode
R = ReviewErrorCode
_CONNECT = socket.socket.connect  # saved before the conftest guard replaces it
SRC = Path(__file__).resolve().parents[1] / "src" / "public_research_os"
REVIEW = SRC / "review"

SEGMENTS = (
    seg(
        "S01",
        "A screenshot proves capture, not that the right scene was shown.",
        "SOURCE",
        src=[ref("README.md", 3, 3, "proves capture, not that the right scene was shown")],
    ),
    seg(
        "S02",
        "The README reports the scene confirmed for 3/10 synthetic artifacts.",
        "SOURCE",
        src=[ref("README.md", 4, 4, "The scene was confirmed for 3/10 synthetic artifacts.")],
    ),
    seg(
        "S03",
        "The angle here is that capture alone does not show the right scene.",
        "AUTHOR",
        auth=["insight"],
        brk=True,
    ),
)
EDITED_S01 = "A screenshot shows capture, not that the right scene was shown."
UNSUPPORTED_S02 = "The README reports the scene confirmed for 9/10 artifacts, a 40% gain."

# The first product rehearsal: the cited line holds the numbers, its quote does not.
COUNTED_FILES = {
    "README.md": HISTORIC_FILES["README.md"]
    + b"Across 26 days, 394 of 453 captures showed the intended scene.\n"  # 6
}
COUNTED_S02 = "Across 26 days, 394 of 453 captures showed the intended scene."
COUNTED_SEGMENTS = (
    SEGMENTS[0],
    seg("S02", COUNTED_S02, "SOURCE", src=[ref("README.md", 6, 6, "showed the intended scene")]),
    SEGMENTS[2],
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Review never reaches Buffer or a model: any attempt fails the test."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("review touched Buffer or the model provider")

    monkeypatch.setattr(buffer, "HttpsBufferTransport", forbidden)
    monkeypatch.setattr(buffer, "load_api_key", forbidden)
    monkeypatch.setattr(provider, "AnthropicDrafter", forbidden)


class Work:
    """One 06D draft made by the real story-draft service, and the stores review needs."""

    def __init__(
        self, root: Path, segments=SEGMENTS, files: dict[str, bytes] | None = None
    ) -> None:
        self.root = root
        self.preferences = root / "preferences.sqlite"
        self.backlog = root / "backlog.sqlite"
        self.ledger = root / "ledger.sqlite"
        self.drafts = root / "drafts"
        self.drafts.mkdir()
        self.snapshot = make_snapshot(root, "historic", files=files)
        with RepositoryPreferences.open(self.preferences, create=True) as store:
            store.set_mode(HISTORIC, "focus", NOW)
        found = write_json(
            root / "findings.json", findings_document(self.snapshot, [historic_finding()])
        )
        run_discovery(self.preferences, self.backlog, [self.snapshot], [found], NOW)
        plan = plan_story_draft(
            self.preferences,
            self.backlog,
            [self.snapshot],
            self.drafts,
            model=DEFAULT_MODEL,
            budget="5.00",
        )
        run_story_draft(plan, FakeDrafter(reply(model_text(segments))), self.backlog, NOW)
        self.sid = plan.story.story_id

    def desk(self, snapshots: bool = True) -> ReviewDesk:
        def check(_story_id):
            response = json.loads(
                (self.path("draft") / "response.json").read_text(encoding="utf-8")
            )
            return {
                "draft": self.path("draft").name,
                "verdicts": [
                    {
                        "segment_id": segment["id"],
                        "verdict": "SUPPORTED",
                        "reason": "fixture",
                        "refs": [],
                    }
                    for segment in response["segments"]
                ],
            }

        return ReviewDesk(
            self.drafts,
            self.backlog,
            self.ledger,
            (self.snapshot,) if snapshots else (),
            fact_checks=check,
        )

    def path(self, kind: str) -> Path:
        return self.drafts / f"{self.sid}-{kind}"


@pytest.fixture
def work(tmp_path):
    return Work(tmp_path)


def texts(**changes: str) -> dict[str, str]:
    base = {segment["id"]: segment["text"] for segment in SEGMENTS}
    base.update(changes)
    return base


def refused(call, *args, **kwargs):
    with pytest.raises((PublicationError, ReviewError)) as raised:
        call(*args, **kwargs)
    return raised.value.code


def approve_current(desk: ReviewDesk, sid: str, due: str = DUE, seq: int | None = None):
    item = desk.item(sid)
    candidate = desk.approval_candidate(sid, CHANNEL, due, NOW)
    typed = confirmation_phrase(candidate.approval_id)
    return desk.approve(sid, CHANNEL, due, typed, item.seq if seq is None else seq, NOW)


def schedule(work: Work, approval_id: str, bundle: Path, due: str = DUE):
    """The publisher, exactly as publish-schedule runs it, over the scripted transport."""
    transport = FakeTransport(channel_reply(), create_ok())
    candidate = load_candidate(bundle, CHANNEL, due, NOW)
    with Ledger.open(work.ledger, create=False) as ledger:
        result = submit(ledger, BufferPublisher(transport), candidate, approval_id, NOW)
    return result, transport


def schedule_refused(work: Work, approval_id: str, bundle: Path) -> PublicationErrorCode:
    transport = FakeTransport()  # any request would fail the test
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    with Ledger.open(work.ledger, create=False) as ledger:
        code = refused(submit, ledger, BufferPublisher(transport), candidate, approval_id, NOW)
        assert ledger.get_intent(approval_id) is None
    assert transport.bodies == []
    return code


# --- the queue and the evidence --------------------------------------------------------------


def test_a_valid_06d_draft_appears_in_the_review_queue_as_unapproved(work):
    items, refusals = work.desk().queue()
    assert refusals == []
    (item,) = items
    assert item.story.story_id == work.sid
    assert item.state == ReviewState.UNAPPROVED and item.decisions == ()
    assert item.current.amap.draft_status == DraftStatus.READY_FOR_REVIEW
    assert item.current.directory == work.path("draft") and not item.edited
    expected = (work.path("draft") / "draft.txt").read_text(encoding="utf-8")[:-1]
    assert item.current.text == expected


def test_the_approval_form_is_prefilled_with_the_saved_channel_and_next_slot(work):
    item = work.desk().item(work.sid)
    defaults = ApprovalDefaults("0123456789abcdef01234567", "2026-11-02T04:00:00Z", "Mon 2 Nov")
    page = story_page(item, "token", StoryView(), None, defaults=defaults)
    assert 'value="0123456789abcdef01234567"' in page or "value=0123456789abcdef01234567" in page
    assert "2026-11-02T04:00:00Z" in page and "Mon 2 Nov" in page
    plain = story_page(item, "token", StoryView(), None)
    assert "Pre-filled" not in plain and "0123456789abcdef01234567" not in plain


def test_the_evidence_shown_is_the_link_and_the_checked_citations(work):
    item = work.desk().item(work.sid)
    assert item.link["review_state"] == "UNAPPROVED"
    assert item.link["semantic_support"] == "UNKNOWN"
    assert [r["path"] for r in item.link["refs"]] == ["README.md"]
    located = [ref.ref_check.value for s in item.current.amap.segments for ref in s.source_refs]
    assert located == ["QUOTE_LOCATED", "QUOTE_LOCATED"]


def test_a_queue_without_link_outputs_is_empty_and_other_entries_are_ignored(tmp_path):
    work = Work(tmp_path)
    for kind in ("link", "draft", "brief", "attempt", "response"):
        shutil.rmtree(work.path(kind))
    (work.drafts / "notes").mkdir()
    assert work.desk().queue() == ([], [])


# --- edit and revalidate ---------------------------------------------------------------------


def test_revalidating_an_edit_shows_the_new_text_and_writes_nothing(work):
    desk = work.desk()
    before = sorted(p.name for p in work.drafts.iterdir())
    preview = desk.preview(desk.item(work.sid), texts(S01=EDITED_S01))
    assert preview.changed and preview.text.startswith(EDITED_S01)
    assert preview.amap.draft_status == DraftStatus.READY_FOR_REVIEW
    assert sorted(p.name for p in work.drafts.iterdir()) == before
    with Ledger.open(work.ledger, create=False) as ledger:
        assert ledger.review_decisions(work.sid) == []


def test_saving_an_edit_publishes_a_checked_revision_and_stays_unapproved(work):
    desk = work.desk()
    decision = desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)
    assert decision.decision == ReviewKind.REVISED and decision.approval_id is None
    revision = work.drafts / decision.draft_bundle
    assert revision.name.startswith(f"{work.sid}{REVISION_INFIX}")
    bundle = verify_bundle(revision, BundleKind.DRAFT)
    assert bundle.files["draft.txt"].decode("utf-8") == decision.text + "\n"
    assert json.loads(bundle.files["response.json"])["generator"]["kind"] == "human"
    item = desk.item(work.sid)
    assert item.state == ReviewState.UNAPPROVED and item.edited
    assert item.current.text.startswith(EDITED_S01)
    # The 06D draft and its link are untouched.
    verify_bundle(work.path("draft"), BundleKind.DRAFT)
    assert item.origin.directory == work.path("draft")


def test_an_unsupported_edit_is_flagged_and_cannot_be_approved(work):
    desk = work.desk()
    preview = desk.preview(desk.item(work.sid), texts(S02=UNSUPPORTED_S02))
    assert preview.amap.draft_status == DraftStatus.NEEDS_REVISION
    codes = {f.code for s in preview.amap.segments for f in s.findings}
    assert FindingCode.NUMBER_NOT_IN_EVIDENCE in codes
    desk.save_edit(work.sid, texts(S02=UNSUPPORTED_S02), 0, NOW)
    assert desk.item(work.sid).current.amap.draft_status == DraftStatus.NEEDS_REVISION
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == E.BUNDLE_NOT_READY
    assert refused(desk.approve, work.sid, CHANNEL, DUE, "APPROVE x", 1, NOW) == (
        E.BUNDLE_NOT_READY
    )
    with Ledger.open(work.ledger, create=False) as ledger:
        assert [d.decision for d in ledger.review_decisions(work.sid)] == [ReviewKind.REVISED]


@pytest.mark.parametrize(
    "change",
    [
        {"S01": "Two lines\nin one segment."},
        {"S01": " leading space"},
        {"S01": ""},
        {"S01": "x" * 701},
        {"S01": "hidden‮text"},
        {"S99": "an unknown segment"},
    ],
    ids=["newline", "outer_space", "empty", "too_long", "bidi", "unknown_segment"],
)
def test_an_edit_outside_the_response_schema_is_refused_and_nothing_is_saved(work, change):
    desk = work.desk()
    edited = {**texts(), **change}
    assert refused(desk.save_edit, work.sid, edited, 0, NOW) == R.EDIT_INVALID
    assert not any(REVISION_INFIX in p.name for p in work.drafts.iterdir())


def test_an_unchanged_edit_is_not_saved(work):
    assert refused(work.desk().save_edit, work.sid, texts(), 0, NOW) == R.EDIT_UNCHANGED


def test_saving_needs_the_story_snapshot_and_writes_nothing_without_it(work):
    desk = work.desk(snapshots=False)
    code = refused(desk.save_edit, work.sid, texts(S01=EDITED_S01), 0, NOW)
    assert code == R.EVIDENCE_SNAPSHOT_NOT_SUPPLIED
    assert not any(REVISION_INFIX in p.name for p in work.drafts.iterdir())


# --- citation scope --------------------------------------------------------------------------


@pytest.fixture
def counted(tmp_path):
    return Work(tmp_path, COUNTED_SEGMENTS, COUNTED_FILES)


def counted_texts(**changes: str) -> dict[str, str]:
    return {**texts(S02=COUNTED_S02), **changes}


def s02(amap):
    return next(s for s in amap.segments if s.id == "S02")


def revisions(work: Work) -> list[Path]:
    return [p for p in work.drafts.iterdir() if REVISION_INFIX in p.name]


def test_a_quote_without_the_numbers_blocks_them_although_the_cited_line_has_them(counted):
    item = counted.desk().item(counted.sid)
    segment = s02(item.current.amap)
    (checked,) = segment.source_refs
    assert (checked.line_start, checked.line_end) == (6, 6)
    assert checked.ref_check == RefCheck.QUOTE_LOCATED
    assert "453" in checked.excerpt[0] and "453" not in checked.quote
    (finding,) = [f for f in segment.findings if f.code == FindingCode.NUMBER_NOT_IN_EVIDENCE]
    assert finding.detail == "26, 394, 453"
    assert item.current.amap.draft_status == DraftStatus.NEEDS_REVISION
    assert refused(counted.desk().approval_candidate, counted.sid, CHANNEL, DUE, NOW) == (
        E.BUNDLE_NOT_READY
    )


def test_using_the_full_cited_range_locates_its_numbers_and_keeps_the_citation(counted):
    desk = counted.desk()
    link = (counted.path("link") / "story-link.json").read_bytes()
    origin = desk.item(counted.sid).current
    preview = desk.preview(desk.item(counted.sid), counted_texts(), frozenset({"S02.1"}))
    assert preview.changed and preview.text == origin.text
    assert preview.amap.draft_status == DraftStatus.READY_FOR_REVIEW
    segment = s02(preview.amap)
    assert [(n.token, n.located) for n in segment.numbers] == [
        ("26", True),
        ("394", True),
        ("453", True),
    ]
    assert segment.source_refs[0].ref_check == RefCheck.RANGE_CITED
    assert revisions(counted) == []  # revalidating writes nothing

    decision = desk.save_edit(counted.sid, counted_texts(), 0, NOW, frozenset({"S02.1"}))
    assert decision.decision == ReviewKind.REVISED and decision.text == origin.text
    revision = verify_bundle(counted.drafts / decision.draft_bundle, BundleKind.DRAFT)
    refs = json.loads(revision.files["response.json"])["segments"][1]["source_refs"]
    assert refs == [{"path": "README.md", "line_start": 6, "line_end": 6, "quote": None}]
    # The other quoted reference keeps its quote.
    kept = json.loads(revision.files["response.json"])["segments"][0]["source_refs"][0]
    assert kept["quote"] == "proves capture, not that the right scene was shown"
    item = desk.item(counted.sid)
    assert item.state == ReviewState.UNAPPROVED and item.edited
    assert item.current.amap.draft_status == DraftStatus.READY_FOR_REVIEW
    # The 06D draft, its link and the snapshot are untouched.
    assert (counted.path("link") / "story-link.json").read_bytes() == link
    assert verify_bundle(counted.path("draft"), BundleKind.DRAFT).record == origin.record
    assert desk.item(counted.sid).origin.amap.draft_status == DraftStatus.NEEDS_REVISION


def test_numbers_absent_from_the_full_cited_range_stay_blocked(counted):
    desk = counted.desk()
    wrong = "Across 27 days, 394 of 453 captures showed the intended scene."
    preview = desk.preview(desk.item(counted.sid), counted_texts(S02=wrong), frozenset({"S02.1"}))
    (finding,) = [
        f for f in s02(preview.amap).findings if f.code == FindingCode.NUMBER_NOT_IN_EVIDENCE
    ]
    assert finding.detail == "27"
    assert preview.amap.draft_status == DraftStatus.NEEDS_REVISION
    desk.save_edit(counted.sid, counted_texts(S02=wrong), 0, NOW, frozenset({"S02.1"}))
    assert refused(desk.approval_candidate, counted.sid, CHANNEL, DUE, NOW) == E.BUNDLE_NOT_READY


def test_a_citation_only_change_is_a_new_revision_that_withdraws_the_approval(work):
    desk = work.desk()
    approval, approved = approve_current(desk, work.sid)
    decision = desk.save_edit(work.sid, texts(), approved.seq, NOW, frozenset({"S01.1"}))
    assert decision.text == approval.text and decision.draft_bundle != approved.draft_bundle
    item = desk.item(work.sid)
    assert item.state == ReviewState.UNAPPROVED and item.approval is None
    assert item.current.record_sha256 != approval.bundle_record_sha256
    revision = work.drafts / decision.draft_bundle
    # Neither the publisher nor the review desk lets the earlier approval schedule anything.
    for bundle in (work.path("draft"), revision):
        assert schedule_refused(work, approval.approval_id, bundle) == E.APPROVAL_WITHDRAWN
    publisher = ScriptedPublisher()
    assert (
        refused(schedule_here, desk, work.sid, publisher, approval_id=approval.approval_id)
        == R.NOT_APPROVED
    )
    assert publisher.built == 0 and publisher.transport.bodies == []
    # The same text at the same time stays bound to the bundle it was approved from.
    assert refused(approve_current, desk, work.sid) == E.APPROVAL_CONFLICT
    # The same choice again changes nothing and is not saved.
    later = desk.item(work.sid)
    assert refused(desk.save_edit, work.sid, texts(), later.seq, NOW, frozenset({"S01.1"})) == (
        R.SCOPE_INVALID
    )
    assert refused(desk.save_edit, work.sid, texts(), later.seq, NOW) == R.EDIT_UNCHANGED


@pytest.mark.parametrize(
    "key",
    ["S99.1", "S01.2", "S01.0", "S03.1", "S01", ""],
    ids=["unknown_segment", "no_such_ref", "zero_index", "uncited_segment", "no_index", "empty"],
)
def test_a_scope_choice_that_names_no_quoted_reference_is_refused(work, key):
    desk = work.desk()
    code = refused(desk.save_edit, work.sid, texts(), 0, NOW, frozenset({key}))
    assert code == R.SCOPE_INVALID
    assert refused(desk.preview, desk.item(work.sid), texts(), frozenset({key})) == R.SCOPE_INVALID
    assert revisions(work) == []


def test_a_scope_choice_from_a_stale_page_or_on_a_tampered_snapshot_is_refused(counted):
    desk = counted.desk()
    full = frozenset({"S02.1"})
    desk.save_edit(counted.sid, counted_texts(S01=EDITED_S01), 0, NOW)  # another tab saved
    assert refused(desk.save_edit, counted.sid, counted_texts(), 0, NOW, full) == E.REVIEW_STALE
    seq = desk.item(counted.sid).seq
    # The pinned snapshot no longer matches its manifest: nothing is built or recorded.
    source = counted.snapshot / SNAPSHOT_DIR / "README.md"
    source.write_bytes(source.read_bytes().replace(b"26 days", b"27 days"))
    with pytest.raises(EditorialError) as raised:
        desk.save_edit(counted.sid, counted_texts(S01=EDITED_S01), seq, NOW, full)
    assert raised.value.code == EditorialErrorCode.SNAPSHOT_INCOMPLETE
    assert len(revisions(counted)) == 1 and desk.item(counted.sid).seq == seq


# --- approval --------------------------------------------------------------------------------


def test_the_exact_text_is_approved_and_persisted_with_destination_time_and_digests(work):
    desk = work.desk()
    approval, decision = approve_current(desk, work.sid)
    item = desk.item(work.sid)
    assert item.state == ReviewState.APPROVED and item.approval == approval
    assert approval.text == item.current.text and approval.text_sha256 == decision.text_sha256
    assert (approval.channel_id, approval.due_at) == (CHANNEL, DUE)
    assert approval.approved_at == decision.decided_at == "2026-11-01T12:00:00Z"
    assert approval.bundle_record_sha256 == item.current.record_sha256
    assert decision.approval_id == approval.approval_id
    assert decision.origin_record_sha256 == item.origin.record_sha256
    assert decision.link_sha256 == item.link_sha256
    # The approval is an ordinary ledger approval: publish-schedule accepts it.
    result, transport = schedule(work, approval.approval_id, item.current.directory)
    assert result.intent.state == IntentState.ACCEPTED and len(transport.bodies) == 2


def test_an_edited_draft_is_approved_as_its_revision_and_schedulable(work):
    desk = work.desk()
    desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)
    approval, decision = approve_current(desk, work.sid)
    assert approval.text.startswith(EDITED_S01)
    revision = work.drafts / decision.draft_bundle
    result, _ = schedule(work, approval.approval_id, revision)
    assert result.intent.state == IntentState.ACCEPTED


def test_a_wrong_phrase_or_past_time_records_nothing(work):
    desk = work.desk()
    assert refused(desk.approve, work.sid, CHANNEL, DUE, "APPROVE", 0, NOW) == E.APPROVAL_DECLINED
    past = (NOW + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert refused(desk.approval_candidate, work.sid, CHANNEL, past, NOW) == E.SCHEDULE_NOT_FUTURE
    assert refused(desk.approve, work.sid, "nothex", DUE, "x", 0, NOW) == E.CHANNEL_INVALID
    with Ledger.open(work.ledger, create=False) as ledger:
        assert ledger.review_decisions(work.sid) == []
    assert desk.item(work.sid).state == ReviewState.UNAPPROVED


def test_text_changed_after_approval_withdraws_it_for_the_publisher(work):
    desk = work.desk()
    approval, decision = approve_current(desk, work.sid)
    desk.save_edit(work.sid, texts(S01=EDITED_S01), decision.seq, NOW)
    item = desk.item(work.sid)
    assert item.state == ReviewState.UNAPPROVED and item.approval is None
    # The old bundle and approval still exist, and the old text is unchanged on disk...
    assert load_candidate(work.path("draft"), CHANNEL, DUE, NOW).text == approval.text
    # ...but the publisher refuses the stale approval before any request.
    assert schedule_refused(work, approval.approval_id, work.path("draft")) == (
        E.APPROVAL_WITHDRAWN
    )


def test_a_rejected_draft_cannot_be_published_with_its_earlier_approval(work):
    desk = work.desk()
    approval, decision = approve_current(desk, work.sid)
    desk.reject(work.sid, reject_phrase(work.sid), decision.seq, NOW)
    assert desk.item(work.sid).state == ReviewState.REJECTED
    assert schedule_refused(work, approval.approval_id, work.path("draft")) == (
        E.APPROVAL_WITHDRAWN
    )
    # Approving the same post again through publish-approve does not revive it.
    with Ledger.open(work.ledger, create=False) as ledger:
        candidate = load_candidate(work.path("draft"), CHANNEL, DUE, NOW)
        approve(ledger, candidate, confirmation_phrase(candidate.approval_id), NOW)
    assert schedule_refused(work, approval.approval_id, work.path("draft")) == (
        E.APPROVAL_WITHDRAWN
    )


def test_a_rejection_is_final_and_needs_its_phrase(work):
    desk = work.desk()
    assert refused(desk.reject, work.sid, "REJECT", 0, NOW) == R.REJECT_NOT_CONFIRMED
    rejected = desk.reject(work.sid, reject_phrase(work.sid), 0, NOW)
    seq = rejected.seq
    assert refused(desk.save_edit, work.sid, texts(S01=EDITED_S01), seq, NOW) == E.REVIEW_CLOSED
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == E.REVIEW_CLOSED
    candidate = load_candidate(work.path("draft"), CHANNEL, DUE, NOW)
    typed = confirmation_phrase(candidate.approval_id)
    assert refused(desk.approve, work.sid, CHANNEL, DUE, typed, seq, NOW) == E.REVIEW_CLOSED
    with Ledger.open(work.ledger, create=False) as ledger:
        assert ledger.get_approval(candidate.approval_id) is None  # rolled back with the decision
        assert [d.decision for d in ledger.review_decisions(work.sid)] == [ReviewKind.REJECTED]


def test_repeated_approval_and_rejection_actions_add_no_rows(work):
    desk = work.desk()
    first, decision = approve_current(desk, work.sid, seq=0)
    again, repeated = approve_current(desk, work.sid, seq=0)  # e.g. a double-submitted form
    assert (again, repeated) == (first, decision)
    rejected = desk.reject(work.sid, reject_phrase(work.sid), decision.seq, NOW)
    assert desk.reject(work.sid, reject_phrase(work.sid), decision.seq, NOW) == rejected
    with Ledger.open(work.ledger, create=False) as ledger:
        rows = ledger._db.execute("SELECT count(*) FROM approvals").fetchone()[0]
        assert rows == 1
        assert [d.seq for d in ledger.review_decisions(work.sid)] == [decision.seq, rejected.seq]


def test_an_action_from_a_stale_page_is_refused(work):
    desk = work.desk()
    desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)  # another tab saved an edit
    assert refused(desk.reject, work.sid, reject_phrase(work.sid), 0, NOW) == E.REVIEW_STALE
    assert refused(approve_current, desk, work.sid, DUE, 0) == E.REVIEW_STALE
    edited = texts(S01=EDITED_S01 + " Again.")
    assert refused(desk.save_edit, work.sid, edited, 0, NOW) == E.REVIEW_STALE


def test_restarting_preserves_every_decision(work):
    desk = work.desk()
    desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)
    approval, _ = approve_current(desk, work.sid)
    restarted = work.desk()  # a new process opens the same files
    item = restarted.item(work.sid)
    assert item.state == ReviewState.APPROVED and item.approval == approval and item.edited
    assert [d.decision for d in item.decisions] == [ReviewKind.REVISED, ReviewKind.APPROVED]


class _DecidesDuringCheck:
    """A provider whose destination check lets another process record a review decision, i.e.
    the decision lands after submit's early checks and before the intent is committed."""

    def __init__(self, inner, decide) -> None:
        self.inner, self.decide = inner, decide

    def check_destination(self, channel_id, service="linkedin"):
        self.inner.check_destination(channel_id, service)
        self.decide()

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.parametrize("decision", ["reject", "edit"])
def test_a_decision_committed_during_submission_stops_the_send(work, decision):
    approval, approved = approve_current(work.desk(), work.sid)
    other = work.desk()  # its own ledger connection, like a second process

    def decide():
        if decision == "reject":
            other.reject(work.sid, reject_phrase(work.sid), approved.seq, NOW)
        else:
            other.save_edit(work.sid, texts(S01=EDITED_S01), approved.seq, NOW)

    transport = FakeTransport(channel_reply())  # a create request would run out of script
    candidate = load_candidate(work.path("draft"), CHANNEL, DUE, NOW)
    with Ledger.open(work.ledger, create=False) as ledger:
        provider_ = _DecidesDuringCheck(BufferPublisher(transport), decide)
        code = refused(submit, ledger, provider_, candidate, approval.approval_id, NOW)
        assert code == E.APPROVAL_WITHDRAWN
        assert ledger.get_intent(approval.approval_id) is None
    assert len(transport.bodies) == 1  # the destination check only; nothing was created


def _same_text_bundle(work, out: Path) -> Path:
    """Another complete, READY bundle with the origin's exact text: only the generator differs."""
    data = json.loads((work.path("draft") / "response.json").read_bytes())
    data["generator"]["label"] = "Another build of the same text"
    prepared = prepare_build(work.snapshot, work.path("brief"))
    build_from_response(prepared, canonical_json(data).encode("utf-8"), out)
    return out


def test_an_approval_from_review_schedules_only_the_reviewed_bundle(work):
    approval, _ = approve_current(work.desk(), work.sid)
    other = _same_text_bundle(work, work.root / "other-draft")
    candidate = load_candidate(other, CHANNEL, DUE, NOW)
    assert candidate.approval_id == approval.approval_id  # same text, channel and time
    assert candidate.bundle_record_sha256 != approval.bundle_record_sha256
    assert schedule_refused(work, approval.approval_id, other) == E.APPROVAL_NOT_REVIEWED_DRAFT
    with Ledger.open(work.ledger, create=False) as ledger:  # and in the intent transaction
        for fingerprint in (candidate.bundle_record_sha256, None):
            code = refused(ledger.begin_send, approval.approval_id, NOW, fingerprint)
            assert code == E.APPROVAL_NOT_REVIEWED_DRAFT
        assert ledger.get_intent(approval.approval_id) is None
    result, _ = schedule(work, approval.approval_id, work.path("draft"))
    assert result.intent.state == IntentState.ACCEPTED


def test_restored_text_cannot_reuse_the_approval_of_another_bundle(work):
    desk = work.desk()
    _, approved = approve_current(desk, work.sid)
    edited = desk.save_edit(work.sid, texts(S01=EDITED_S01), approved.seq, NOW)
    restored = desk.save_edit(work.sid, texts(), edited.seq, NOW)  # back to the approved text
    assert restored.draft_bundle != approved.draft_bundle
    assert refused(approve_current, desk, work.sid) == E.APPROVAL_CONFLICT
    item = desk.item(work.sid)  # nothing was recorded, and the draft is still reviewable
    assert item.state == ReviewState.UNAPPROVED and item.seq == restored.seq
    with Ledger.open(work.ledger, create=False) as ledger:
        assert ledger._db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 1
    later = "2026-11-02T10:30:00Z"
    approval, decision = approve_current(desk, work.sid, due=later)
    assert desk.item(work.sid).state == ReviewState.APPROVED
    assert approval.bundle_record_sha256 == decision.draft_record_sha256
    result, _ = schedule(work, approval.approval_id, work.drafts / restored.draft_bundle, later)
    assert result.intent.state == IntentState.ACCEPTED


def test_reapproving_the_same_bundle_and_time_reuses_its_approval(work):
    desk = work.desk()
    first, _ = approve_current(desk, work.sid)
    approve_current(desk, work.sid, due="2026-11-02T10:30:00Z")  # withdraws the first
    again, decision = approve_current(desk, work.sid)
    assert again == first and decision.approval_id == first.approval_id
    assert desk.item(work.sid).approval == first
    result, _ = schedule(work, first.approval_id, work.path("draft"))
    assert result.intent.state == IntentState.ACCEPTED


# --- tampered or incomplete inputs ----------------------------------------------------------


def _edit_link(work):
    path = work.path("link") / "story-link.json"
    data = json.loads(path.read_bytes())
    data["angle"] = "A different angle."
    path.write_bytes(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())


def _edit_draft_text(work):
    path = work.path("draft") / "draft.txt"
    path.write_bytes(path.read_bytes().replace(b"screenshot", b"photo"))


def _rewrite_map_consistently(work):
    """A forged assertion map whose record digests are recomputed: still refused, because the
    map is not what the checker produces from the response."""
    import hashlib

    directory = work.path("draft")
    amap = json.loads((directory / "assertion-map.json").read_bytes())
    amap["segments"][0]["classification"] = "FRAMING"
    data = json.dumps(amap, sort_keys=True, separators=(",", ":")).encode()
    (directory / "assertion-map.json").write_bytes(data)
    record = json.loads((directory / "bundle-record.json").read_bytes())
    for entry in record["files"]:
        if entry["name"] == "assertion-map.json":
            entry["sha256"], entry["size"] = hashlib.sha256(data).hexdigest(), len(data)
    (directory / "bundle-record.json").write_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    )


def _extra_link_file(work):
    (work.path("link") / "extra.txt").write_bytes(b"x")


def _remove_brief(work):
    shutil.rmtree(work.path("brief"))


def _new_evidence(work):
    changed = historic_finding(support="The README reports the scene confirmed for 3/10 runs.")
    found = write_json(work.root / "again.json", findings_document(work.snapshot, [changed]))
    run_discovery(work.preferences, work.backlog, [work.snapshot], [found], NOW)


TAMPERING = {
    "link_edited": (_edit_link, Refusal.LINK_MISMATCH),
    "draft_text_edited": (_edit_draft_text, Refusal.DRAFT_INCOMPLETE),
    "map_forged_with_record": (_rewrite_map_consistently, Refusal.DRAFT_INCONSISTENT),
    "link_extra_file": (_extra_link_file, Refusal.LINK_UNREADABLE),
    "brief_removed": (_remove_brief, Refusal.BRIEF_INCOMPLETE),
    "newer_evidence": (_new_evidence, Refusal.EVIDENCE_CHANGED),
}


@pytest.mark.parametrize("name", TAMPERING)
def test_a_tampered_or_incomplete_draft_is_refused_for_every_action(work, name):
    damage, reason = TAMPERING[name]
    damage(work)
    desk = work.desk()
    items, refusals = desk.queue()
    assert items == [] and [(r.story_id, r.reason) for r in refusals] == [(work.sid, reason)]
    assert refused(desk.item, work.sid) == R.NOT_REVIEWABLE
    assert refused(desk.save_edit, work.sid, texts(S01=EDITED_S01), 0, NOW) == R.NOT_REVIEWABLE
    assert refused(desk.reject, work.sid, reject_phrase(work.sid), 0, NOW) == R.NOT_REVIEWABLE
    assert refused(approve_current, desk, work.sid) == R.NOT_REVIEWABLE


def test_a_removed_or_modified_revision_is_never_replaced_by_the_original(work):
    desk = work.desk()
    decision = desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)
    revision = work.drafts / decision.draft_bundle
    (revision / "draft.txt").write_bytes(b"Something else.\n")
    assert desk.queue()[1][0].reason == Refusal.REVISION_INVALID
    shutil.rmtree(revision)
    assert desk.queue()[1][0].reason == Refusal.REVISION_INVALID
    assert refused(desk.item, work.sid) == R.NOT_REVIEWABLE


def test_a_decision_for_another_link_is_refused(work):
    desk = work.desk()
    approve_current(desk, work.sid)
    connection = sqlite3.connect(work.ledger, isolation_level=None)
    try:  # a forged row naming another 06D draft (the triggers forbid editing the real one)
        connection.execute(
            "INSERT INTO review_decisions (story_id, decision, approval_id, text, text_sha256, "
            "draft_bundle, draft_record_sha256, origin_record_sha256, link_sha256, decided_at) "
            "VALUES (?, 'REVISED', NULL, 't', ?, ?, ?, ?, ?, '2026-11-01T12:00:00Z')",
            (work.sid, "a" * 64, f"{work.sid}-draft", "b" * 64, "c" * 64, "d" * 64),
        )
    finally:
        connection.close()
    assert desk.queue()[1][0].reason == Refusal.DECISION_MISMATCH


def test_review_decisions_cannot_be_changed_or_deleted(work):
    approve_current(work.desk(), work.sid)
    connection = sqlite3.connect(work.ledger, isolation_level=None)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute("UPDATE review_decisions SET decision = 'REJECTED'")
        with pytest.raises(sqlite3.DatabaseError, match="permanent"):
            connection.execute("DELETE FROM review_decisions")
    finally:
        connection.close()


def test_a_schema_1_ledger_is_upgraded_in_place_and_keeps_its_approvals(tmp_path):
    """A ledger written before 06E (approval and intent) opens, even for scheduling, gains the
    review table, and its approval is unaffected: it was never reviewed."""
    path = tmp_path / "ledger.sqlite"
    candidate = Candidate("A post.", CHANNEL, DUE, "demo_workspace", "e" * 64)
    connection = sqlite3.connect(path, isolation_level=None)
    for statement in ledger_module._SCHEMA_V1:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ledger_module._approval_row(candidate, NOW),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    with Ledger.open(path, create=False) as ledger:
        assert ledger.get_approval(candidate.approval_id).text == "A post."
        assert not ledger.approval_withdrawn(candidate.approval_id)
        assert ledger._db.execute("PRAGMA user_version").fetchone()[0] == 2
        ledger.begin_send(candidate.approval_id, NOW)  # the schema 1 contract still works


def test_a_schema_1_file_with_anything_else_is_not_upgraded(tmp_path):
    path = tmp_path / "ledger.sqlite"
    connection = sqlite3.connect(path, isolation_level=None)
    for statement in ledger_module._SCHEMA_V1:
        connection.execute(statement)
    connection.execute("CREATE TABLE unrelated (x)")
    connection.execute("PRAGMA user_version = 1")
    connection.close()
    before = path.read_bytes()
    for create in (False, True):
        assert refused(Ledger.open, path, create=create) == E.LEDGER_UNUSABLE
    assert path.read_bytes() == before


ALTERED_SCHEMAS = {
    "added_column": "ALTER TABLE approvals ADD COLUMN unexpected TEXT",
    "replaced_trigger": "DROP TRIGGER approvals_no_update; CREATE TRIGGER approvals_no_update "
    "BEFORE UPDATE ON approvals BEGIN SELECT 1; END",
}


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("change", ALTERED_SCHEMAS)
def test_a_ledger_with_the_right_names_but_altered_definitions_is_refused(
    tmp_path, version, change
):
    path = tmp_path / "ledger.sqlite"
    schema = ledger_module._SCHEMA_V1 if version == 1 else ledger_module._SCHEMA
    connection = sqlite3.connect(path, isolation_level=None)
    for statement in schema:
        connection.execute(statement)
    connection.executescript(ALTERED_SCHEMAS[change])
    connection.execute(f"PRAGMA user_version = {version}")
    connection.close()
    before = path.read_bytes()
    for create in (False, True):
        assert refused(Ledger.open, path, create=create) == E.LEDGER_UNUSABLE
    assert path.read_bytes() == before


# --- the local interface ---------------------------------------------------------------------


class Client:
    """HTTP/1.1 to the review server over a loopback socket; nothing else can be reached."""

    def __init__(self, server: ReviewServer) -> None:
        self.server = server
        self.cookie: str | None = None

    def request(self, method, path, form=None, headers=None, host=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        _CONNECT(sock, (HOST, self.server.server_port))
        connection = http.client.HTTPConnection(HOST, self.server.server_port, timeout=10)
        connection.sock = sock
        sent = dict(headers or {})
        if host is not None:
            sent["Host"] = host
        if self.cookie:
            sent.setdefault("Cookie", self.cookie)
        body = None
        if form is not None:
            body = urlencode(form).encode("utf-8")
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        # A supplied Host header replaces the one http.client would add.
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        result = (response.status, dict(response.getheaders()), page)
        connection.close()
        return result

    def sign_in(self) -> None:
        status, headers, _ = self.request("GET", f"/?token={self.server.token}")
        assert status == 303 and headers["Location"] == "/"
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def post(self, path, **fields):
        return self.request("POST", path, {"token": self.server.token, **fields})


@pytest.fixture
def serve():
    servers = []

    def start(desk: ReviewDesk, scheduler=None, stopped=lambda: False) -> Client:
        server = ReviewServer(desk, 0, clock=lambda: NOW, scheduler=scheduler, stopped=stopped)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        client = Client(server)
        client.sign_in()
        return client

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def seq_of(page: str) -> str:
    return page.split("name=seq value=", 1)[1].split(">", 1)[0]


def test_the_complete_review_workflow_through_the_local_interface(work, serve):
    client = serve(work.desk())
    story = f"/story/{work.sid}"
    # The queue lists the draft as UNAPPROVED.
    status, headers, page = client.request("GET", "/")
    assert status == 200 and work.sid[:12] in page and "UNAPPROVED" in page
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    # The story page shows the post, repository, angle, citations, checks and limitations.
    status, _, page = client.request("GET", story)
    assert status == 200
    for expected in (
        "proves capture, not that the right scene was shown",
        HISTORIC.lower(),
        "unverified backlog text",
        "README.md:3-4",
        "QUOTE_LOCATED",
        "READY_FOR_REVIEW",
        "Limitation:",
        "not semantic certification",
        "Semantic support: <span class=badge>UNKNOWN</span>",
    ):
        assert expected in page, expected
    # Revalidate an unsupported edit: shown, flagged, not saved.
    form = {f"seg-{k}": v for k, v in texts(S02=UNSUPPORTED_S02).items()}
    status, _, page = client.post(f"{story}/edit", seq=seq_of(page), action="check", **form)
    assert status == 200 and "NEEDS_REVISION" in page and "NUMBER_NOT_IN_EVIDENCE" in page
    assert "UNSAVED edit" in page and "40% gain" in page
    assert "Save or discard the edit first" in page
    # Revalidate and save a supported edit.
    form = {f"seg-{k}": v for k, v in texts(S01=EDITED_S01).items()}
    status, headers, _ = client.post(f"{story}/edit", seq="0", action="save", **form)
    assert status == 303 and headers["Location"].split("?", 1)[0] == story
    status, _, page = client.request("GET", story)
    assert EDITED_S01 in page and "edited by owner" in page and "REVISED" in page
    # The approval sheet, then the typed phrase.
    seq = seq_of(page)
    status, _, page = client.post(f"{story}/sheet", seq=seq, channel_id=CHANNEL, due_at=DUE)
    assert status == 200 and "Approve this exact post?" in page
    confirmation = page.split('name=confirmation value="', 1)[1].split('"', 1)[0]
    status, _, page = client.post(
        f"{story}/approve",
        seq=seq,
        channel_id=CHANNEL,
        due_at=DUE,
        confirmation="unknown",
        confirm="yes",
    )
    assert status == 409 and "REVIEW_STALE" in page
    status, _, _ = client.post(
        f"{story}/approve", seq=seq, confirmation=confirmation, confirm="yes"
    )
    assert status == 303
    # A double submission of the same form adds nothing.
    status, _, _ = client.post(
        f"{story}/approve", seq=seq, confirmation=confirmation, confirm="yes"
    )
    assert status == 303
    status, _, page = client.request("GET", story)
    assert "Recorded approval" in page and "publish-schedule --bundle" not in page
    approval_id = page.split("<p>approval id: ", 1)[1].split("<", 1)[0]

    # Restart: a new server process over the same files shows the same decision.
    restarted = serve(work.desk())
    status, _, page = restarted.request("GET", "/")
    assert "APPROVED" in page and "(edited)" in page
    with Ledger.open(work.ledger, create=False) as ledger:
        kinds = [d.decision for d in ledger.review_decisions(work.sid)]
        assert kinds == [ReviewKind.REVISED, ReviewKind.APPROVED]
        approval = ledger.get_approval(approval_id)
    assert approval.text.startswith(EDITED_S01)
    # The publisher accepts exactly this approval for the revision bundle.
    revision = next(p for p in work.drafts.iterdir() if REVISION_INFIX in p.name)
    result, _ = schedule(work, approval_id, revision)
    assert result.intent.state == IntentState.ACCEPTED


def test_the_full_cited_range_is_chosen_through_the_local_interface(counted, serve):
    client = serve(counted.desk())
    story = f"/story/{counted.sid}"
    status, _, page = client.request("GET", story)
    assert status == 200 and "NEEDS_REVISION" in page and "NUMBER_NOT_IN_EVIDENCE" in page
    # The page says what each reference is checked against, and what the finding means.
    assert "checked against: the quoted text only, &ldquo;showed the intended scene&rdquo;" in page
    assert "selects just the quoted text, even when other numbers are visible" in page
    assert "name=full-S02.1 value=1>" in page and "name=full-S03" not in page
    form = {f"seg-{k}": v for k, v in counted_texts().items()}
    # Revalidate with the full range: checked, shown, still selected, nothing saved.
    status, _, page = client.post(
        f"{story}/edit", seq=seq_of(page), action="check", **{"full-S02.1": "1"}, **form
    )
    assert status == 200 and "READY_FOR_REVIEW" in page and "UNSAVED edit" in page
    assert "name=full-S02.1 value=1 checked>" in page and revisions(counted) == []
    # A forged choice is refused with its reason; nothing is saved.
    status, _, page = client.post(
        f"{story}/edit", seq="0", action="save", **{"full-S03.1": "1"}, **form
    )
    assert status == 409 and "SCOPE_INVALID" in page and revisions(counted) == []
    status, headers, _ = client.post(
        f"{story}/edit", seq="0", action="save", **{"full-S02.1": "1"}, **form
    )
    assert status == 303 and headers["Location"].split("?", 1)[0] == story
    status, _, page = client.request("GET", story)
    assert "REVISED" in page and "edited by owner" in page and "UNAPPROVED" in page
    assert "checked against: the entire cited range, lines 6-6" in page
    assert "name=full-S02.1" not in page and "name=full-S01.1" in page
    # The page offered from before the save is stale now.
    status, _, page = client.post(
        f"{story}/edit", seq="0", action="save", **{"full-S01.1": "1"}, **form
    )
    assert status == 409 and "REVIEW_STALE" in page and len(revisions(counted)) == 1


def test_rejecting_through_the_interface_is_final(work, serve):
    client = serve(work.desk())
    story = f"/story/{work.sid}"
    _, _, page = client.request("GET", story)
    status, _, _ = client.post(f"{story}/reject", seq=seq_of(page), typed=reject_phrase(work.sid))
    assert status == 303
    _, _, page = client.request("GET", story)
    assert "A rejection is final" in page and "name=seq" not in page  # no action forms left


@pytest.mark.parametrize(
    "host", ["localhost", "evil.example:8765", "127.0.0.1", "127.0.0.1:1"], ids=str
)
def test_a_request_for_another_host_name_is_refused(work, serve, host):
    client = serve(work.desk())
    status, _, page = client.request("GET", "/", host=host)
    assert status == 400 and work.sid[:12] not in page


def test_pages_and_actions_need_the_session(work, serve):
    client = serve(work.desk())
    client.cookie = None
    status, _, page = client.request("GET", "/")
    assert status == 403 and work.sid[:12] not in page
    assert client.request("GET", "/?token=wrong")[0] == 403
    status, _, _ = client.post(f"/story/{work.sid}/reject", seq="0", typed=reject_phrase(work.sid))
    assert status == 403
    client.cookie = f"pros_review={client.server.token}"
    status, _, _ = client.request(
        "POST",
        f"/story/{work.sid}/reject",
        {"token": "wrong", "seq": "0", "typed": reject_phrase(work.sid)},
    )
    assert status == 403
    assert work.desk().item(work.sid).state == ReviewState.UNAPPROVED


def test_a_refused_post_receives_its_page_instead_of_a_reset_connection(work, serve):
    """The body of a refused request is read before closing: a socket closed with unread data
    resets, and the client would see a dropped connection instead of the refusal."""
    client = serve(work.desk())
    client.cookie = None
    padding = "x" * 200_000
    for _ in range(5):
        status, _, page = client.post(f"/story/{work.sid}/reject", seq="0", pad=padding)
        assert status == 403 and "Not signed in" in page


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}],
    ids=["origin", "fetch_site"],
)
def test_a_cross_site_post_is_refused(work, serve, headers):
    client = serve(work.desk())
    form = {"token": client.server.token, "seq": "0", "typed": reject_phrase(work.sid)}
    status, _, _ = client.request("POST", f"/story/{work.sid}/reject", form, headers=headers)
    assert status == 403
    assert work.desk().item(work.sid).state == ReviewState.UNAPPROVED


# What Chrome sends for the review page's own forms, observed with headless Chrome: the page's
# Referrer-Policy: no-referrer makes the request origin opaque.
CHROME_FORM = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}


def test_chromes_own_form_posts_are_accepted(work, serve):
    client = serve(work.desk())
    story = f"/story/{work.sid}"
    _, _, page = client.request("GET", story)
    form = {"token": client.server.token, "seq": seq_of(page), "action": "check"}
    form |= {f"seg-{k}": v for k, v in texts().items()}
    status, _, checked = client.request("POST", f"{story}/edit", form, headers=CHROME_FORM)
    assert status == 200 and "READY_FOR_REVIEW" in checked
    form = {"token": client.server.token, "seq": seq_of(page), "typed": reject_phrase(work.sid)}
    status, _, _ = client.request("POST", f"{story}/reject", form, headers=CHROME_FORM)
    assert status == 303
    assert work.desk().item(work.sid).state == ReviewState.REJECTED


@pytest.mark.parametrize(
    "site",
    [None, "cross-site", "same-site", "none", "Same-Origin", "same-origin, cross-site"],
    ids=["missing", "cross_site", "same_site", "none", "case", "list"],
)
def test_a_null_origin_is_refused_without_same_origin_fetch_metadata(work, serve, site):
    client = serve(work.desk())
    headers = {"Origin": "null"} | ({} if site is None else {"Sec-Fetch-Site": site})
    form = {"token": client.server.token, "seq": "0", "typed": reject_phrase(work.sid)}
    status, _, _ = client.request("POST", f"/story/{work.sid}/reject", form, headers=headers)
    assert status == 403
    assert work.desk().item(work.sid).state == ReviewState.UNAPPROVED


@pytest.mark.parametrize("attack", ["no_session", "form_token", "host"])
def test_chromes_headers_do_not_replace_the_session_token_or_host_checks(work, serve, attack):
    client = serve(work.desk())
    form = {"token": client.server.token, "seq": "0", "typed": reject_phrase(work.sid)}
    host = None
    if attack == "no_session":
        client.cookie = None
    elif attack == "form_token":
        form["token"] = "wrong"
    else:
        host = "localhost"
    status, _, _ = client.request(
        "POST", f"/story/{work.sid}/reject", form, headers=CHROME_FORM, host=host
    )
    assert status == {"host": 400}.get(attack, 403)
    assert work.desk().item(work.sid).state == ReviewState.UNAPPROVED


def test_malformed_forms_are_refused(work, serve):
    client = serve(work.desk())
    path = f"/story/{work.sid}/reject"
    token = client.server.token
    assert client.request("POST", path, {"token": token, "seq": "x"})[0] == 400
    plain = {"Content-Type": "text/plain"}
    assert client.request("POST", path, {"token": token, "seq": "0"}, headers=plain)[0] == 400
    # The same field twice: built by hand, since urlencode of a dict cannot repeat a key.
    sock = socket.socket()
    sock.settimeout(10)
    _CONNECT(sock, (HOST, client.server.server_port))
    connection = http.client.HTTPConnection(HOST, client.server.server_port, timeout=10)
    connection.sock = sock
    body = f"token={token}&seq=0&typed=a&typed={reject_phrase(work.sid)}".replace(" ", "+")
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": client.cookie}
    connection.request("POST", path, body=body.encode(), headers=headers)
    assert connection.getresponse().status == 400
    connection.close()
    assert work.desk().item(work.sid).state == ReviewState.UNAPPROVED


def test_the_server_listens_on_loopback_only(work, serve):
    client = serve(work.desk())
    assert client.server.server_address[0] == "127.0.0.1"
    assert client.server.start_url.startswith(f"http://127.0.0.1:{client.server.server_port}/")


def test_an_unexpected_defect_shows_a_fixed_page_without_internals(work, serve, monkeypatch):
    desk = work.desk()
    client = serve(desk)

    def broken():
        raise RuntimeError(f"internal detail {work.drafts}")

    monkeypatch.setattr(desk, "queue", broken)
    status, _, page = client.request("GET", "/")
    assert status == 500 and "internal detail" not in page and "Traceback" not in page
    assert client.server.token not in page


# --- scheduling from the review page (06F) ---------------------------------------------------


class ScriptedPublisher:
    """The provider factory `review-serve --enable-scheduling` hands the server, over the scripted
    transport instead of HTTPS. `built` counts how often a provider was made; `transport.bodies`
    records every request, so an extra or retried request cannot pass unseen."""

    def __init__(self, *script) -> None:
        self.transport = FakeTransport(*script)
        self.built = 0

    def __call__(self) -> BufferPublisher:
        self.built += 1
        return BufferPublisher(self.transport)

    def creates(self) -> list[dict]:
        return [
            r["variables"]["input"]
            for r in self.transport.requests
            if r["query"].startswith("mutation")
        ]


def schedule_here(desk: ReviewDesk, sid: str, publisher, now=NOW, **form):
    """The schedule action as the current page would post it, unless `form` overrides a field."""
    item = desk.item(sid)
    approval_id = form.get("approval_id", item.approval.approval_id if item.approval else "")
    typed = form.get("typed", schedule_phrase(approval_id))
    return desk.schedule(sid, approval_id, typed, form.get("seq", item.seq), publisher, now)


def intent_of(work: Work, approval_id: str):
    with Ledger.open(work.ledger, create=False) as ledger:
        return ledger.get_intent(approval_id)


def publish_schedule_refusal(work: Work, approval_id: str, bundle: Path) -> PublicationErrorCode:
    """publish-schedule's own path (load_candidate + submit) for an approval, expecting no send."""
    transport = FakeTransport()  # any request would fail the test
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    with Ledger.open(work.ledger, create=False) as ledger:
        code = refused(submit, ledger, BufferPublisher(transport), candidate, approval_id, NOW)
    assert transport.bodies == []
    return code


SCHEDULE_BUTTON = "Schedule approved post</button>"


def test_scheduling_is_refused_while_it_is_disabled(work):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    assert refused(schedule_here, desk, work.sid, None) == R.SCHEDULING_DISABLED
    assert intent_of(work, approval.approval_id) is None


def test_an_approved_draft_is_scheduled_with_one_request_for_the_exact_approved_post(
    work, monkeypatch
):
    calls = []
    real_submit = review_service.submit

    def counted(*args, **kwargs):
        calls.append(args)
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(review_service, "submit", counted)
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    publisher = ScriptedPublisher(channel_reply(), create_ok())
    result = schedule_here(desk, work.sid, publisher)
    assert len(calls) == 1 and publisher.built == 1  # the existing submit, exactly once
    assert publisher.transport.operations() == ["query", "mutation"]
    (sent,) = publisher.creates()
    assert sent["text"] == approval.text == desk.item(work.sid).current.text
    assert sent["channelId"] == CHANNEL and sent["dueAt"] == "2026-11-02T09:30:00.000Z"
    assert sent["mode"] == "customScheduled"
    assert result.intent.state == IntentState.ACCEPTED and result.intent.provider_post_id == POST_ID
    assert desk.item(work.sid).intent == result.intent == intent_of(work, approval.approval_id)


def test_an_approved_revision_is_scheduled_with_its_edited_text(work):
    desk = work.desk()
    desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)
    approval, _ = approve_current(desk, work.sid)
    publisher = ScriptedPublisher(channel_reply(), create_ok())
    schedule_here(desk, work.sid, publisher)
    (sent,) = publisher.creates()
    assert sent["text"] == approval.text and sent["text"].startswith(EDITED_S01)


def test_an_unapproved_draft_cannot_be_scheduled(work):
    publisher = ScriptedPublisher()
    assert refused(schedule_here, work.desk(), work.sid, publisher) == R.NOT_APPROVED
    assert publisher.built == 0 and publisher.transport.bodies == []


def test_scheduling_needs_its_typed_phrase(work):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    publisher = ScriptedPublisher()
    for typed in ("", "SCHEDULE", confirmation_phrase(approval.approval_id)):
        code = refused(schedule_here, desk, work.sid, publisher, typed=typed)
        assert code == R.SCHEDULE_NOT_CONFIRMED
    assert publisher.built == 0 and intent_of(work, approval.approval_id) is None


@pytest.mark.parametrize("change", ["reject", "edit", "reapprove"])
def test_a_rejected_or_withdrawn_approval_cannot_be_scheduled(work, change):
    desk = work.desk()
    approval, decision = approve_current(desk, work.sid)
    if change == "reject":
        desk.reject(work.sid, reject_phrase(work.sid), decision.seq, NOW)
    elif change == "edit":
        desk.save_edit(work.sid, texts(S01=EDITED_S01), decision.seq, NOW)
    else:
        approve_current(desk, work.sid, due="2026-11-02T10:30:00Z")
    old = approval.approval_id
    publisher = ScriptedPublisher()
    # From the page shown before the change...
    code = refused(schedule_here, desk, work.sid, publisher, approval_id=old, seq=decision.seq)
    assert code == E.REVIEW_STALE
    # ...and from a reloaded page that still names the withdrawn approval.
    code = refused(schedule_here, desk, work.sid, publisher, approval_id=old)
    expected = {"reject": E.REVIEW_CLOSED, "edit": R.NOT_APPROVED, "reapprove": E.REVIEW_STALE}
    assert code == expected[change]
    assert publisher.built == 0 and publisher.transport.bodies == []
    assert intent_of(work, old) is None


def test_an_approval_whose_time_is_too_close_cannot_be_scheduled(work):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    late = datetime(2026, 11, 2, 9, 26, tzinfo=UTC)  # 4 minutes before the approved time
    publisher = ScriptedPublisher()
    assert refused(schedule_here, desk, work.sid, publisher, now=late) == E.SCHEDULE_NOT_FUTURE
    assert publisher.built == 0 and intent_of(work, approval.approval_id) is None


def _forge_decision_for_another_link(work):
    connection = sqlite3.connect(work.ledger, isolation_level=None)
    try:
        connection.execute(
            "INSERT INTO review_decisions (story_id, decision, approval_id, text, text_sha256, "
            "draft_bundle, draft_record_sha256, origin_record_sha256, link_sha256, decided_at) "
            "VALUES (?, 'REVISED', NULL, 't', ?, ?, ?, ?, ?, '2026-11-01T12:00:00Z')",
            (work.sid, "a" * 64, f"{work.sid}-draft", "b" * 64, "c" * 64, "d" * 64),
        )
    finally:
        connection.close()


def _remove_draft(work):
    shutil.rmtree(work.path("draft"))


SCHEDULE_TAMPERING = {
    **{name: damage for name, (damage, _) in TAMPERING.items()},
    "draft_removed": _remove_draft,
    "conflicting_decision": _forge_decision_for_another_link,
}


@pytest.mark.parametrize("name", SCHEDULE_TAMPERING)
def test_a_missing_altered_or_conflicting_bundle_cannot_be_scheduled(work, name):
    approval, decision = approve_current(work.desk(), work.sid)
    SCHEDULE_TAMPERING[name](work)
    publisher = ScriptedPublisher()
    phrase = schedule_phrase(approval.approval_id)
    code = refused(
        work.desk().schedule, work.sid, approval.approval_id, phrase, decision.seq, publisher, NOW
    )
    assert code == R.NOT_REVIEWABLE
    assert publisher.built == 0 and intent_of(work, approval.approval_id) is None


def test_a_removed_approved_revision_is_not_replaced_by_the_original(work):
    desk = work.desk()
    desk.save_edit(work.sid, texts(S01=EDITED_S01), 0, NOW)
    approval, decision = approve_current(desk, work.sid)
    shutil.rmtree(work.drafts / decision.draft_bundle)
    publisher = ScriptedPublisher()
    phrase = schedule_phrase(approval.approval_id)
    code = refused(
        desk.schedule, work.sid, approval.approval_id, phrase, decision.seq, publisher, NOW
    )
    assert code == R.NOT_REVIEWABLE and publisher.built == 0


FIRST_OUTCOMES = {
    "accepted": (create_ok(), IntentState.ACCEPTED, "ACCEPTED"),
    "rejected": (create_error("InvalidInputError"), IntentState.REJECTED, "InvalidInputError"),
    "ambiguous": (timeout(), IntentState.AMBIGUOUS, "TIMEOUT"),
}


@pytest.mark.parametrize("first", FIRST_OUTCOMES)
def test_a_submitted_approval_is_never_sent_again(work, first):
    create, state, code = FIRST_OUTCOMES[first]
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    once = ScriptedPublisher(channel_reply(), create)
    result = schedule_here(desk, work.sid, once)
    assert (result.intent.state, result.intent.detail_code) == (state, code)
    again = ScriptedPublisher()  # any request would fail the test
    assert refused(schedule_here, desk, work.sid, again) == E.DUPLICATE_SUBMISSION
    assert again.transport.bodies == [] and len(once.transport.bodies) == 2
    # The manual publish-schedule path refuses it as well.
    code = publish_schedule_refusal(work, approval.approval_id, work.path("draft"))
    assert code == E.DUPLICATE_SUBMISSION


UNCERTAIN = {
    "timeout": (timeout(), "TIMEOUT"),
    "network": (TransportFailure(timed_out=False), "NETWORK"),
    "server_error": (TransportReply(502, b"{}"), "HTTP_STATUS"),
    "unparseable": (TransportReply(200, b"not json"), "UNPARSEABLE_RESPONSE"),
}


@pytest.mark.parametrize("outcome", UNCERTAIN)
def test_an_uncertain_outcome_is_recorded_and_never_retried(work, outcome):
    create, code = UNCERTAIN[outcome]
    desk = work.desk()
    approve_current(desk, work.sid)
    publisher = ScriptedPublisher(channel_reply(), create)
    result = schedule_here(desk, work.sid, publisher)
    assert result.intent.state == IntentState.AMBIGUOUS and result.intent.detail_code == code
    assert publisher.transport.operations() == ["query", "mutation"]  # one create, no retry
    assert desk.item(work.sid).intent.state == IntentState.AMBIGUOUS


def test_an_interrupted_submission_stays_sending_and_is_never_offered_again(work, serve):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    publisher = ScriptedPublisher(channel_reply(), KeyboardInterrupt())  # the process stops
    with pytest.raises(KeyboardInterrupt):
        schedule_here(desk, work.sid, publisher)
    assert len(publisher.creates()) == 1
    # A new process: the intent is still SENDING, shown as uncertain, and never sent again.
    restarted = work.desk()
    assert restarted.item(work.sid).intent.state == IntentState.SENDING
    again = ScriptedPublisher()
    assert refused(schedule_here, restarted, work.sid, again) == E.DUPLICATE_SUBMISSION
    client = serve(restarted, scheduler=again)
    _, _, page = client.request("GET", f"/story/{work.sid}")
    assert escape(INTENT_GUIDANCE[IntentState.SENDING], quote=True) in page
    assert SCHEDULE_BUTTON not in page and f"{work.sid}/schedule" not in page
    assert again.transport.bodies == []
    assert intent_of(work, approval.approval_id).state == IntentState.SENDING


_STATE_PATH = {
    IntentState.SENDING: (),
    IntentState.ACCEPTED: (IntentState.ACCEPTED,),
    IntentState.AMBIGUOUS: (IntentState.AMBIGUOUS,),
    IntentState.REJECTED: (IntentState.REJECTED,),
}


@pytest.mark.parametrize("state", list(IntentState))
def test_the_page_shows_the_persisted_intent_state_and_its_meaning(work, serve, state):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    with Ledger.open(work.ledger, create=False) as ledger:
        ledger.begin_send(approval.approval_id, NOW, approval.bundle_record_sha256)
        for step in _STATE_PATH.get(state, (IntentState.ACCEPTED, state)):
            ledger.transition(approval.approval_id, step, NOW, post_id=POST_ID, code="CODE")
    publisher = ScriptedPublisher()
    client = serve(work.desk(), scheduler=publisher)
    _, _, page = client.request("GET", f"/story/{work.sid}")
    assert f"<span class=badge>{state.value}</span>" in page
    assert escape(INTENT_GUIDANCE[state], quote=True) in page
    assert SCHEDULE_BUTTON not in page and "Not submitted" not in page
    assert (escape(NOT_CANCELLED, quote=True) in page) == (state != IntentState.REJECTED)
    _, _, queue = client.request("GET", "/")
    assert f"Buffer: {state.value}" in queue
    assert publisher.built == 0
    guidance = INTENT_GUIDANCE[state].lower()
    if state == IntentState.SENT:
        assert "(published)" in guidance
    else:  # nothing short of provider evidence of sending is called published
        assert "published" not in guidance.replace("not published", "")
    if state == IntentState.ACCEPTED:
        assert "not yet independently confirmed" in guidance


def test_a_post_scheduled_with_publish_schedule_is_shown_and_not_offered_again(work, serve):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    result, _ = schedule(work, approval.approval_id, work.path("draft"))  # the manual command
    assert result.intent.state == IntentState.ACCEPTED
    publisher = ScriptedPublisher()
    client = serve(work.desk(), scheduler=publisher)
    _, _, page = client.request("GET", f"/story/{work.sid}")
    assert "<span class=badge>ACCEPTED</span>" in page and SCHEDULE_BUTTON not in page
    assert "Check the current status in Buffer" in page and "publish-schedule --bundle" not in page
    assert refused(schedule_here, desk, work.sid, publisher) == E.DUPLICATE_SUBMISSION
    assert publisher.transport.bodies == []


@pytest.mark.parametrize("change", ["reject", "edit"])
def test_withdrawing_after_a_request_explains_that_buffer_is_not_cancelled(work, serve, change):
    desk = work.desk()
    approval, decision = approve_current(desk, work.sid)
    schedule_here(desk, work.sid, ScriptedPublisher(channel_reply(), create_ok()))
    if change == "reject":
        desk.reject(work.sid, reject_phrase(work.sid), decision.seq, NOW)
    else:
        desk.save_edit(work.sid, texts(S01=EDITED_S01), decision.seq, NOW)
    item = desk.item(work.sid)
    assert item.intent is None
    assert [i.approval_id for i in item.withdrawn_intents] == [approval.approval_id]
    publisher = ScriptedPublisher()
    client = serve(work.desk(), scheduler=publisher)
    _, _, page = client.request("GET", f"/story/{work.sid}")
    assert "Earlier Buffer submissions" in page and escape(NOT_CANCELLED, quote=True) in page
    assert "does NOT cancel" in page and "<span class=badge>ACCEPTED</span>" in page
    assert SCHEDULE_BUTTON not in page
    _, _, queue = client.request("GET", "/")
    assert "Earlier Buffer submissions may still exist" in queue
    assert "ACCEPTED (approval withdrawn here)" in queue.split("<summary>Details</summary>", 1)[1]
    # The withdrawn approval can never be submitted again, from review or the manual command.
    code = refused(schedule_here, desk, work.sid, publisher, approval_id=approval.approval_id)
    assert code == (E.REVIEW_CLOSED if change == "reject" else R.NOT_APPROVED)
    code = publish_schedule_refusal(work, approval.approval_id, work.path("draft"))
    assert code == E.APPROVAL_WITHDRAWN
    assert publisher.built == 0


def test_with_scheduling_off_the_page_offers_no_schedule_action(work, serve):
    approval, decision = approve_current(work.desk(), work.sid)
    client = serve(work.desk())
    story = f"/story/{work.sid}"
    _, _, page = client.request("GET", story)
    assert "Scheduling from this page is off" in page and "publish-schedule --bundle" not in page
    assert SCHEDULE_BUTTON not in page and f"{work.sid}/schedule" not in page
    assert "Not submitted" in page and "scheduled for" not in page
    status, _, page = client.post(
        f"{story}/schedule",
        seq=str(decision.seq),
        approval_id=approval.approval_id,
        typed=schedule_phrase(approval.approval_id),
    )
    assert status == 403 and "SCHEDULING_DISABLED" in page
    assert intent_of(work, approval.approval_id) is None


def test_a_stop_file_refuses_approval_and_every_buffer_send_and_says_so(work, serve, tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("", encoding="utf-8")
    publisher = ScriptedPublisher(channel_reply(), create_ok())
    client = serve(work.desk(), scheduler=publisher, stopped=stop.exists)
    story = f"/story/{work.sid}"
    banner = escape(STOP_MESSAGE, quote=True)
    assert "Posts already scheduled in Buffer are not cancelled; pause them in Buffer" in banner
    _, _, queue = client.request("GET", "/")
    _, _, page = client.request("GET", story)
    assert banner in queue and banner in page and f"{work.sid}/sheet" not in page
    # Approve and its sheet are refused on the server, whatever the form says.
    seq = seq_of(page)
    status, _, page = client.post(f"{story}/sheet", seq=seq, channel_id=CHANNEL, due_at=DUE)
    assert status == 423 and banner in page
    candidate = work.desk().approval_candidate(work.sid, CHANNEL, DUE, NOW)
    status, _, page = client.post(
        f"{story}/approve",
        seq=seq,
        channel_id=CHANNEL,
        due_at=DUE,
        typed=confirmation_phrase(candidate.approval_id),
    )
    assert status == 423 and banner in page
    with Ledger.open(work.ledger, create=False) as ledger:
        assert ledger.review_decisions(work.sid) == []
    # An approval made before STOP is not sent (LinkedIn or X: both go through schedule).
    approval, decision = approve_current(work.desk(), work.sid)
    _, _, page = client.request("GET", story)
    assert banner in page and SCHEDULE_BUTTON not in page
    form = {"seq": str(decision.seq), "approval_id": approval.approval_id}
    phrase = schedule_phrase(approval.approval_id)
    status, _, page = client.post(f"{story}/schedule", typed=phrase, **form)
    assert status == 423 and banner in page
    assert publisher.built == 0 and publisher.transport.bodies == []
    assert intent_of(work, approval.approval_id) is None
    # Removing STOP while the page is open lets the owner schedule again.
    stop.unlink()
    _, _, page = client.request("GET", story)
    assert banner not in page and SCHEDULE_BUTTON in page
    status, _, _ = client.post(f"{story}/schedule", typed=phrase, **form)
    assert status == 303 and publisher.built == 1 and len(publisher.creates()) == 1


SCHEDULE_ATTACKS = [
    "no_session",
    "form_token",
    "origin",
    "fetch_site",
    "null_origin",
    "host",
    "stale",
    "get",
]


@pytest.mark.parametrize("attack", SCHEDULE_ATTACKS)
def test_the_schedule_action_has_every_request_protection(work, serve, attack):
    approval, decision = approve_current(work.desk(), work.sid)
    publisher = ScriptedPublisher()
    client = serve(work.desk(), scheduler=publisher)
    path = f"/story/{work.sid}/schedule"
    form = {
        "token": client.server.token,
        "seq": str(decision.seq),
        "approval_id": approval.approval_id,
        "typed": schedule_phrase(approval.approval_id),
    }
    headers, host, method = {}, None, "POST"
    if attack == "no_session":
        client.cookie = None
    elif attack == "form_token":
        form["token"] = "wrong"
    elif attack == "origin":
        headers = {"Origin": "http://evil.example"}
    elif attack == "fetch_site":
        headers = {"Sec-Fetch-Site": "cross-site"}
    elif attack == "null_origin":
        headers = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
    elif attack == "host":
        host = "localhost"
    elif attack == "stale":
        form["seq"] = str(decision.seq - 1)
    else:
        method, form = "GET", None
    status, _, _ = client.request(method, path, form, headers=headers, host=host)
    expected = {"host": 400, "stale": 409, "get": 404}.get(attack, 403)
    assert status == expected
    assert publisher.built == 0 and publisher.transport.bodies == []
    assert intent_of(work, approval.approval_id) is None


def test_the_complete_scheduling_workflow_through_the_local_interface(work, serve):
    publisher = ScriptedPublisher(channel_reply(), create_ok())
    client = serve(work.desk(), scheduler=publisher)
    story = f"/story/{work.sid}"
    # Viewing, the approval sheet and approval send nothing and build no publisher.
    client.request("GET", "/")
    _, _, page = client.request("GET", story)
    assert SCHEDULE_BUTTON not in page  # not approved yet
    seq = seq_of(page)
    _, _, page = client.post(f"{story}/sheet", seq=seq, channel_id=CHANNEL, due_at=DUE)
    assert "target time" in page and "Buffer has not scheduled it" in page
    assert "scheduled for" not in page
    confirmation = page.split('name=confirmation value="', 1)[1].split('"', 1)[0]
    status, _, _ = client.post(
        f"{story}/approve", seq=seq, confirmation=confirmation, confirm="yes"
    )
    assert status == 303 and publisher.built == 0
    # The approved page shows the exact post, channel, time and approval id, and the action.
    _, _, page = client.request("GET", story)
    with Ledger.open(work.ledger, create=False) as ledger:
        (decision,) = ledger.review_decisions(work.sid)
        approval = ledger.get_approval(decision.approval_id)
    schedule_box = page.split("<h2>Schedule with Buffer</h2>", 1)[1]
    for expected in (
        f"<pre>{escape(approval.text, quote=True)}</pre>",
        CHANNEL,
        DUE,
        approval.approval_id,
        "Not submitted",
        SCHEDULE_BUTTON,
    ):
        assert expected in schedule_box, expected
    assert "approved target time" in page and "scheduled for" not in page
    scheduling = page.split("To schedule, type exactly: <b>", 1)[1].split("</b>", 1)[0]
    assert scheduling == schedule_phrase(approval.approval_id)
    form = {"seq": seq_of(page), "approval_id": approval.approval_id}
    # A wrong phrase sends nothing.
    status, _, page = client.post(f"{story}/schedule", typed="SCHEDULE", **form)
    assert status == 409 and "SCHEDULE_NOT_CONFIRMED" in page and publisher.built == 0
    # The owner confirms: one destination read, one create, then a redirect.
    status, headers, _ = client.post(f"{story}/schedule", typed=scheduling, **form)
    assert status == 303 and headers["Location"].split("?", 1)[0] == story
    assert publisher.transport.operations() == ["query", "mutation"]
    (sent,) = publisher.creates()
    assert (sent["text"], sent["channelId"]) == (approval.text, CHANNEL)
    assert sent["dueAt"] == "2026-11-02T09:30:00.000Z"
    # Refreshing the page repeats the GET only; the state is ACCEPTED, not "scheduled".
    for _ in range(2):
        _, _, page = client.request("GET", story)
        assert "<span class=badge>ACCEPTED</span>" in page
        assert "not yet independently confirmed" in page and SCHEDULE_BUTTON not in page
        assert "Check the current status in Buffer" in page
    _, _, queue = client.request("GET", "/")
    assert "Buffer accepted; schedule not confirmed" in queue
    # The browser resubmits the confirmed form: refused before any request.
    status, _, page = client.post(f"{story}/schedule", typed=scheduling, **form)
    assert status == 409 and "DUPLICATE_SUBMISSION" in page
    assert len(publisher.transport.bodies) == 2
    # Restart: a new server over the same files shows the same intent and offers nothing.
    later = ScriptedPublisher()
    restarted = serve(work.desk(), scheduler=later)
    _, _, page = restarted.request("GET", story)
    assert "<span class=badge>ACCEPTED</span>" in page and SCHEDULE_BUTTON not in page
    status, _, _ = restarted.post(f"{story}/schedule", typed=scheduling, **form)
    assert status == 409 and later.transport.bodies == []
    # Only publish-reconcile's read-back makes it SCHEDULED, and the page says so.
    readback = FakeTransport(post_reply(text=approval.text))
    with Ledger.open(work.ledger, create=False) as ledger:
        (result,) = reconcile(ledger, BufferPublisher(readback), NOW)
    assert result.after == IntentState.SCHEDULED
    _, _, page = restarted.request("GET", story)
    assert escape(INTENT_GUIDANCE[IntentState.SCHEDULED], quote=True) in page


# --- source boundaries -----------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_review_cannot_reach_buffer_the_model_or_the_environment():
    paths = sorted(REVIEW.glob("*.py"))
    assert {p.name for p in paths} == {
        "__init__.py",
        "gate.py",
        "pages.py",
        "required.py",
        "server.py",
        "service.py",
        "ux.py",
        "x_post.py",
    }
    for path in paths:
        imported = _imports(path)
        assert "public_research_os.publication.buffer" not in imported, path.name
        assert "public_research_os.drafting.provider" not in imported, path.name
        assert not any(name.split(".")[0] in {"anthropic", "subprocess"} for name in imported)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"environ", "getenv", "system", "popen"}, path.name


def test_only_the_review_server_imports_http_and_only_to_serve_locally():
    server = REVIEW / "server.py"
    network = {name for name in _imports(server) if name.split(".")[0] in {"http", "urllib"}}
    assert network == {"http", "http.server", "urllib.parse"}
    assert server_module.HOST == "127.0.0.1"
    source = server.read_text(encoding="utf-8")
    assert "0.0.0.0" not in source and "client" not in network


# --- X as a second channel (Step 6) ------------------------------------------------------------

X_CHANNEL = "d4" * 12
X_POST_ID = "e5" * 12


def x_desk(work: Work) -> ReviewDesk:
    return ReviewDesk(
        work.drafts,
        work.backlog,
        work.ledger,
        (work.snapshot,),
        x_channel_id=X_CHANNEL,
        fact_checks=work.desk().fact_checks,
    )


def approve_both(desk: ReviewDesk, sid: str, x_seen: str | None = None):
    """What the page does: the sheet (LinkedIn and X together), then Approve with its digest."""
    item = desk.item(sid)
    candidate = desk.approval_candidate(sid, CHANNEL, DUE, NOW)
    shown = x_digest(desk.x_sheet(sid, candidate)) if x_seen is None else x_seen
    typed = confirmation_phrase(candidate.approval_id)
    return desk.approve(sid, CHANNEL, DUE, typed, item.seq, NOW, shown)


def schedule_both(desk: ReviewDesk, sid: str, approval_id: str, transport: FakeTransport):
    item = desk.item(sid)
    return desk.schedule(
        sid,
        approval_id,
        schedule_phrase(approval_id),
        item.seq,
        lambda: BufferPublisher(transport),
        NOW,
    )


def creates(transport: FakeTransport) -> list[dict]:
    bodies = [json.loads(body) for body in transport.bodies]
    return [b["variables"]["input"] for b in bodies if b["query"].startswith("mutation")]


def x_candidate(state: XState) -> Candidate:
    approval = state.approval
    return Candidate(
        approval.text,
        approval.channel_id,
        approval.due_at,
        approval.workspace_id,
        approval.bundle_record_sha256,
    )


def test_one_approve_records_the_linkedin_and_the_x_post_together(work):
    desk = x_desk(work)
    approval, _ = approve_both(desk, work.sid)
    state = desk.x_state(desk.item(work.sid))
    assert state.post.text is not None and state.intent is None
    assert state.approval.text == state.post.text
    assert state.approval.channel_id == X_CHANNEL
    assert state.approval.due_at == approval.due_at
    assert state.approval.bundle_record_sha256 == approval.bundle_record_sha256
    # The short synthetic post fits whole: its two sentences and its limitation line.
    assert state.post.text == approval.text and state.post.segment_ids == ("S01", "S02", "S03")


def test_an_approve_from_a_sheet_showing_another_x_text_records_nothing(work):
    desk = x_desk(work)
    assert refused(approve_both, desk, work.sid, "none") == E.REVIEW_STALE
    assert refused(approve_both, desk, work.sid, "") == E.REVIEW_STALE
    with Ledger.open(work.ledger, create=False) as ledger:
        assert ledger.review_decisions(work.sid) == []
    assert desk.item(work.sid).state == ReviewState.UNAPPROVED


def test_scheduling_sends_linkedin_then_x_in_the_same_slot(work):
    desk = x_desk(work)
    approval, _ = approve_both(desk, work.sid)
    transport = FakeTransport(
        channel_reply(),
        create_ok(),
        channel_reply(channel=X_CHANNEL, service="twitter"),
        create_ok(X_POST_ID),
    )
    schedule_both(desk, work.sid, approval.approval_id, transport)
    linkedin, x = creates(transport)
    state = desk.x_state(desk.item(work.sid))
    assert (linkedin["channelId"], x["channelId"]) == (CHANNEL, X_CHANNEL)
    assert linkedin["dueAt"] == x["dueAt"] == "2026-11-02T09:30:00.000Z"
    assert linkedin["text"] == approval.text and x["text"] == state.post.text
    assert state.intent.state == IntentState.ACCEPTED
    assert state.intent.provider_post_id == X_POST_ID
    assert desk.item(work.sid).intent.state == IntentState.ACCEPTED
    # Nothing is offered or sent twice.
    assert refused(schedule_both, desk, work.sid, approval.approval_id, FakeTransport()) == (
        E.DUPLICATE_SUBMISSION
    )


def test_x_gets_nothing_when_buffer_refuses_the_linkedin_post(work):
    desk = x_desk(work)
    approval, _ = approve_both(desk, work.sid)
    transport = FakeTransport(channel_reply(), create_error("InvalidInputError"))
    result = schedule_both(desk, work.sid, approval.approval_id, transport)
    assert result.intent.state == IntentState.REJECTED
    assert len(transport.bodies) == 2  # the LinkedIn check and create only
    assert desk.x_state(desk.item(work.sid)).intent is None
    again = FakeTransport()
    assert refused(schedule_both, desk, work.sid, approval.approval_id, again) == (
        E.LEAD_NOT_ACCEPTED
    )
    assert again.bodies == []


def test_an_x_post_refused_before_sending_is_sent_alone_later(work):
    desk = x_desk(work)
    approval, _ = approve_both(desk, work.sid)
    first = FakeTransport(channel_reply(), create_ok(), channel_reply(channel=X_CHANNEL))
    code = refused(schedule_both, desk, work.sid, approval.approval_id, first)
    assert code == E.DESTINATION_MISMATCH  # the X channel answered as a LinkedIn channel
    assert desk.item(work.sid).intent.state == IntentState.ACCEPTED
    assert desk.x_state(desk.item(work.sid)).intent is None
    second = FakeTransport(
        channel_reply(channel=X_CHANNEL, service="twitter"), create_ok(X_POST_ID)
    )
    schedule_both(desk, work.sid, approval.approval_id, second)
    assert [c["channelId"] for c in creates(second)] == [X_CHANNEL]  # LinkedIn is not resent
    assert desk.x_state(desk.item(work.sid)).intent.state == IntentState.ACCEPTED


def test_submit_refuses_an_x_post_whose_linkedin_post_was_not_sent(work):
    desk = x_desk(work)
    approval, _ = approve_both(desk, work.sid)
    state = desk.x_state(desk.item(work.sid))
    transport = FakeTransport()
    with Ledger.open(work.ledger, create=False) as ledger:
        code = refused(
            submit,
            ledger,
            BufferPublisher(transport),
            x_candidate(state),
            state.approval.approval_id,
            NOW,
            service="twitter",
            lead_approval_id=approval.approval_id,
        )
        assert ledger.get_intent(state.approval.approval_id) is None
    assert code == E.LEAD_NOT_ACCEPTED and transport.bodies == []


def test_an_edit_committed_during_the_x_destination_check_stops_the_x_send(work):
    desk = x_desk(work)
    approval, approved = approve_both(desk, work.sid)
    state = desk.x_state(desk.item(work.sid))
    candidate = load_candidate(work.path("draft"), CHANNEL, DUE, NOW)
    with Ledger.open(work.ledger, create=False) as ledger:
        lead = FakeTransport(channel_reply(), create_ok())
        submit(ledger, BufferPublisher(lead), candidate, approval.approval_id, NOW)
    other = x_desk(work)

    def edit():
        other.save_edit(work.sid, texts(S01=EDITED_S01), approved.seq, NOW)

    transport = FakeTransport(channel_reply(channel=X_CHANNEL, service="twitter"))
    with Ledger.open(work.ledger, create=False) as ledger:
        provider_ = _DecidesDuringCheck(BufferPublisher(transport), edit)
        code = refused(
            submit,
            ledger,
            provider_,
            x_candidate(state),
            state.approval.approval_id,
            NOW,
            service="twitter",
            lead_approval_id=approval.approval_id,
        )
        assert ledger.get_intent(state.approval.approval_id) is None
    assert code == E.APPROVAL_WITHDRAWN
    assert len(transport.bodies) == 1  # the X destination check only; nothing was created


def test_without_an_x_channel_scheduling_sends_linkedin_only(work):
    desk = work.desk()
    approval, _ = approve_current(desk, work.sid)
    assert desk.x_state(desk.item(work.sid)).post.reason == review_service.X_OFF
    transport = FakeTransport(channel_reply(), create_ok())
    schedule_both(desk, work.sid, approval.approval_id, transport)
    assert [c["channelId"] for c in creates(transport)] == [CHANNEL]
    with Ledger.open(work.ledger, create=False) as ledger:
        assert len(ledger.list_intents()) == 1


def test_the_sheet_shows_the_linkedin_and_the_x_text_together_and_binds_the_x_text(work):
    desk = x_desk(work)
    item = desk.item(work.sid)
    candidate = desk.approval_candidate(work.sid, CHANNEL, DUE, NOW)
    post = desk.x_sheet(work.sid, candidate)
    page = story_page(
        item,
        "tok",
        StoryView(sheet=candidate, x_sheet=post, confirmation="synthetic-confirmation"),
        None,
    )
    assert "<h3>LinkedIn</h3>" in page and "<h3>X (same slot, after LinkedIn)</h3>" in page
    assert escape(post.text.split("\n\n")[0]) in page
    assert 'name=confirmation value="synthetic-confirmation"' in page
    assert "name=x_seen" not in page
    nothing = story_page(
        item,
        "tok",
        StoryView(
            sheet=candidate,
            x_sheet=XPost(None, "scope line doesn't fit"),
            confirmation="synthetic-confirmation",
        ),
        None,
    )
    assert "<b>X: nothing</b>, scope line doesn&#x27;t fit." in nothing
    assert "name=x_seen" not in nothing


def test_reconcile_reads_the_x_post_back_like_the_linkedin_post(work):
    desk = x_desk(work)
    approval, _ = approve_both(desk, work.sid)
    send = FakeTransport(
        channel_reply(),
        create_ok(),
        channel_reply(channel=X_CHANNEL, service="twitter"),
        create_ok(X_POST_ID),
    )
    schedule_both(desk, work.sid, approval.approval_id, send)
    state = desk.x_state(desk.item(work.sid))
    replies = {
        approval.approval_id: post_reply(text=approval.text),
        state.approval.approval_id: post_reply(
            post_id=X_POST_ID, channel=X_CHANNEL, text=state.post.text
        ),
    }
    # Reconcile reads intents by (created_at, approval_id); both share created_at here.
    reads = FakeTransport(*(replies[key] for key in sorted(replies)))
    with Ledger.open(work.ledger, create=False) as ledger:
        done = {r.approval_id: r.after for r in reconcile(ledger, BufferPublisher(reads), NOW)}
    assert done == {
        approval.approval_id: IntentState.SCHEDULED,
        state.approval.approval_id: IntentState.SCHEDULED,
    }
