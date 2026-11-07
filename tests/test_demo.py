"""Synthetic demo lifecycle and the non-publishing capability, with loopback HTTP only."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_review import Client
from test_review_ux import Fields

from shipyap.automation import demo as module
from shipyap.automation.cli import main
from shipyap.automation.demo import workspace
from shipyap.automation.review import serve as normal_review
from shipyap.drafting import provider
from shipyap.publication import buffer
from shipyap.publication.approval import confirmation_phrase
from shipyap.publication.ledger import Ledger
from shipyap.review.pages import ApprovalDefaults
from shipyap.review.server import ReviewServer
from shipyap.review.service import ReviewError, ReviewErrorCode, schedule_phrase

CHANNEL = "0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("demo touched a provider or credential loader")

    monkeypatch.setattr(provider, "load_api_key", forbidden)
    monkeypatch.setattr(provider, "AnthropicDrafter", forbidden)
    monkeypatch.setattr(buffer, "load_api_key", forbidden)
    monkeypatch.setattr(buffer, "publishing_enabled", forbidden)
    monkeypatch.setattr(buffer, "HttpsBufferTransport", forbidden)
    monkeypatch.setattr(buffer, "BufferPublisher", forbidden)


def test_two_demo_launches_are_isolated_and_cleaned():
    with workspace() as first:
        root1 = first.config.workspace_root
        (root1 / "user-edit.txt").write_text("synthetic edit", encoding="utf-8")
        with workspace() as second:
            root2 = second.config.workspace_root
            assert root1 != root2 and root1.exists() and root2.exists()
            assert not (root2 / "user-edit.txt").exists()
            assert second.desk.item(second.story_id).state.value == "UNAPPROVED"
        assert not root2.exists()
    assert not root1.exists()


def test_demo_shows_a_real_mechanical_failure_and_supplied_correction():
    with workspace() as demo:
        assert "9/10" in demo.example.original and "3/10" in demo.example.corrected
        assert "3/10" in demo.example.evidence
        assert demo.example.block == "NUMBER_NOT_IN_EVIDENCE: 9"
        assert demo.example.corrected_blocks == 0
        item = demo.desk.item(demo.story_id)
        assert not demo.desk.gate(item).approvable and item.seq == 0
        assert "9/10" in item.current.text
        _fix(demo)
        corrected = demo.desk.item(demo.story_id)
        assert corrected.seq == 1 and demo.desk.gate(corrected).approvable
        assert corrected.current.amap.counts.blocking == 0
        assert "9/10" not in corrected.current.text
        assert demo.desk.fact_check(item)["label"] == "SAMPLE fact check (scripted)"
        assert item.state.value == "UNAPPROVED"


def _fix(demo):
    item = demo.desk.item(demo.story_id)
    texts = {s.id: s.text for s in item.current.response.segments}
    texts["S01"] = demo.example.corrected
    demo.desk.save_edit(demo.story_id, texts, item.seq, datetime.now(UTC))


def test_demo_refuses_any_injected_scheduler():
    with workspace() as demo:
        with pytest.raises(ValueError, match="cannot have a scheduler"):
            ReviewServer(demo.desk, 0, scheduler=lambda: pytest.fail("called"))


def test_service_refuses_scheduling_even_with_a_factory_and_valid_approval():
    with workspace() as demo:
        _fix(demo)
        now = datetime.now(UTC)
        due = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidate = demo.desk.approval_candidate(demo.story_id, CHANNEL, due, now)
        approval, _ = demo.desk.approve(
            demo.story_id, CHANNEL, due, confirmation_phrase(candidate.approval_id), 1, now
        )
        with pytest.raises(ReviewError) as raised:
            demo.desk.schedule(
                demo.story_id,
                approval.approval_id,
                schedule_phrase(approval.approval_id),
                2,
                lambda: pytest.fail("provider constructed"),
                now,
            )
        assert raised.value.code == ReviewErrorCode.SCHEDULING_DISABLED
        with Ledger.open(demo.desk.ledger_path, create=False) as ledger:
            assert ledger.list_intents() == []


def test_forged_http_schedule_is_denied_after_approval_with_buffer_env_set(monkeypatch):
    monkeypatch.setenv("PUBLISHING_DISABLED", "false")
    monkeypatch.setenv("PROS_BUFFER_API_KEY", "synthetic-never-used")
    with workspace() as demo:
        now = datetime.now(UTC)
        due = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def defaults():
            return ApprovalDefaults(CHANNEL, due, "Synthetic demo destination")

        with ReviewServer(demo.desk, 0, defaults=defaults, demo_example=demo.example) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = Client(server)
                client.sign_in()
                path = f"/story/{demo.story_id}"
                status, _, page = client.request("GET", "/")
                assert status == 200 and "SYNTHETIC SAMPLE DATA" in page
                assert "SAMPLE fact check (scripted)" in page
                main = page.split("<details id=details")[0]
                assert demo.example.original in main and "Must fix" in main
                assert "README.md:3-3" in main and "3/10" in main
                assert main.count("README.md:3-3") == 1
                assert "data-severity=must-fix" in main
                assert "A number is missing" in main
                assert (
                    "<button disabled aria-describedby=approval-blocked-reason>Approve</button>"
                    in main
                )
                assert main.index("id=approve") < main.index("id=fix")
                assert "NUMBER_NOT_IN_EVIDENCE" not in main
                assert "review-serve" not in page and "--enable-scheduling" not in page
                assert "publish-schedule" not in page and "publish-reconcile" not in page
                assert (
                    client.post(path + "/sheet", seq="0", channel_id=CHANNEL, due_at=due)[0] == 409
                )
                fix = Fields(main.split("<section id=fix>")[1]).values
                # A forged token and a stale sequence leave the original blocked.
                assert (
                    client.post(path + "/edit", **{**fix, "token": "wrong", "action": "save"})[0]
                    == 403
                )
                assert (
                    client.post(path + "/edit", **{**fix, "seq": "99", "action": "save"})[0] == 409
                )
                assert demo.desk.item(demo.story_id).seq == 0
                code, headers, _ = client.post(path + "/edit", **fix, action="save")
                assert code == 303 and headers["Location"] == path + "?panel=decision#decision"
                _, _, corrected = client.request("GET", headers["Location"])
                assert "<details id=details><summary>Details</summary>" in corrected
                corrected_main = corrected.split("<details id=details")[0]
                assert "Recheck passed" in corrected_main and "0 Must fix items" in corrected_main
                assert "data-severity=check-this" in corrected_main
                assert (
                    "<button disabled aria-describedby=approval-blocked-reason>Approve</button>"
                    not in corrected_main
                )
                assert ">Approve</button>" in corrected_main
                assert ">Fix it and recheck</button>" not in corrected_main
                assert "SYNTHETIC SAMPLE DATA" in corrected_main
                assert "Schedule approved post" not in corrected
                assert client.post(path + "/edit", **fix, action="save")[0] == 409
                candidate = demo.desk.approval_candidate(demo.story_id, CHANNEL, due, now)
                approval, _ = demo.desk.approve(
                    demo.story_id, CHANNEL, due, confirmation_phrase(candidate.approval_id), 1, now
                )
                # A valid session and confirmation cannot override the demo capability.
                status, _, page = client.post(
                    path + "/schedule",
                    seq="2",
                    approval_id=approval.approval_id,
                    typed=schedule_phrase(approval.approval_id),
                )
                assert status == 403 and "permanently disabled" in page
                assert "--enable-scheduling" not in page
                assert client.request("GET", path)[0] == 200
                assert "Schedule approved post" not in client.request("GET", path)[2]
                with Ledger.open(demo.desk.ledger_path, create=False) as ledger:
                    assert ledger.list_intents() == []
            finally:
                server.shutdown()
                thread.join(timeout=5)


def test_demo_fix_does_not_replace_a_custom_edit_or_hide_a_remaining_error():
    from shipyap.review.pages import StoryView, story_page

    with workspace() as demo:
        item = demo.desk.item(demo.story_id)
        texts = {s.id: s.text for s in item.current.response.segments}
        texts["S01"] = demo.example.corrected.replace("3/10", "8/10")
        demo.desk.save_edit(demo.story_id, texts, 0, datetime.now(UTC))
        item = demo.desk.item(demo.story_id)
        html = story_page(
            item,
            "sample-token",
            StoryView(),
            None,
            demo=True,
            demo_example=demo.example,
            gate=demo.desk.gate(item),
        )
        assert "8/10" in html and "Must fix" in html
        assert ">Fix it and recheck</button>" not in html and "Recheck passed" not in html
        assert "<button disabled aria-describedby=approval-blocked-reason>Approve</button>" in html


def test_demo_fix_preserves_unsaved_preview_and_stop_messages():
    from shipyap.review.pages import StoryView, story_page

    with workspace() as demo:
        item = demo.desk.item(demo.story_id)
        texts = {s.id: s.text for s in item.current.response.segments}
        texts["S02"] = "Synthetic unsaved custom wording."
        preview = demo.desk.preview(item, texts)
        html = story_page(
            item,
            "sample-token",
            StoryView(texts=texts, preview=preview),
            None,
            demo=True,
            demo_example=demo.example,
            gate=demo.desk.gate(item),
        )
        assert "Synthetic unsaved custom wording." in html
        assert ">Fix it and recheck</button>" not in html and "Recheck passed" not in html
        assert demo.desk.item(demo.story_id).seq == 0
        _fix(demo)
        item = demo.desk.item(demo.story_id)
        stopped = story_page(
            item,
            "sample-token",
            StoryView(),
            None,
            demo=True,
            stopped=True,
            demo_example=demo.example,
            gate=demo.desk.gate(item),
        )
        assert "STOPPED" in stopped and "Approve is now available" not in stopped


def test_normal_review_cannot_enable_scheduling_for_a_demo_workspace():
    with workspace() as demo:
        assert normal_review(demo.config.workspace_root / "pros.toml", 0) == 2


def test_demo_accepts_no_user_workspace_config_or_paid_flags():
    for args in (["demo", "--workspace", "x"], ["demo", "--config", "x"], ["demo", "--allow-paid"]):
        with pytest.raises(SystemExit) as raised:
            main(args)
        assert raised.value.code == 2


def test_demo_cleans_up_after_a_startup_failure(monkeypatch):
    roots = []
    original = module.initialise

    def track(*args, **kwargs):
        cfg = original(*args, **kwargs)
        roots.append(cfg.workspace_root)
        return cfg

    def fail(*args, **kwargs):
        raise ValueError("synthetic startup failure")

    monkeypatch.setattr(module, "initialise", track)
    monkeypatch.setattr(module, "run_brief", fail)
    with pytest.raises(ValueError):
        with workspace():
            pass
    assert roots and all(not p.exists() for p in roots)
