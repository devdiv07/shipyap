"""`pros repo set` and `pros repo list` through the real CLI, and the source-level boundary of the
preferences package: no network, no credential, no model, no publisher."""

import ast
import sqlite3
from pathlib import Path

import pytest

from public_research_os.automation.cli import main
from public_research_os.preferences.repositories import RepositoryPreferences
from public_research_os.publication import buffer

SRC = Path(__file__).resolve().parents[1] / "src" / "public_research_os"
PREFERENCES = SRC / "preferences"
HISTORIC = "sample-owner/capture-lab"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "preferences.sqlite"


@pytest.fixture(autouse=True)
def no_outside_world(monkeypatch):
    """Any socket, Buffer transport or credential use fails the test."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the outside world was touched")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr(buffer, "HttpsBufferTransport", forbidden)
    monkeypatch.setattr(buffer, "load_api_key", forbidden)
    monkeypatch.setenv("PUBLISHING_DISABLED", "false")  # even enabled, nothing is scheduled


COMMANDS = {"repo-set": ["repo", "set"], "repo-list": ["repo", "list"]}


def run(capsys, *argv):
    code = main([*COMMANDS[argv[0]], *(str(arg) for arg in argv[1:])])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_repo_set_then_repo_list_shows_the_normalized_repository_and_mode(capsys, db):
    code, out, err = run(
        capsys,
        "repo-set",
        "--repo",
        "Sample-Owner/Capture-Lab",
        "--mode",
        "focus",
        "--db",
        db,
    )
    assert (code, err) == (0, "")
    assert out == f"repo-set: {HISTORIC} focus\n"
    run(capsys, "repo-set", "--repo", "acme/aaa", "--mode", "off", "--db", db)
    code, out, err = run(capsys, "repo-list", "--db", db)
    assert (code, err) == (0, "")
    assert out == f"repo-list: 2 repositories\n  acme/aaa off\n  {HISTORIC} focus\n"


def test_repeating_the_same_command_is_idempotent(capsys, db):
    args = ["repo-set", "--repo", HISTORIC, "--mode", "occasional", "--db", db]
    assert run(capsys, *args)[0] == 0
    assert run(capsys, *args)[0] == 0
    assert (
        run(capsys, "repo-list", "--db", db)[1]
        == f"repo-list: 1 repository\n  {HISTORIC} occasional\n"
    )


@pytest.mark.parametrize("repo", ["", "owner", "../x", "owner/../x", "a/b/c"])
def test_an_invalid_repository_exits_2_with_a_fixed_code_and_stores_nothing(capsys, db, repo):
    code, out, err = run(capsys, "repo-set", "--repo", repo, "--mode", "focus", "--db", db)
    assert (code, out) == (2, "")
    assert "REPOSITORY_INVALID" in err
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM repository_preferences").fetchone() == (0,)
    finally:
        conn.close()


def test_an_invalid_mode_exits_2_without_echoing_the_value_or_writing(capsys, db):
    with pytest.raises(SystemExit) as raised:
        main(["repo", "set", "--repo", HISTORIC, "--mode", "sometimes-ish", "--db", str(db)])
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "sometimes-ish" not in captured.err and captured.out == ""
    assert not db.exists()


def test_repo_list_refuses_a_missing_database_instead_of_creating_one(capsys, db):
    code, out, err = run(capsys, "repo-list", "--db", db)
    assert (code, out) == (2, "")
    assert "STORE_UNUSABLE" in err
    assert not db.exists()


def test_an_existing_empty_store_lists_no_repositories(capsys, db):
    RepositoryPreferences.open(db, create=True).close()
    assert run(capsys, "repo-list", "--db", db) == (0, "repo-list: 0 repositories\n", "")


def test_the_commands_never_use_the_publisher_the_network_or_a_model(capsys, db):
    for mode in ("focus", "occasional", "off"):
        assert run(capsys, "repo-set", "--repo", HISTORIC, "--mode", mode, "--db", db)[0] == 0
    assert run(capsys, "repo-list", "--db", db)[0] == 0  # the autouse guard would have failed


def test_the_preferences_package_has_no_network_environment_model_or_publisher_imports():
    forbidden_roots = {
        "http",
        "ssl",
        "socket",
        "urllib",
        "anthropic",
        "os",
        "subprocess",
        "ctypes",
        "requests",
    }
    forbidden_prefixes = (
        "public_research_os.publication",
        "public_research_os.drafting",
        "public_research_os.editorial",
    )
    modules = sorted(PREFERENCES.glob("*.py"))
    assert len(modules) >= 3
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                assert name.split(".")[0] not in forbidden_roots, (path.name, name)
                assert not name.startswith(forbidden_prefixes), (path.name, name)
            if isinstance(node, ast.Constant):
                assert node.value not in {"PROS_BUFFER_API_KEY", "PUBLISHING_DISABLED"}, path.name
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, path.name
