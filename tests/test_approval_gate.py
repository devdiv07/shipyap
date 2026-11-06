"""Step 3: only real errors block Approve. A sentence repeating a plan that a later record of the
same revision changed is OUTDATED; the fact check's CONTRADICTED and OUTDATED block, its
OVERSTATED and UNVERIFIABLE are warnings with their lines; edits re-run the mechanical checks and
leave the model's verdict "not re-checked"; every decision saves one eval case. Synthetic files,
offline, through the real review desk."""

import json
from html import escape

import pytest
from editorial_fake import seg
from publication_fake import CHANNEL, DUE, NOW
from stories_fake import HISTORIC_FILES, make_snapshot, ref
from test_review import SEGMENTS, Work, approve_current, refused, texts

from shipyap.automation import evaluation
from shipyap.editorial.supersession import (
    CHANGED_ITEM,
    STATUS_LINE,
    Record,
    check_superseded,
)
from shipyap.review.gate import Gate, Note, SegmentGate
from shipyap.review.pages import StoryView, story_page
from shipyap.review.service import ReviewDesk, ReviewErrorCode, reject_phrase

R = ReviewErrorCode
PLAN_FILES = {
    **HISTORIC_FILES,
    "docs/PROTOCOL.md": (
        b"# Protocol\n"  # 1
        b"\n"  # 2
        b"Status: harness built; zero real requests made.\n"  # 3
        b"The adapter sends `timeout` on every call.\n"  # 4
    ),
}
DEVIATION = {
    "evidence/DEVIATION_001.md": (
        b"# Deviation 001 - timeout removed\n"  # 1
        b"\n"  # 2
        b"The harness no longer sends `timeout`; the live service rejected it.\n"  # 3
    )
}
PLAN_SEGMENTS = (
    SEGMENTS[0],
    seg(
        "S02",
        "The adapter sends a timeout on every call.",
        "SOURCE",
        src=[ref("docs/PROTOCOL.md", 4, 4)],
    ),
    SEGMENTS[2],
)
PLAN_TEXTS = {s["id"]: s["text"] for s in PLAN_SEGMENTS}


class PlanWork(Work):
    """A draft whose S02 repeats the plan; a second snapshot of the same revision holds the
    deviation record that changed it."""

    def __init__(self, root):
        super().__init__(root, segments=PLAN_SEGMENTS, files=PLAN_FILES)
        self.records = make_snapshot(root, "records", files={**PLAN_FILES, **DEVIATION})

    def desk(self, snapshots: bool = True, **kwargs) -> ReviewDesk:
        inputs = (self.snapshot, self.records) if snapshots else (self.snapshot,)
        kwargs.setdefault("fact_checks", super().desk().fact_checks)
        return ReviewDesk(self.drafts, self.backlog, self.ledger, inputs, **kwargs)


@pytest.fixture
def plan(tmp_path):
    return PlanWork(tmp_path)


def test_a_sentence_repeating_a_changed_plan_item_blocks_approval_beside_its_record(plan):
    desk = plan.desk()
    item = desk.item(plan.sid)
    gate = desk.gate(item)
    assert not gate.approvable
    (blocked,) = gate.blocked()
    assert blocked.segment_id == "S02"
    (note,) = blocked.blocks
    assert note.code == "OUTDATED" and note.refs == ("evidence/DEVIATION_001.md:1",)
    with pytest.raises(Exception) as raised:
        desk.approval_candidate(plan.sid, CHANNEL, DUE, NOW)
    assert raised.value.code == R.APPROVAL_BLOCKED
    assert any("evidence/DEVIATION_001.md:1" in line for line in raised.value.details)
    page = story_page(item, "t", StoryView(), None, gate=gate)
    assert "BLOCKS OUTDATED" in page and "evidence/DEVIATION_001.md:1" in page
    assert "<button disabled>Approve</button>" in page
    assert "A later record supersedes what this sentence says" in page
    assert "Show the approval sheet" not in page
    # Without the later record nothing says the plan changed: the gate cannot block on it.
    assert plan.desk(snapshots=False).gate(item).approvable


def test_editing_the_outdated_sentence_reruns_the_mechanical_checks(plan, tmp_path):
    desk = plan.desk(eval_cases=tmp_path / "cases")
    fixed = {**PLAN_TEXTS, "S02": "The protocol specifies an adapter setting for every call."}
    preview = desk.preview(desk.item(plan.sid), fixed)
    assert preview.gate is not None and preview.gate.approvable
    still = {**PLAN_TEXTS, "S02": "The protocol specifies a timeout of 40 seconds per call."}
    blocked = desk.preview(desk.item(plan.sid), still).gate
    codes = {note.code for s in blocked.segments for note in s.blocks}
    assert codes == {"OUTDATED", "NUMBER_NOT_IN_EVIDENCE"}
    desk.save_edit(plan.sid, fixed, 0, NOW)
    assert desk.gate(desk.item(plan.sid)).approvable
    approval, _ = approve_current(desk, plan.sid)
    assert approval.text.count("adapter setting") == 1


def test_the_status_line_and_change_record_rules_are_generic():
    brief_lines = PLAN_FILES["docs/PROTOCOL.md"].decode().splitlines()

    class Source:
        def __init__(self, path, lines):
            self.path, self.lines = path, tuple(lines)

    class Brief:
        sources = (Source("docs/PROTOCOL.md", brief_lines),)

        def source(self, path):
            return next((s for s in self.sources if s.path == path), None)

    class Segment:
        def __init__(self, sid, text, start, path="docs/PROTOCOL.md"):
            self.id, self.text = sid, text
            self.source_refs = (
                type("R", (), {"path": path, "line_start": start, "line_end": start}),
            )

    class Response:
        segments = (
            Segment("S01", "No real requests had been made.", 3),
            Segment("S02", "The adapter sends a timeout.", 4),
            Segment("S03", "The adapter sends a timeout.", 4, path="README.md"),  # not a plan
        )

    deviation = Record(
        "evidence/DEVIATION_001.md",
        tuple(DEVIATION["evidence/DEVIATION_001.md"].decode().splitlines()),
    )
    found = {s.segment_id: s for s in check_superseded(Brief(), Response(), [deviation])}
    assert set(found) == {"S01", "S02"}
    assert found["S01"].rule == STATUS_LINE and found["S02"].rule == CHANGED_ITEM
    assert found["S02"].item == "timeout"
    assert check_superseded(Brief(), Response(), []) == ()
    # A results record is later evidence (the status line is dated) but not a change record.
    results = Record("evidence/RESULTS.md", ("# Results - timeout", "", "`timeout` was kept."))
    found = {s.segment_id: s.rule for s in check_superseded(Brief(), Response(), [results])}
    assert found == {"S01": STATUS_LINE}
    # The record's own path words are not changed items.
    named = Record("evidence/timeout/DEVIATION_002.md", ("# Deviation 002 - timeout", "`timeout`"))
    found = {s.segment_id: s.rule for s in check_superseded(Brief(), Response(), [named])}
    assert found == {"S01": STATUS_LINE}


def _verdicts(work, *verdicts):
    origin = work.path("draft").name
    return lambda _sid: {
        "draft": origin,
        "verdicts": [
            {"segment_id": sid, "verdict": v, "reason": f"{v} reason", "refs": refs}
            for sid, v, refs in verdicts
        ],
    }


def test_contradicted_blocks_and_overstated_warns_with_its_lines(tmp_path):
    work = Work(tmp_path)
    checks = _verdicts(
        work,
        ("S01", "CONTRADICTED", ["README.md:5-5"]),
        ("S02", "OVERSTATED", ["README.md:4-4"]),
        ("S03", "SUPPORTED", []),
    )
    desk = ReviewDesk(work.drafts, work.backlog, work.ledger, (work.snapshot,), fact_checks=checks)
    item = desk.item(work.sid)
    gate = desk.gate(item)
    assert [s.segment_id for s in gate.blocked()] == ["S01"]
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == R.APPROVAL_BLOCKED
    page = story_page(item, "t", StoryView(), None, gate=gate, fact_check=desk.fact_check(item))
    assert "WARNING FACT_CHECK_OVERSTATED: OVERSTATED reason" in page and "README.md:4-4" in page
    # An edit leaves the model's verdict "not re-checked"; the mechanical checks judge it.
    desk.save_edit(
        work.sid,
        texts(S01="A screenshot shows capture, not that the right scene was shown."),
        0,
        NOW,
    )
    gate = desk.gate(desk.item(work.sid))
    s01 = next(s for s in gate.segments if s.segment_id == "S01")
    assert s01.blocks == () and s01.verdict is None
    assert [n.code for n in s01.warnings] == ["NOT_RECHECKED"]
    assert gate.approvable
    approve_current(desk, work.sid)


def test_a_fact_check_of_another_draft_or_without_verdicts_never_applies(tmp_path):
    work = Work(tmp_path)

    def other(_sid):
        return {
            "draft": "other-draft",
            "verdicts": [{"segment_id": "S01", "verdict": "CONTRADICTED"}],
        }

    desk = ReviewDesk(work.drafts, work.backlog, work.ledger, (work.snapshot,), fact_checks=other)
    assert desk.fact_check(desk.item(work.sid)) is None
    assert not desk.gate(desk.item(work.sid)).approvable


def test_one_edit_creates_one_eval_case_and_each_decision_one_more(tmp_path):
    work = Work(tmp_path)
    cases = tmp_path / "ws" / "automation" / "eval-cases"
    desk = ReviewDesk(
        work.drafts,
        work.backlog,
        work.ledger,
        (work.snapshot,),
        eval_cases=cases,
        fact_checks=work.desk().fact_checks,
    )
    edited = "A screenshot shows capture, not that the right scene was shown."
    desk.save_edit(work.sid, texts(S01=edited), 0, NOW)
    (only,) = cases.iterdir()
    case = json.loads(only.read_text(encoding="utf-8"))
    assert case["decision"] == "REVISED" and case["seq"] == 1
    s01, s02, _ = case["segments"]
    assert s01["edited"] and s01["text"] == SEGMENTS[0]["text"] and s01["owner_text"] == edited
    assert not s02["edited"] and s02["evidence"][0]["ref"] == "README.md:4-4"
    assert case["approvable_before"] is True
    approve_current(desk, work.sid)
    desk.reject(work.sid, reject_phrase(work.sid), 2, NOW)
    names = sorted(p.name for p in cases.iterdir())
    assert [n.rsplit("-", 1)[1] for n in names] == [
        "REVISED.json",
        "APPROVED.json",
        "REJECTED.json",
    ]
    decisions = evaluation.load_decisions(tmp_path / "ws")
    assert [(d.decision, d.edited) for d in decisions] == [
        ("REVISED", 1),
        ("APPROVED", 0),
        ("REJECTED", 0),
    ]
    report = evaluation.evaluate([], [], decisions)
    assert "decisions=3" in evaluation.render(report)[-1]


def test_a_case_that_cannot_be_written_is_reported_after_the_decision_is_recorded(tmp_path):
    work = Work(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    desk = ReviewDesk(work.drafts, work.backlog, work.ledger, (work.snapshot,), eval_cases=blocker)
    edited = "A screenshot shows capture, not that the right scene was shown."
    assert refused(desk.save_edit, work.sid, texts(S01=edited), 0, NOW) == R.EVAL_CASE_NOT_SAVED
    assert desk.item(work.sid).seq == 1  # the decision itself is recorded


def test_first_person_is_shown_beside_the_authors_statement(tmp_path):
    work = Work(tmp_path)
    item = work.desk().item(work.sid)
    gate = Gate(
        tuple(
            SegmentGate(
                s.id, warnings=(Note("FIRST_PERSON_FROM_STATEMENT", "I"),) if s.id == "S01" else ()
            )
            for s in item.current.response.segments
        )
    )
    page = story_page(item, "t", StoryView(), None, gate=gate)
    statement = item.current.brief.contribution.attempted[0]
    assert "WARNING FIRST_PERSON_FROM_STATEMENT" in page
    assert f"Author statement: {escape(statement)}" in page
    assert "Nothing blocks approval" in page


def _older_checker(monkeypatch):
    """Bundles built while this is active carry the map an older rule would write: one more
    blocking finding on the first segment, with the classification, question, counts and status
    that follow from it (consistent throughout, as a real older checker's map is)."""
    from shipyap.editorial import grounding
    from shipyap.editorial import service as editorial_service
    from shipyap.editorial.models import (
        Classification,
        DraftStatus,
        Finding,
        FindingCode,
        Severity,
    )

    current = grounding.check_draft

    def older(*args, **kwargs):
        amap = current(*args, **kwargs)
        first = amap.segments[0]
        extra = Finding(
            code=FindingCode.FIRST_PERSON_UNATTRIBUTED,
            severity=Severity.BLOCKING,
            segment_id=first.id,
            message="first person without a contributor reference",
            detail="I",
        )
        blocked = first.model_copy(
            update={
                "findings": (*first.findings, extra),
                "classification": Classification.UNSUPPORTED,
                "review_question": grounding.REVIEW_QUESTIONS[Classification.UNSUPPORTED],
            }
        )
        field = {Classification.SOURCE_REFERENCED: "source_referenced"}[first.classification]
        counts = amap.counts.model_copy(
            update={
                field: getattr(amap.counts, field) - 1,
                "unsupported": amap.counts.unsupported + 1,
                "blocking": amap.counts.blocking + 1,
            }
        )
        return amap.model_copy(
            update={
                "segments": (blocked, *amap.segments[1:]),
                "counts": counts,
                "draft_status": DraftStatus.NEEDS_REVISION,
            }
        )

    monkeypatch.setattr(editorial_service, "check_draft", older)


def test_a_bundle_checked_under_older_rules_is_stale_rejectable_and_never_approvable(
    tmp_path, monkeypatch
):
    from shipyap.review.service import STALE_MESSAGE

    with monkeypatch.context() as patched:
        _older_checker(patched)
        work = Work(tmp_path)  # built by the real story-draft path under the "older" rule
    cases = tmp_path / "cases"
    desk = ReviewDesk(work.drafts, work.backlog, work.ledger, (work.snapshot,), eval_cases=cases)
    items, refusals = desk.queue()
    assert refusals == [] and items[0].stale
    item = desk.item(work.sid)
    assert item.current.amap.draft_status.value == "READY_FOR_REVIEW"  # the current rules' view
    assert not desk.gate(item).approvable
    page = story_page(item, "t", StoryView(), None, gate=desk.gate(item))
    assert f"STALE: {escape(STALE_MESSAGE)}" in page and "<h2>Edit</h2>" not in page
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == R.STALE_DRAFT
    assert refused(approve_current, desk, work.sid) == R.STALE_DRAFT
    edited = "A screenshot shows capture, not that the right scene was shown."
    assert refused(desk.save_edit, work.sid, texts(S01=edited), 0, NOW) == R.STALE_DRAFT
    desk.reject(work.sid, reject_phrase(work.sid), 0, NOW)
    (case,) = (json.loads(p.read_text(encoding="utf-8")) for p in cases.iterdir())
    assert case["decision"] == "REJECTED" and case["approvable_before"] is False
