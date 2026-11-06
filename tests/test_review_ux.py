"""Plain review UI and exact, owner-confirmed HTTP approval over synthetic fixtures."""

from dataclasses import replace
from html import unescape
from html.parser import HTMLParser

import pytest
from test_review import (
    CHANNEL,
    DUE,
    NOW,
    approve_current,
    texts,
)
from test_review import (
    counted as counted,
)
from test_review import (
    serve as serve,
)
from test_review import (
    work as work,
)

from public_research_os.editorial.models import FindingCode
from public_research_os.publication.ledger import Ledger
from public_research_os.review.pages import DEMO_GUIDE, StoryView, queue_page, story_page
from public_research_os.review.ux import REASONS, attention_count, status


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and "name" in attrs:
            self.values[attrs["name"]] = attrs.get("value", "")


def sheet(client, sid, seq="0", **changes):
    code, _, html = client.post(
        f"/story/{sid}/sheet", seq=seq, channel_id=CHANNEL, due_at=DUE, **changes
    )
    assert code == 200 and "Approve this exact post?" in html
    assert "To approve, type exactly" not in html and "name=typed" not in html.split("id=reject")[0]
    return Fields(html).values["confirmation"]


def confirm(client, sid, key, seq="0", **changes):
    return client.post(f"/story/{sid}/approve", seq=seq, confirmation=key, confirm="yes", **changes)


def test_all_mechanical_findings_have_a_plain_reason():
    assert set(FindingCode) <= REASONS.keys()


def test_confirm_binds_exact_text_destination_time_and_is_idempotent(work, serve):
    client = serve(work.desk())
    key = sheet(client, work.sid)
    assert confirm(client, work.sid, key)[0] == 303
    assert confirm(client, work.sid, key)[0] == 303
    with Ledger.open(work.ledger, create=False) as ledger:
        decisions = ledger.review_decisions(work.sid)
        assert len(decisions) == 1
        approval = ledger.get_approval(decisions[0].approval_id)
        assert approval.channel_id == CHANNEL and approval.due_at == DUE
        assert approval.text == work.desk().item(work.sid).current.text
        assert ledger.list_intents() == []


@pytest.mark.parametrize(
    "extra",
    [
        {"typed": "APPROVE"},
        {"channel_id": "f" * 24},
        {"due_at": "2030-01-01T00:00:00Z"},
        {"x_seen": "forged"},
    ],
)
def test_browser_cannot_replace_server_held_candidate(work, serve, extra):
    client = serve(work.desk())
    key = sheet(client, work.sid)
    assert confirm(client, work.sid, key, **extra)[0] == 409
    assert work.desk().item(work.sid).seq == 0


def test_unknown_cancelled_replaced_and_other_session_confirmations_fail(work, serve):
    client = serve(work.desk())
    assert confirm(client, work.sid, "unknown")[0] == 409
    old = sheet(client, work.sid)
    key = sheet(client, work.sid)
    assert key != old and confirm(client, work.sid, old)[0] == 409
    other = serve(work.desk())
    assert confirm(other, work.sid, key)[0] == 409
    assert client.post(f"/story/{work.sid}/cancel", seq="0", confirmation=key)[0] == 303
    assert confirm(client, work.sid, key)[0] == 409
    assert work.desk().item(work.sid).seq == 0


def test_confirmation_cannot_approve_an_edited_bundle(work, serve):
    client = serve(work.desk())
    key = sheet(client, work.sid)
    desk = work.desk()
    desk.save_edit(work.sid, texts(S01="A synthetic revised introduction."), 0, NOW)
    assert confirm(client, work.sid, key)[0] == 409
    assert work.desk().item(work.sid).approval is None


def test_stop_after_sheet_still_refuses_confirmation(work, serve):
    stopped = []
    client = serve(work.desk(), stopped=lambda: bool(stopped))
    key = sheet(client, work.sid)
    stopped.append(True)
    assert confirm(client, work.sid, key)[0] == 423
    assert work.desk().item(work.sid).seq == 0


def test_blocked_sentence_has_plain_reason_and_numbered_citations(counted, serve):
    client = serve(counted.desk())
    code, _, html = client.request("GET", f"/story/{counted.sid}")
    assert code == 200 and "Must fix" in html and "Check this" in html
    assert "A number is missing" in html and "checked against quoted words only" in html
    assert "<button disabled>Approve</button>" in html
    assert (
        html.index("class=post")
        < html.index("class=status")
        < html.index('class="panel flagged"')
        < html.index("id=details")
    )
    main = html.split("<details id=details")[0]
    assert "NUMBER_NOT_IN_EVIDENCE" not in main and "QUOTE_LOCATED" not in main


def test_details_are_closed_and_no_refresh_or_initial_focus(work, serve):
    client = serve(work.desk())
    _, headers, html = client.request("GET", f"/story/{work.sid}")
    assert "<details id=details><summary>Details</summary>" in html
    assert "autofocus" not in html and "Refresh" not in headers
    assert "<script" not in html and "http-equiv" not in html
    for obsolete in ("review-serve", "--enable-scheduling", "publish-", "story-draft"):
        assert obsolete not in html


def test_edit_and_reject_errors_keep_inputs_open_and_targeted(work, serve):
    client = serve(work.desk())
    path = f"/story/{work.sid}"
    code, _, html = client.post(path + "/reject", seq="0", typed="unfinished typing")
    assert code == 409 and "<details id=reject open>" in html
    assert 'value="unfinished typing"' in html and "autofocus" in html
    fields = {f"seg-{key}": value for key, value in texts(S01="Synthetic preview text.").items()}
    code, _, html = client.post(path + "/edit", seq="0", action="check", **fields)
    assert code == 200 and "<details id=details open>" in html
    assert "<textarea autofocus" in html and "Synthetic preview text." in html
    code, headers, _ = client.post(path + "/edit", seq="0", action="save", **fields)
    assert code == 303 and headers["Location"].endswith("?panel=edit#edit")
    _, _, html = client.request("GET", headers["Location"])
    assert "<details id=details open>" in html and "<textarea autofocus" in html


def test_queue_uses_plain_status_newest_first_and_ids_only_in_details(work):
    desk = work.desk()
    item = desk.item(work.sid)
    older = replace(
        item,
        story=replace(
            item.story,
            story_id="a" * 64,
            offered_at="2020-01-01T00:00:00Z",
            latest=replace(item.story.latest, angle="Older draft"),
        ),
    )
    newer = replace(
        item,
        story=replace(
            item.story,
            story_id="b" * 64,
            offered_at="2026-01-01T00:00:00Z",
            latest=replace(item.story.latest, angle="Newer draft"),
        ),
    )
    html = queue_page([older, newer], [])
    assert html.index("Newer draft") < html.index("Older draft")
    assert "<details><summary>Details</summary><p>Story id:" in html
    assert status(item, desk.gate(item)) in ("Ready to approve", "Needs attention")
    approve_current(desk, work.sid)
    assert status(desk.item(work.sid)) == "Approved, not scheduled"


def test_demo_guide_is_demo_only_and_example_is_in_details(work):
    item = work.desk().item(work.sid)
    assert DEMO_GUIDE not in story_page(item, "token", StoryView(), None)
    demo = story_page(item, "token", StoryView(), None, demo=True)
    assert DEMO_GUIDE in unescape(demo) and unescape(demo).index(DEMO_GUIDE) < demo.index(
        "<h1>Post</h1>"
    )
    assert "scheduling is disabled" in demo


def test_status_count_groups_multiple_notes_on_one_sentence(work):
    gate = work.desk().gate(work.desk().item(work.sid))
    assert attention_count(gate) == sum(bool(s.blocks or s.warnings) for s in gate.segments) + bool(
        gate.document_blocks or gate.document_warnings
    )


def test_changed_x_text_invalidates_the_confirmation(work, serve, monkeypatch):
    from public_research_os.review.x_post import XPost

    client = serve(work.desk())
    key = sheet(client, work.sid)
    monkeypatch.setattr(
        client.server.desk, "x_sheet", lambda *args: XPost("Changed synthetic X text", None)
    )
    assert confirm(client, work.sid, key)[0] == 409
    assert work.desk().item(work.sid).approval is None


def test_successful_confirmation_cannot_be_reused_after_rejection(work, serve):
    from public_research_os.review.service import reject_phrase

    client = serve(work.desk())
    key = sheet(client, work.sid)
    assert confirm(client, work.sid, key)[0] == 303
    desk = work.desk()
    desk.reject(work.sid, reject_phrase(work.sid), 1, NOW)
    assert confirm(client, work.sid, key)[0] == 409
    assert desk.item(work.sid).state.value == "REJECTED"


def test_both_launchers_print_one_box_without_opening_a_browser(tmp_path, monkeypatch, capsys):
    from public_research_os.automation.demo import serve as demo_serve
    from public_research_os.automation.initialise import initialise
    from public_research_os.automation.review import serve as review_serve
    from public_research_os.drafting import provider
    from public_research_os.publication import buffer
    from public_research_os.review.server import ReviewServer

    def stop(server):
        raise KeyboardInterrupt

    def forbidden(*args, **kwargs):
        pytest.fail("launcher touched a credential loader")

    monkeypatch.setattr(ReviewServer, "serve_forever", stop)
    monkeypatch.setattr(buffer, "publishing_enabled", lambda: False)
    monkeypatch.setattr(buffer, "load_api_key", forbidden)
    monkeypatch.setattr(provider, "load_api_key", forbidden)
    cfg = initialise(tmp_path / "synthetic workspace with spaces", "sample-owner/retry-lab")
    for run in (demo_serve, lambda: review_serve(cfg.workspace_root / "pros.toml", 0)):
        assert run() == 0
        output = capsys.readouterr().out.splitlines()
        heading = "Open this in your browser (Ctrl+click):"
        assert output.count(heading) == 1
        index = output.index(heading)
        assert output[index - 1].startswith("+-") and output[index + 2].startswith("+-")
        assert output[index + 1].startswith("http://127.0.0.1:")


@pytest.mark.parametrize("action", ["approve", "reject", "edit"])
def test_eval_copy_failure_never_claims_the_committed_decision_was_lost(
    work, serve, tmp_path, action
):
    from public_research_os.review.service import reject_phrase

    occupied = tmp_path / "occupied-eval-path"
    occupied.write_bytes(b"synthetic owner file")
    desk = work.desk()
    desk.eval_cases = occupied
    client = serve(desk)
    path = f"/story/{work.sid}"
    if action == "approve":
        key = sheet(client, work.sid)
        code, _, html = confirm(client, work.sid, key)
    elif action == "reject":
        code, _, html = client.post(path + "/reject", seq="0", typed=reject_phrase(work.sid))
    else:
        fields = {
            f"seg-{key}": value for key, value in texts(S01="A saved synthetic edit.").items()
        }
        code, _, html = client.post(path + "/edit", seq="0", action="save", **fields)
        assert "Unsaved edits" not in html and "A saved synthetic edit." in html
    assert code == 409 and "Your change was recorded" in html
    assert "EVAL_CASE_NOT_SAVED" in html and "could not be recorded" not in html
    assert desk.item(work.sid).seq == 1 and occupied.read_bytes() == b"synthetic owner file"
    if action == "approve":
        code, _, html = confirm(client, work.sid, key)
        assert code == 200 and "Your change was recorded" in html
        assert desk.item(work.sid).seq == 1


@pytest.mark.parametrize("prior", ["unrelated", "repeat"])
def test_receipt_recovery_uses_actual_decision_not_per_story_sequence_arithmetic(
    work, serve, tmp_path, prior
):
    from public_research_os.publication.ledger import ReviewKind

    desk = work.desk()
    if prior == "repeat":
        approve_current(desk, work.sid)
    else:
        subject = replace(
            desk.item(work.sid).subject(), story_id="c" * 64, draft_bundle="c" * 64 + "-draft"
        )
        with Ledger.open(work.ledger, create=False) as ledger:
            ledger.add_review_decision(
                subject, ReviewKind.REJECTED, "Other synthetic draft", 0, NOW
            )
    occupied = tmp_path / "occupied-eval-path"
    occupied.write_bytes(b"synthetic")
    desk.eval_cases = occupied
    seq = str(desk.item(work.sid).seq)
    client = serve(desk)
    key = sheet(client, work.sid, seq=seq)
    code, _, html = confirm(client, work.sid, key, seq=seq)
    assert code == 409 and "Your change was recorded" in html
    actual = desk.item(work.sid).seq
    assert actual == (1 if prior == "repeat" else 2)
    code, _, html = confirm(client, work.sid, key, seq=seq)
    assert code == 200 and "Your change was recorded" in html
    assert desk.item(work.sid).seq == actual


def test_fresh_demo_editor_is_the_first_section_inside_details():
    from public_research_os.automation.demo import workspace

    with workspace() as demo:
        item = demo.desk.item(demo.story_id)
        html = story_page(
            item, "synthetic", StoryView(), None, demo=True, gate=demo.desk.gate(item)
        )
        details = html.split("<details id=details><summary>Details</summary>", 1)[1]
        assert details.startswith("<section id=edit><h2>Edit and revalidate</h2>")
        assert details.count("<textarea ") == len(item.current.response.segments) == 5
        assert details.index("<textarea ") < details.index("<h2>Story</h2>")
