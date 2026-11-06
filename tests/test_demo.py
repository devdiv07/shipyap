"""Synthetic demo lifecycle and the non-publishing capability, with loopback HTTP only."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_review import Client

from public_research_os.automation import demo as module
from public_research_os.automation.cli import main
from public_research_os.automation.demo import workspace
from public_research_os.automation.review import serve as normal_review
from public_research_os.drafting import provider
from public_research_os.publication import buffer
from public_research_os.publication.approval import confirmation_phrase
from public_research_os.publication.ledger import Ledger
from public_research_os.review.pages import ApprovalDefaults
from public_research_os.review.server import ReviewServer
from public_research_os.review.service import ReviewError, ReviewErrorCode, schedule_phrase

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
        assert demo.desk.gate(item).approvable and item.seq == 0
        assert demo.desk.fact_check(item)["label"] == "SAMPLE fact check (scripted)"
        assert item.state.value == "UNAPPROVED"


def test_demo_refuses_any_injected_scheduler():
    with workspace() as demo:
        with pytest.raises(ValueError, match="cannot have a scheduler"):
            ReviewServer(demo.desk, 0, scheduler=lambda: pytest.fail("called"))


def test_service_refuses_scheduling_even_with_a_factory_and_valid_approval():
    with workspace() as demo:
        now = datetime.now(UTC)
        due = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        candidate = demo.desk.approval_candidate(demo.story_id, CHANNEL, due, now)
        approval, _ = demo.desk.approve(
            demo.story_id, CHANNEL, due, confirmation_phrase(candidate.approval_id), 0, now
        )
        with pytest.raises(ReviewError) as raised:
            demo.desk.schedule(
                demo.story_id,
                approval.approval_id,
                schedule_phrase(approval.approval_id),
                1,
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
                status, _, page = client.request("GET", path)
                assert status == 200 and "SYNTHETIC SAMPLE DATA" in page
                assert "SAMPLE fact check (scripted)" in page
                assert "A claim caught and corrected" in page and "NUMBER_NOT_IN_EVIDENCE" in page
                assert "review-serve" not in page and "--enable-scheduling" not in page
                assert "publish-schedule" not in page and "publish-reconcile" not in page
                candidate = demo.desk.approval_candidate(demo.story_id, CHANNEL, due, now)
                approval, _ = demo.desk.approve(
                    demo.story_id, CHANNEL, due, confirmation_phrase(candidate.approval_id), 0, now
                )
                # A valid session and confirmation cannot override the demo capability.
                status, _, page = client.post(
                    path + "/schedule",
                    seq="1",
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
