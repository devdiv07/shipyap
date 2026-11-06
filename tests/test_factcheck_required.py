"""Fact-check completion is required at the server-side approval boundary."""

from dataclasses import replace

import pytest
from test_review import CHANNEL, DUE, NOW, R, refused
from test_review import serve as serve
from test_review import work as work

from public_research_os.review.pages import StoryView, queue_page, story_page


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"verdicts": []},
        {"verdicts": [{"segment_id": [], "verdict": "SUPPORTED", "reason": "x", "refs": []}]},
        {"verdicts": [{"segment_id": "S01", "verdict": [], "reason": "x", "refs": 5}]},
    ],
)
def test_unchecked_or_corrupt_record_is_visible_but_never_approvable(work, record):
    desk = work.desk()
    if record is not None:
        record["draft"] = work.path("draft").name
    desk.fact_checks = lambda _: record
    items, _ = desk.queue()
    assert [item.story.story_id for item in items] == [work.sid]
    item = items[0]
    gate = desk.gate(item)
    assert not gate.approvable
    assert "Not fact-checked - cannot approve" in queue_page(items, [], gates={work.sid: gate})
    page = story_page(item, "token", StoryView(), None, gate=gate, fact_check=desk.fact_check(item))
    assert "<p class=status id=decision tabindex=-1>Not fact-checked - cannot approve</p>" in page
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == R.APPROVAL_BLOCKED
    assert refused(desk.approve, work.sid, CHANNEL, DUE, "forged", 0, NOW) == R.APPROVAL_BLOCKED


def test_forged_http_approval_with_no_fact_check_is_denied(work, serve):
    desk = work.desk()
    desk.fact_checks = lambda _: None
    client = serve(desk)
    code, _, _ = client.post(
        f"/story/{work.sid}/approve", seq="0", confirmation="forged", confirm="yes"
    )
    assert code != 303
    assert desk.item(work.sid).seq == 0


def test_scripted_sample_check_cannot_approve_a_real_draft(work):
    desk = work.desk()
    valid = desk.fact_checks(work.sid)
    valid["label"] = "SAMPLE fact check (scripted)"
    desk.fact_checks = lambda _: valid
    assert not desk.gate(desk.item(work.sid)).approvable
    assert refused(desk.approval_candidate, work.sid, CHANNEL, DUE, NOW) == R.APPROVAL_BLOCKED
    repaired = replace(desk.item(work.sid), repair={"fact_check": valid})
    assert desk.fact_check(repaired) is None
