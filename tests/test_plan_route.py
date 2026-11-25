"""C3: the claude-plan route. Two-phase runs (outbox -> wrapper -> inbox -> --resume) through
the real engine, with the wrapper played by `plan_fake.answer_all` and Claude by the engine
tests' fake model. No process is started and no socket opened here; the PowerShell side is
tested in test_plan_wrapper.py (Windows only).
"""

import json
from datetime import timedelta

import pytest
from plan_fake import (
    FIXTURES,
    QUOTE_IDS,
    QUOTES,
    answer_all,
    fixture,
    needed,
    outbox_record,
    request_of,
    toml_workspace,
    write_inbox,
)
from test_api_route_unchanged import GOLDEN_CALLS
from test_automation import (  # noqa: F401
    FIRST_PERSON,
    NOW,
    Github,
    Model,
    _with_author,
    model_text,
)
from test_automation import work as automation_work  # noqa: F401  (fixture)

from shipyap.automation import cli, engine, factcheck, plan_route, repair
from shipyap.automation.config import AutomationError, load_config
from shipyap.automation.state import State
from shipyap.drafting.errors import DraftingErrorCode
from shipyap.drafting.models import ModelRequest

STARTED = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def work(automation_work):  # noqa: F811  (the shared fixture, under this module's name)
    return automation_work


def plan_config(work, extra: str = "", author: bool = True):
    cfg, path = work
    path.write_text(
        path.read_text(encoding="utf-8") + "provider = 'claude-plan'\n" + extra, encoding="utf-8"
    )
    return (_with_author(cfg, path) if author else load_config(path)), path


def golden_model() -> Model:
    model = Model()
    model.draft_text = model_text(FIRST_PERSON)
    model.verdicts = {"S03": ("OUTDATED", "The canary replaced this behaviour.")}
    return model


def loop(cfg, model, github=None, now=NOW, shape="good_structured", by_kind=None, rounds=12):
    """`pros run`, then answer + `pros run --resume` while it says NEEDS_MODEL."""
    github = github or Github()
    results, asked = [engine.run(cfg, github, now=now)], []
    while "NEEDS_MODEL" in results[-1].statuses:
        assert len(results) < rounds, [r.statuses for r in results]
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        asked += answer_all(cfg.workspace_root, model, stamp, shape, by_kind)
        results.append(engine.run(cfg, github, now=now, resume=True))
    return results, asked


def spending(cfg):
    db = State(cfg.state / "automation.sqlite").db
    return db.execute(
        "SELECT kind, request_hash, estimated_usd FROM spending ORDER BY id"
    ).fetchall()


# --- the full loop -----------------------------------------------------------------------------


def test_the_plan_route_asks_exactly_the_requests_the_api_route_sends(work):
    """Discovery, draft, fact check and two repair rounds, one request per round, through the
    outbox. Every request is byte-identical to the API route's (the golden hashes recorded on
    master), the production adapter is never built (the fixture forbids it), and no API money
    is reserved."""
    cfg, _ = plan_config(work)
    results, asked = loop(cfg, golden_model())
    assert asked == GOLDEN_CALLS
    assert [r.statuses[0] for r in results[:-1]] == ["NEEDS_MODEL"] * 5
    final = results[-1]
    assert "DRAFT_CHECKED" in final.statuses and "SHELVED" in final.statuses
    assert "S03: FACT_CHECK_OUTDATED" in " ".join(final.shelved)
    assert [(k, h) for k, h, _ in spending(cfg)] == GOLDEN_CALLS
    assert {usd for *_, usd in spending(cfg)} == {"0"}
    assert all(r.estimated_usd == 0 for r in results)
    assert not (cfg.state / plan_route.FOLLOW_UP).exists()
    # Every reply is kept: one inbox record per request, none archived.
    inbox = cfg.workspace_root / plan_route.INBOX
    assert sorted(p.stem for p in inbox.glob("*.json")) == sorted(h for _, h in GOLDEN_CALLS)
    assert not (inbox / plan_route.ARCHIVE).exists()
    # The outbox record is the exact request, with its prompt files byte for byte.
    kind, digest = GOLDEN_CALLS[1]
    record = outbox_record(cfg.workspace_root, digest)
    request = request_of(cfg.workspace_root, digest)
    assert (record["kind"], plan_route.request_hash(request)) == (kind, digest)
    outbox = cfg.workspace_root / plan_route.OUTBOX
    assert (outbox / record["user_text_file"]).read_bytes() == request.user_text.encode()
    assert (outbox / record["system_prompt_file"]).read_bytes() == request.system_prompt.encode()
    assert json.loads(record["output_schema_json"]) == request.output_schema


def test_fact_check_flags_are_told_once_in_the_round_that_finishes_the_draft(work):
    cfg, _ = plan_config(work)
    results, _ = loop(cfg, golden_model())
    told = [r for r in results if any(f.startswith("fact check: S03") for f in r.failures)]
    assert told == [results[-1]]
    for middle in results[:-1]:
        assert not middle.failures and middle.follow_up_pending == (
            middle is not results[0] and middle is not results[1]
        )


def test_a_clean_draft_is_offered_with_one_ready_toast_at_the_end(work, monkeypatch):
    cfg, path = plan_config(work)
    monkeypatch.setattr(cli.review_page, "waiting", lambda config: [])
    model = Model()
    model.draft_text = model_text(FIRST_PERSON)
    results, asked = loop(cfg, model)
    assert [k for k, _ in asked] == ["discovery", "draft", "fact_check"]
    toasts = []
    for result in results:
        cli._tell(cfg, result, [])
        toasts += (cfg.workspace_root / "notify.txt").read_text(encoding="utf-8").splitlines()
    ready = [t for t in toasts if "ready to review" in t]
    assert len(ready) == 1 and ready[0].endswith(results[-1].checked_story)
    assert results[-1].statuses == ["DRAFT_CHECKED"]


def test_the_replies_replay_offline_like_api_replies(work):
    """pros eval reads the same stores: the discovery reply is in automation.sqlite."""
    from shipyap.automation import evaluation

    cfg, _ = plan_config(work)
    loop(cfg, golden_model())
    cases = evaluation.load_workspace(cfg.workspace_root)
    assert len(cases) == 1
    report = evaluation.evaluate(cases, [], [])
    assert report.stories and report.stories[0].findings


# --- the command line --------------------------------------------------------------------------


def test_pros_run_exits_3_with_needs_model_and_resume_needs_every_answer(work, capsys, monkeypatch):
    cfg, path = plan_config(work)
    monkeypatch.setattr(cli, "HttpsTransport", lambda limits: Github())
    assert cli.main(["run", "--config", str(path)]) == 3
    out = capsys.readouterr().out
    assert "NEEDS_MODEL: 1 request(s) wait in model-outbox" in out
    assert "Route: claude-plan" in out
    assert cli.main(["run", "--config", str(path), "--resume"]) == 2
    assert "NEEDS_ATTENTION: PLAN_REPLIES_MISSING" in capsys.readouterr().out
    assert cli.main(["run", "--config", str(path), "--allow-paid"]) == 2
    assert "ALLOW_PAID_NOT_USED_BY_PLAN_ROUTE" in capsys.readouterr().out


def test_resume_is_refused_on_the_api_route(work):
    cfg, _ = work
    with pytest.raises(AutomationError, match="RESUME_NEEDS_PLAN_ROUTE"):
        engine.run(cfg, Github(), now=NOW, resume=True)


def test_chatgpt_plan_is_a_stub_that_reads_and_sends_nothing(work):
    cfg, path = work
    path.write_text(path.read_text(encoding="utf-8") + "provider = 'chatgpt-plan'\n", "utf-8")
    github = Github()
    result = engine.run(load_config(path), github, now=NOW)
    assert (
        result.statuses == ["NEEDS_ATTENTION"] and "PROVIDER_NOT_IMPLEMENTED" in result.failures[0]
    )
    assert github.calls == []


@pytest.mark.parametrize(
    "line",
    [
        "provider = 'claude'",
        "plan_calls_per_day = 0",
        "plan_calls_per_day = 51",
        "plan_calls_per_day = '8'",
        "plan_window = '1:00-6:00'",
        "plan_window = '01:00-01:00'",
        "plan_window = '01:00'",
        "plan_timeout_seconds = 30",
    ],
)
def test_invalid_plan_settings_are_refused(work, line):
    _, path = work
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    with pytest.raises(AutomationError, match="CONFIG_INVALID"):
        load_config(path)


def test_plan_settings_default_to_the_api_route(work):
    cfg, _ = work
    assert (cfg.provider, cfg.plan_calls_per_day, cfg.plan_window, cfg.plan_timeout_seconds) == (
        "anthropic-api",
        8,
        "",
        300,
    )
    assert not cfg.plan_route


# --- the two output shapes ---------------------------------------------------------------------


REQUEST = ModelRequest("claude-sonnet-5-5", "system", "user", {"type": "object"}, 100, "medium")
DIGEST = plan_route.request_hash(REQUEST)
ANSWER = json.dumps({"findings": [], "note": "ü ✓"}, ensure_ascii=False)


@pytest.mark.parametrize(
    ("name", "shape"),
    [
        ("good_structured", "structured_output"),
        ("good_result_text", "result"),
        ("good_result_fenced", "result"),
    ],
)
def test_both_output_shapes_give_the_same_reply(name, shape):
    answer = plan_route.read_answer(fixture(name, DIGEST, STARTED, ANSWER), REQUEST, DIGEST)
    assert answer.error is None and answer.shape == shape
    reply = answer.reply
    assert json.loads(reply.texts[0]) == json.loads(ANSWER)
    assert (reply.model, reply.stop_reason, reply.request_id) == (
        "claude-sonnet-5-5",
        "end_turn",
        None,
    )
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (9120, 812)


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  {"a": 1}\n', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('Here you are: {"a": 1} Hope it helps.', '{"a": 1}'),
        ('{"a": 1} trailing words', '{"a": 1}'),
        ("no json at all", "no json at all"),
        ("Here: {not json}", "Here: {not json}"),
    ],
)
def test_json_is_taken_from_the_result_text_only_where_it_is_unambiguous(text, found):
    assert plan_route.json_text(text) == found


def test_the_model_reported_is_kept_and_an_unsafe_name_falls_back_to_the_asked_one():
    record = fixture("good_structured", DIGEST, STARTED, ANSWER)
    stdout = json.loads(record["stdout"])
    stdout["modelUsage"] = {
        "claude-haiku-5-5": {"outputTokens": 3},
        "claude-opus-5-5": {"outputTokens": 900},
    }
    record["stdout"] = json.dumps(stdout)
    assert plan_route.read_answer(record, REQUEST, DIGEST).reply.model == "claude-opus-5-5"
    stdout["modelUsage"] = {"Claude Sonnet [1m]": {"outputTokens": 9}}
    record["stdout"] = json.dumps(stdout)
    assert plan_route.read_answer(record, REQUEST, DIGEST).reply.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    ("name", "code", "transient"),
    [
        ("malformed", None, False),  # a reply: refused later by the strict parser
        ("not_signed_in", DraftingErrorCode.PLAN_SIGNIN_REQUIRED, True),
        ("not_signed_in_skipped", DraftingErrorCode.PLAN_SIGNIN_REQUIRED, True),
        ("not_subscription_skipped", DraftingErrorCode.PLAN_NOT_SUBSCRIPTION, True),
        ("usage_limit", DraftingErrorCode.PLAN_USAGE_LIMIT, True),
        ("usage_limit_no_reset", DraftingErrorCode.PLAN_USAGE_LIMIT, True),
        ("timeout", DraftingErrorCode.PROVIDER_TIMEOUT, True),
        ("cli_failed", DraftingErrorCode.PROVIDER_UNAVAILABLE, True),
    ],
)
def test_every_fixture_is_classified(name, code, transient):
    answer = plan_route.read_answer(fixture(name, DIGEST, STARTED, ANSWER), REQUEST, DIGEST)
    assert (answer.error.code if answer.error else None) == code
    assert answer.transient == transient
    if code is DraftingErrorCode.PLAN_USAGE_LIMIT:
        resets = answer.error.resets_at
        assert (resets.isoformat() if resets else None) == (
            "2026-10-29T06:00:00+00:00" if name == "usage_limit" else None
        )


@pytest.mark.parametrize(
    "change",
    [
        {"inbox_version": "2"},
        {"route": "anthropic-api"},
        {"request_hash": "0" * 64},
        {"skipped": "something_else"},
    ],
)
def test_a_record_for_another_request_or_route_is_not_a_reply(change):
    record = {**fixture("good_structured", DIGEST, STARTED, ANSWER), **change}
    answer = plan_route.read_answer(record, REQUEST, DIGEST)
    assert answer.error.code == DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE
    assert not answer.transient


def test_no_cli_text_reaches_an_error_message():
    record = fixture("cli_failed", DIGEST, STARTED)
    record["stderr_tail"] = "secret-looking text sk-ant-123"
    answer = plan_route.read_answer(record, REQUEST, DIGEST)
    assert "sk-ant" not in str(answer.error) and "sk-ant" not in plan_route.pause_message(
        answer.error
    )


# --- failures ----------------------------------------------------------------------------------


def _first_round(cfg):
    result = engine.run(cfg, Github(), now=NOW)
    assert result.statuses[0] == "NEEDS_MODEL"
    (digest,) = needed(cfg.workspace_root)
    return digest


def test_a_malformed_draft_reply_is_kept_and_never_asked_again(work):
    cfg, _ = plan_config(work)
    results, asked = loop(cfg, Model(), by_kind={"draft": "malformed"})
    assert [k for k, _ in asked] == ["discovery", "draft"]
    assert "RESPONSE_INVALID_JSON" in " ".join(results[-1].failures)
    (draft,) = [h for k, h in asked if k == "draft"]
    assert (cfg.workspace_root / plan_route.INBOX / f"{draft}.json").is_file()  # kept, not archived
    again = engine.run(cfg, Github(), now=NOW)
    assert "GENERATION_ATTEMPTED" in " ".join(again.failures)
    assert "NEEDS_MODEL" not in again.statuses


def test_not_signed_in_holds_tells_once_and_continues_after_sign_in(work, monkeypatch):
    cfg, _ = plan_config(work)
    root = cfg.workspace_root
    digest = _first_round(cfg)
    write_inbox(root, digest, fixture("not_signed_in_skipped", digest, STARTED))
    blocked = engine.run(cfg, Github(), now=NOW, resume=True)
    assert blocked.statuses[0] == "SIGN_IN_REQUIRED" and "NEEDS_ATTENTION" in blocked.statuses
    assert blocked.failures == [
        "synthetic-owner/synthetic-repo: PLAN_SIGNIN_REQUIRED: "
        + plan_route.pause_message(blocked.plan_blocked)
    ]
    monkeypatch.setattr(cli.review_page, "waiting", lambda config: [])
    cli._tell(cfg, blocked, [])
    toasts = (root / "notify.txt").read_text(encoding="utf-8").splitlines()
    assert len(toasts) == 1 and "Sign in to Claude Code (run `claude` once)" in toasts[0]
    assert not (root / plan_route.INBOX / f"{digest}.json").exists()  # archived, kept
    assert len(list((root / plan_route.INBOX / plan_route.ARCHIVE).glob("*.json"))) == 1
    later = NOW + timedelta(hours=1)
    github = Github()
    waiting = engine.run(cfg, github, now=later)
    assert waiting.statuses == ["SIGN_IN_WAIT"] and not waiting.failures and github.calls == []
    cli._tell(cfg, waiting, [])
    assert (root / "notify.txt").read_text(encoding="utf-8") == ""  # told once
    # The wrapper saw a good sign-in after the hold began: the next run continues by itself.
    (root / plan_route.INBOX / plan_route.AUTH_STATUS).write_text(
        json.dumps({"ok": True, "checked_at": (later + timedelta(minutes=5)).isoformat()}), "utf-8"
    )
    resumed = engine.run(cfg, Github(), now=later + timedelta(minutes=6))
    assert resumed.statuses[0] == "NEEDS_MODEL" and needed(root) == [digest]


def test_an_api_key_login_is_refused_and_held_like_a_missing_sign_in(work):
    cfg, _ = plan_config(work)
    digest = _first_round(cfg)
    write_inbox(cfg.workspace_root, digest, fixture("not_subscription_skipped", digest, STARTED))
    blocked = engine.run(cfg, Github(), now=NOW, resume=True)
    assert blocked.statuses[0] == "SIGN_IN_REQUIRED"
    assert "PLAN_NOT_SUBSCRIPTION" in blocked.failures[0]
    assert engine.run(cfg, Github(), now=NOW).statuses == ["SIGN_IN_WAIT"]


def test_a_plan_usage_limit_holds_until_its_reset_unless_retry_now(work):
    cfg, _ = plan_config(work)
    digest = _first_round(cfg)
    write_inbox(cfg.workspace_root, digest, fixture("usage_limit", digest, STARTED))
    blocked = engine.run(cfg, Github(), now=NOW, resume=True)
    assert blocked.statuses[0] == "USAGE_LIMIT_REACHED"
    assert (
        "Your Claude plan usage limit is reached (resets 2026-10-29 06:00 UTC)"
        in (blocked.failures[0])
    )
    github = Github()
    held = engine.run(cfg, github, now=NOW + timedelta(hours=5))
    assert held.statuses == ["USAGE_LIMIT_WAIT"] and github.calls == []
    assert "2026-10-29 06:00 UTC" in held.notes[0]
    assert engine.run(cfg, Github(), now=NOW + timedelta(hours=6)).statuses[0] == "NEEDS_MODEL"
    write_inbox(cfg.workspace_root, digest, fixture("usage_limit_no_reset", digest, STARTED))
    engine.run(cfg, Github(), now=NOW + timedelta(hours=6), resume=True)
    assert engine.run(cfg, Github(), now=NOW + timedelta(hours=10)).statuses == ["USAGE_LIMIT_WAIT"]
    assert engine.run(cfg, Github(), now=NOW + timedelta(hours=10), retry_now=True).statuses[0] == (
        "NEEDS_MODEL"
    )


def test_a_timeout_is_kept_reported_and_asked_again_on_the_next_run(work):
    cfg, _ = plan_config(work)
    digest = _first_round(cfg)
    write_inbox(cfg.workspace_root, digest, fixture("timeout", digest, STARTED))
    result = engine.run(cfg, Github(), now=NOW, resume=True)
    assert "PROVIDER_TIMEOUT" in result.failures[0] and "asked again" in result.failures[0]
    assert "NEEDS_MODEL" not in result.statuses
    again = engine.run(cfg, Github(), now=NOW)  # no hold for a timeout
    assert again.statuses[0] == "NEEDS_MODEL" and needed(cfg.workspace_root) == [digest]


def test_the_daily_call_cap_counts_archived_calls_and_waits_for_tomorrow(work):
    cfg, _ = plan_config(work, "plan_calls_per_day = 1\n")
    root = cfg.workspace_root
    digest = _first_round(cfg)
    write_inbox(root, digest, fixture("timeout", digest, STARTED))
    engine.run(cfg, Github(), now=NOW, resume=True)  # archived: one call used today
    capped = engine.run(cfg, Github(), now=NOW)
    assert "PLAN_CAP_REACHED" in capped.statuses and "NEEDS_MODEL" not in capped.statuses
    assert not capped.failures and needed(root) == []
    assert any("plan_calls_per_day" in n for n in capped.notes)
    tomorrow = engine.run(cfg, Github(), now=NOW + timedelta(days=1))
    assert tomorrow.statuses[0] == "NEEDS_MODEL"


def test_the_cap_uses_the_owners_clock(work):
    cfg, _ = plan_config(work, "plan_calls_per_day = 1\n")
    drafter = plan_route.PlanDrafter(cfg.workspace_root, NOW, "+05:30", 1, 300)
    write_inbox(cfg.workspace_root, "a" * 64, {"started_at": "2026-10-28T19:00:00Z"})  # 00:30 IST
    write_inbox(cfg.workspace_root, "b" * 64, {"started_at": "2026-10-28T18:00:00Z"})  # 23:30 IST
    write_inbox(cfg.workspace_root, "c" * 64, {"no": "start"})  # unreadable: counted
    assert drafter.calls_today() == 2


def test_outside_the_night_window_nothing_is_read_or_sent(work):
    cfg, _ = plan_config(work, "plan_window = '01:00-06:00'\n")
    github = Github()
    # NOW is 05:30 in India (+05:30): inside. 06:30 is outside.
    assert engine.run(cfg, github, now=NOW).statuses[0] == "NEEDS_MODEL"
    github = Github()
    late = engine.run(cfg, github, now=NOW + timedelta(hours=1))
    assert late.statuses == ["PLAN_WINDOW_WAIT"] and github.calls == []


@pytest.mark.parametrize(
    ("window", "hour", "inside"),
    [
        ("01:00-06:00", 1, True),
        ("01:00-06:00", 6, False),
        ("22:00-02:00", 23, True),
        ("22:00-02:00", 1, True),
        ("22:00-02:00", 12, False),
        ("", 12, True),
    ],
)
def test_windows_may_wrap_past_midnight(window, hour, inside):
    now = NOW.replace(hour=hour)
    assert plan_route.in_window(window, now, "+00:00") == inside


def test_holds_of_one_route_never_stop_the_other(tmp_path):
    state = State(tmp_path / "s.sqlite")
    state.hold("PLAN_USAGE_LIMIT", "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert state.holding("2026-06-01T00:00:00Z") is None
    assert state.holding("2026-06-01T00:00:00Z", plan=True)["code"] == "PLAN_USAGE_LIMIT"
    state.hold("PROVIDER_USAGE_LIMIT", "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
    assert state.holding("2026-06-01T00:00:00Z")["code"] == "PROVIDER_USAGE_LIMIT"
    state.release(plan=True)
    assert state.holding("2026-06-01T00:00:00Z", plan=True) is None
    assert state.holding("2026-06-01T00:00:00Z") is not None


def test_the_plan_request_identity_is_the_engines():
    assert plan_route.request_hash(REQUEST) == engine.request_hash(REQUEST)


def test_a_follow_up_record_that_points_elsewhere_is_refused(work):
    cfg, _ = plan_config(work)
    (cfg.state).mkdir(parents=True, exist_ok=True)
    for bad in (
        {"follow_up_version": "1", "story_id": "x", "snapshot": "s"},
        {"follow_up_version": "1", "story_id": "a" * 64, "snapshot": "../s"},
        {"follow_up_version": "1", "story_id": "a" * 64, "snapshot": ".."},
    ):
        (cfg.state / plan_route.FOLLOW_UP).write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            plan_route.load_follow_up(cfg.state)


def test_the_fixtures_are_the_documented_cli_shapes():
    for path in FIXTURES.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["route"] == "claude-plan" and record["inbox_version"] == "1"
        if record["stdout"]:
            result = json.loads(record["stdout"].replace('"<ANSWER_OBJECT>"', "{}"))
            assert result["type"] == "result" and "is_error" in result


def test_repair_and_fact_check_records_are_written_as_on_the_api_route(work):
    cfg, _ = plan_config(work)
    results, _ = loop(cfg, golden_model())
    sid = results[-1].checked_story
    assert factcheck.load(cfg.state, sid)["verdicts"][2]["verdict"] == "OUTDATED"
    assert repair.record_dir(cfg.drafts, sid).exists()


# --- last-run.txt: the whole run, every round --------------------------------------------------


def _last_run(cfg, result, monkeypatch) -> list[str]:
    monkeypatch.setattr(cli.review_page, "waiting", lambda config: [])
    cli._tell(cfg, result, [])
    return (cfg.workspace_root / "last-run.txt").read_text(encoding="utf-8").splitlines()


def test_last_run_sums_every_round_of_the_run(work, monkeypatch):
    cfg, _ = plan_config(work)
    results, asked = loop(cfg, golden_model())
    final = results[-1]
    lines = _last_run(cfg, final, monkeypatch)
    assert lines[0].endswith(
        f"(claude-plan run of {len(results)} round(s) since 2026-10-29 00:00 UTC)"
    )
    charged = sum(r.charged_usd for r in results)
    assert lines[1] == (
        f"drafts created 1, plan calls used {len(asked)} (your Claude plan, no API credits), "
        f"${charged:.4f} API list-price equivalent (not charged)"
    )
    assert final.drafts_created == 0  # the draft was made in an earlier round
    # Notes and failures from every round, each once: the draft round's style notes too.
    assert any(line.startswith("needs you: draft shelved") for line in lines)
    assert (
        sum(
            line == "note: fact check: no sentence flagged as outdated, contradicted or overstated"
            for line in lines
        )
        <= 1
    )
    assert not (cfg.state / plan_route.SERIES).exists()  # the run is over


def test_a_round_that_still_waits_reports_the_run_so_far(work, monkeypatch):
    cfg, _ = plan_config(work)
    first = engine.run(cfg, Github(), now=NOW)
    answer_all(cfg.workspace_root, Model(), STARTED)
    second = engine.run(cfg, Github(), now=NOW, resume=True)
    assert "NEEDS_MODEL" in second.statuses and (cfg.state / plan_route.SERIES).is_file()
    lines = _last_run(cfg, second, monkeypatch)
    assert "run of 2 round(s)" in lines[0] and "plan calls used 1 " in lines[1]
    assert first.summary["rounds"] == 1


def test_a_new_run_starts_a_new_summary(work, monkeypatch):
    cfg, _ = plan_config(work)
    engine.run(cfg, Github(), now=NOW)
    answer_all(cfg.workspace_root, Model(), STARTED)
    again = engine.run(cfg, Github(), now=NOW)  # not --resume: a new run
    assert again.summary["rounds"] == 1


def test_the_api_route_last_run_is_unchanged(work, monkeypatch):
    cfg, _ = work
    from test_automation import execute

    result = execute(cfg, allow_paid=True)
    assert result.summary is None
    lines = _last_run(cfg, result, monkeypatch)
    assert lines[1] == (
        f"drafts created 1, estimated spend ${result.estimated_usd:.4f} reserved (a ceiling), "
        f"${result.charged_usd:.4f} at list price for the usage reported"
    )


@pytest.mark.parametrize("quote", QUOTES, ids=QUOTE_IDS)
def test_pros_settings_resolves_both_quote_styles(tmp_path, quote, capsys):
    """What the PowerShell wrappers read instead of parsing TOML themselves."""
    root, config = toml_workspace(tmp_path, quote)
    assert cli.main(["settings", "--config", str(config)]) == 0
    settings = json.loads(capsys.readouterr().out)
    assert (settings["workspace_root"], settings["provider"]) == (str(root), "claude-plan")
    assert settings["plan_calls_per_day"] == 8


def test_pros_settings_refuses_an_invalid_config(tmp_path, capsys):
    config = tmp_path / "pros.toml"
    config.write_text("workspace_root = 'missing'\n", encoding="utf-8")
    assert cli.main(["settings", "--config", str(config)]) == 2
    assert capsys.readouterr().out == "NEEDS_ATTENTION: CONFIG_INVALID\n"
