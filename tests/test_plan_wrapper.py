"""C3: scripts/model-plan.ps1, the only place a plan-route CLI call is made. Windows only (it is
Windows PowerShell): the real script runs a fake `claude` (fake_claude.py) and the real engine
resumes from what it wrote. Checks the isolation the owner asked for: the exact flags (with an
empty --tools value surviving Windows quoting), an empty working folder, no API key variables in
the child's environment, the prompt on stdin and in a file, never on the command line.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from plan_fake import QUOTE_IDS, QUOTES, needed, request_of, toml_workspace
from test_automation import NOW, Github
from test_automation import work as automation_work  # noqa: F401  (fixture)
from test_plan_route import plan_config

from shipyap.automation import engine, plan_route

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell wrapper")


@pytest.fixture
def work(automation_work):  # noqa: F811  (the shared fixture, under this module's name)
    return automation_work


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "model-plan.ps1"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"
FAKE_PROS = Path(__file__).resolve().parent / "fake_pros.py"
PROS = ("-ProsFile", sys.executable, "-ProsPrefix", str(FAKE_PROS))
FLAGS = [
    "-p", "--output-format", "json", "--model", "claude-sonnet-5-5", "--effort", "medium",
    "--system-prompt-file", None, "--json-schema", None, "--tools", "", "--safe-mode",
    "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
    "--no-session-persistence", "--permission-mode", "dontAsk", "--permission-prompts", "none",
]  # fmt: skip


def powershell(*arguments, env=None, script=SCRIPT):
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
        check=False,
    )


def answer(path: Path, log: Path, **variables):
    env = {
        **os.environ,
        "FAKE_CLAUDE_LOG": str(log),
        "ANTHROPIC_API_KEY": "test-value-not-a-key",
        "PROS_ANTHROPIC_API_KEY": "test-value-not-a-key",
        **variables,
    }
    done = powershell(
        "-Config",
        str(path),
        "-LauncherFile",
        sys.executable,
        "-LauncherPrefix",
        str(FAKE),
        *PROS,
        env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def calls(log: Path) -> list[dict]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def run_loop(cfg, path, log, **variables):
    """pros run, then the real wrapper + pros run --resume while it says NEEDS_MODEL."""
    results, requests = [engine.run(cfg, Github(), now=NOW)], {}
    while "NEEDS_MODEL" in results[-1].statuses:
        assert len(results) < 8, [r.statuses for r in results]
        requests.update({h: request_of(cfg.workspace_root, h) for h in needed(cfg.workspace_root)})
        answer(path, log, **variables)
        results.append(engine.run(cfg, Github(), now=NOW, resume=True))
    return results, requests


@pytest.mark.parametrize("shape", ["structured", "result"])
def test_the_wrapper_answers_every_round_in_isolation(work, tmp_path, shape):
    cfg, path = plan_config(work, author=False)
    log = tmp_path / "fake-claude.log"
    results, requests = run_loop(cfg, path, log, FAKE_CLAUDE_SHAPE=shape)
    assert results[-1].statuses == ["DRAFT_CHECKED"], results[-1].failures
    made = calls(log)
    assert len(made) == 3  # discovery, draft, fact check
    by_stdin = {r.user_text: r for r in requests.values()}
    for call in made:
        request = by_stdin[call["stdin"]]  # the user text arrived exactly, on stdin
        assert call["system"] == request.system_prompt  # and the system prompt in a file
        argv = call["argv"]
        assert len(argv) == len(FLAGS)
        assert [a for a, f in zip(argv, FLAGS, strict=True) if f is not None] == [
            f for f in FLAGS if f is not None
        ]
        assert json.loads(argv[FLAGS.index("--json-schema") + 1]) == request.output_schema
        assert request.user_text not in " ".join(argv)  # never on the command line
        assert call["cwd_entries"] == ["system.txt"]  # a fresh, otherwise empty folder
        assert Path(call["cwd"]).name.startswith("shipyap-plan-")
        assert not {"ANTHROPIC_API_KEY", "PROS_ANTHROPIC_API_KEY"} & set(call["env"])
        assert not Path(call["cwd"]).exists()  # removed after the call
    status = json.loads(
        (cfg.workspace_root / "model-inbox" / "auth-status.json").read_text("utf-8-sig")
    )
    assert (status["ok"], status["auth_method"], status["cli_version"]) == (
        True,
        "claude.ai",
        "2.1.318",
    )
    for digest in requests:
        record = json.loads(
            (cfg.workspace_root / "model-inbox" / f"{digest}.json").read_text("utf-8-sig")
        )
        assert (record["route"], record["cli_version"], record["exit_code"]) == (
            "claude-plan",
            "2.1.318",
            0,
        )
        assert record["timed_out"] is False and record["duration_ms"] >= 0
        assert "<output_schema_json>" in record["arguments"]


@pytest.mark.parametrize(
    ("auth", "code"), [("logged_out", "PLAN_SIGNIN_REQUIRED"), ("api_key", "PLAN_NOT_SUBSCRIPTION")]
)
def test_without_a_subscription_sign_in_nothing_is_sent(work, tmp_path, auth, code):
    cfg, path = plan_config(work, author=False)
    log = tmp_path / "fake-claude.log"
    first = engine.run(cfg, Github(), now=NOW)
    assert first.statuses[0] == "NEEDS_MODEL"
    answer(path, log, FAKE_CLAUDE_AUTH=auth)
    assert calls(log) == []  # no print-mode call was made
    blocked = engine.run(cfg, Github(), now=NOW, resume=True)
    assert blocked.statuses[0] == "SIGN_IN_REQUIRED" and code in blocked.failures[0]
    status = json.loads(
        (cfg.workspace_root / "model-inbox" / "auth-status.json").read_text("utf-8-sig")
    )
    assert status["ok"] is False


def test_auth_only_records_the_sign_in_and_calls_nothing(work, tmp_path):
    cfg, path = plan_config(work, author=False)
    engine.run(cfg, Github(), now=NOW)
    log = tmp_path / "fake-claude.log"
    env = {**os.environ, "FAKE_CLAUDE_LOG": str(log)}
    done = powershell(
        "-Config",
        str(path),
        "-AuthOnly",
        "-LauncherFile",
        sys.executable,
        "-LauncherPrefix",
        str(FAKE),
        *PROS,
        env=env,
    )
    assert done.returncode == 0
    replies = [p for p in (cfg.workspace_root / "model-inbox").glob("*.json") if len(p.stem) == 64]
    assert calls(log) == [] and replies == []
    assert plan_route.signed_in_since(cfg.workspace_root) is not None


def _path_only(directory: Path) -> dict:
    system = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        **os.environ,
        "PATH": f"{directory};{system}\\System32;{system};{system}\\System32\\WindowsPowerShell\\v1.0",
    }


@pytest.mark.parametrize("shim", ["claude.cmd", "claude.ps1"])
def test_the_launcher_is_resolved_from_the_npm_shim(tmp_path, shim):
    target = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    text = {
        "claude.cmd": '@ECHO off\r\n"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n',
        "claude.ps1": '& "$basedir/node_modules/@anthropic-ai/claude-code/bin/claude.exe"   $args\n',
    }[shim]
    (tmp_path / shim).write_text(text, encoding="utf-8")
    done = powershell("-PrintLauncher", env=_path_only(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == str(target)


def test_a_javascript_shim_runs_node_with_cli_js(tmp_path):
    cli = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    (tmp_path / "node.exe").write_bytes(b"")
    (tmp_path / "claude.cmd").write_text(
        '@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n',
        encoding="utf-8",
    )
    done = powershell("-PrintLauncher", env=_path_only(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == f"{tmp_path / 'node.exe'}|{cli}"


def test_a_missing_cli_is_a_clear_error(tmp_path):
    done = powershell("-PrintLauncher", env=_path_only(tmp_path))
    assert done.returncode == 2
    assert "CLAUDE_CLI_NOT_FOUND" in done.stdout and "run `claude` once" in done.stdout


# --- reading the config through `pros settings` (both TOML quote styles) ----------------------

NOTIFY = REPO / "scripts" / "run-and-notify.ps1"


@pytest.mark.parametrize("quote", QUOTES, ids=QUOTE_IDS)
def test_model_plan_finds_the_workspace_in_both_quote_styles(tmp_path, quote):
    root, config = toml_workspace(tmp_path, quote)
    done = powershell(
        "-Config", str(config), "-AuthOnly", "-LauncherFile", sys.executable,
        "-LauncherPrefix", str(FAKE), *PROS,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    assert plan_route.signed_in_since(root) is not None


def _notify(tmp_path, config: Path, rounds: list[dict], *extra: str):
    runs, log, toasts = tmp_path / "runs.json", tmp_path / "pros-runs.log", tmp_path / "toasts.txt"
    runs.write_text(json.dumps(rounds), encoding="utf-8")
    env = {**os.environ, "FAKE_PROS_RUNS": str(runs), "FAKE_PROS_LOG": str(log)}
    done = powershell(
        "-Config", str(config), *PROS, "-PlanLauncherFile", sys.executable,
        "-PlanLauncherPrefix", str(FAKE), "-NotifyLog", str(toasts), *extra,
        env=env, script=NOTIFY,
    )  # fmt: skip
    ran = [json.loads(x) for x in log.read_text("utf-8").splitlines()] if log.is_file() else []
    shown = toasts.read_text("utf-8").splitlines() if toasts.is_file() else []
    return done, ran, shown


@pytest.mark.parametrize("quote", QUOTES, ids=QUOTE_IDS)
def test_run_and_notify_loops_the_plan_route_in_both_quote_styles(tmp_path, quote):
    root, config = toml_workspace(tmp_path, quote)
    rounds = [
        {"exit": 3, "notify": []},
        {"exit": 3, "notify": ["Shipyap|first"]},
        {"exit": 0, "notify": ["Shipyap|first", "Shipyap: 1 draft ready to review|Click"]},
    ]
    done, ran, shown = _notify(tmp_path, config, rounds)
    assert done.returncode == 0, done.stdout + done.stderr
    base = ["run", "--config", str(config)]
    assert ran == [base, base + ["--resume"], base + ["--resume"]]
    assert shown == ["Shipyap|first", "Shipyap: 1 draft ready to review|Click"]  # once each
    assert plan_route.signed_in_since(root) is not None  # the sign-in was checked first


@pytest.mark.parametrize("quote", QUOTES, ids=QUOTE_IDS)
def test_run_and_notify_runs_the_api_route_once_in_both_quote_styles(tmp_path, quote):
    _, config = toml_workspace(tmp_path, quote, provider=None)
    done, ran, shown = _notify(
        tmp_path, config, [{"exit": 0, "notify": ["Shipyap|done"]}], "-EnablePaid"
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert ran == [["run", "--config", str(config), "--allow-paid"]] and shown == ["Shipyap|done"]


def test_an_unreadable_config_still_tells_and_fails(tmp_path):
    config = tmp_path / "pros.toml"
    config.write_text("workspace_root = 'C:\\no\\such\\folder'\n", encoding="utf-8")
    done, ran, shown = _notify(tmp_path, config, [{"exit": 0, "notify": []}])
    assert done.returncode == 1 and ran == []
    assert len(shown) == 1 and shown[0].startswith("Shipyap needs you|run-and-notify failed:")
    assert "CONFIG_INVALID" in shown[0]


def test_a_crashed_round_still_tells_and_fails(tmp_path):
    root, config = toml_workspace(tmp_path, '"', provider=None)
    (root / "notify.txt").write_text("Old|from yesterday\n", encoding="utf-8")
    rounds = [{"exit": 2, "notify": None, "say": "NEEDS_ATTENTION: STATE_INVALID"}]
    done, _, shown = _notify(tmp_path, config, rounds)
    assert done.returncode == 2
    assert shown == [
        "Shipyap needs you|pros run failed (NEEDS_ATTENTION: STATE_INVALID); run it by hand to see why."
    ]  # yesterday's notify.txt is not shown again


def test_the_plan_route_refuses_enable_paid(tmp_path):
    _, config = toml_workspace(tmp_path, "'")
    done, ran, shown = _notify(tmp_path, config, [{"exit": 0, "notify": []}], "-EnablePaid")
    assert done.returncode == 1 and ran == []
    assert "never uses API credits" in shown[0]
