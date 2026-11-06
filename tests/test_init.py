"""Initializer preservation, filesystem failures and fixed posting offsets, offline."""

import hashlib
import sqlite3
from datetime import datetime

import pytest
from public_fake import directory_link

from shipyap.automation import initialise as module
from shipyap.automation.cli import main
from shipyap.automation.config import load_config
from shipyap.automation.initialise import InitError, initialise
from shipyap.preferences.repositories import RepositoryPreferences

REPO = "sample-owner/retry-lab"


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def assert_preserved(root, before):
    after = hashes(root)
    assert before.keys() <= after.keys()
    assert {key: after[key] for key in before} == before


def test_init_creates_all_stores_without_keys_and_handles_spaces(tmp_path):
    root = tmp_path / "workspace with spaces"
    assert main(["init", "--workspace", str(root), "--repo", REPO, "--utc-offset", "+05:30"]) == 0
    cfg = load_config(root / "pros.toml")
    assert cfg.utc_offset == "+05:30" and cfg.authors == {}
    assert cfg.buffer_channel_id == cfg.x_channel_id == ""
    for path in (
        cfg.preferences,
        cfg.backlog,
        cfg.state / "automation.sqlite",
        cfg.state / "run-lock.sqlite",
        cfg.state / "review-ledger.sqlite",
    ):
        assert path.is_file()
    for path in (cfg.drafts, cfg.snapshots, cfg.state / "fact-checks", cfg.state / "eval-cases"):
        assert path.is_dir()
    with RepositoryPreferences.open(cfg.preferences, create=False) as store:
        assert [(p.repo_id, p.mode.value) for p in store.list()] == [(REPO, "focus")]


def test_every_preexisting_file_is_identical_after_init_twice(tmp_path):
    cfg = initialise(tmp_path, REPO, "+05:30")
    with RepositoryPreferences.open(cfg.preferences, create=False) as store:
        store.set_mode(REPO, "off", datetime.now().astimezone())
        store.set_mode("sample-owner/other", "occasional", datetime.now().astimezone())
    (tmp_path / "STOP").write_bytes(b"owner stop\n")
    (cfg.drafts / "owner-notes.txt").write_bytes(b"synthetic owner's existing data\x00\xff")
    # Missing derived directories/files can be filled without rewriting existing ones.
    (cfg.state / "review-ledger.sqlite").unlink()
    before = hashes(tmp_path)
    for _ in range(2):
        assert main(["init", "--workspace", str(tmp_path), "--repo", REPO]) == 0
        assert_preserved(tmp_path, before)
    assert (cfg.state / "review-ledger.sqlite").is_file()
    with RepositoryPreferences.open(cfg.preferences, create=False) as store:
        assert store.get(REPO).mode.value == "off"


@pytest.mark.parametrize(
    "target",
    [
        "repository-preferences.sqlite",
        "story-backlog.sqlite",
        "automation/automation.sqlite",
        "automation/run-lock.sqlite",
        "automation/review-ledger.sqlite",
    ],
)
def test_corrupt_stores_and_every_other_file_are_untouched(tmp_path, target):
    initialise(tmp_path, REPO)
    (tmp_path / target).write_bytes(b"synthetic corrupt store")
    before = hashes(tmp_path)
    assert main(["init", "--workspace", str(tmp_path), "--repo", REPO]) == 2
    assert hashes(tmp_path) == before


def test_legacy_ledger_is_not_migrated(tmp_path):
    initialise(tmp_path, REPO)
    path = tmp_path / "automation/review-ledger.sqlite"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=1")
    db.close()
    before = hashes(tmp_path)
    assert main(["init", "--workspace", str(tmp_path), "--repo", REPO]) == 2
    assert hashes(tmp_path) == before


def test_existing_preferences_are_not_modified_to_add_another_repo(tmp_path):
    initialise(tmp_path, REPO)
    before = hashes(tmp_path)
    with pytest.raises(InitError, match="USE_REPO_SET"):
        initialise(tmp_path, "sample-owner/another")
    assert hashes(tmp_path) == before


def test_existing_config_can_create_missing_stores_in_custom_parent(tmp_path):
    initialise(tmp_path, REPO)
    path = tmp_path / "pros.toml"
    path.write_text(
        path.read_text("utf-8").replace(
            "preferences = 'repository-preferences.sqlite'", "preferences = 'custom/prefs.sqlite'"
        ),
        encoding="utf-8",
    )
    before = hashes(tmp_path)
    initialise(tmp_path, REPO)
    initialise(tmp_path, REPO)
    assert_preserved(tmp_path, before)
    assert (tmp_path / "custom/prefs.sqlite").is_file()


def test_invalid_store_parent_is_refused_before_any_changes(tmp_path):
    initialise(tmp_path, REPO)
    path = tmp_path / "pros.toml"
    path.write_text(
        path.read_text("utf-8").replace(
            "preferences = 'repository-preferences.sqlite'", "preferences = 'occupied/prefs.sqlite'"
        ),
        encoding="utf-8",
    )
    (tmp_path / "occupied").write_bytes(b"synthetic owner file")
    before = hashes(tmp_path)
    with pytest.raises(InitError):
        initialise(tmp_path, REPO)
    assert hashes(tmp_path) == before


@pytest.mark.parametrize("offset", ["+05:30", "-03:30", "+12:45", "+00:00"])
def test_optional_offset_is_saved_with_an_explanatory_comment(tmp_path, offset):
    assert (
        main(["init", "--workspace", str(tmp_path), "--repo", REPO, f"--utc-offset={offset}"]) == 0
    )
    assert load_config(tmp_path / "pros.toml").utc_offset == offset
    assert "supplied with --utc-offset" in (tmp_path / "pros.toml").read_text("utf-8")


def test_default_is_machine_offset_and_rerun_preserves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "local_offset", lambda: "+09:45")
    initialise(tmp_path, REPO)
    assert load_config(tmp_path / "pros.toml").utc_offset == "+09:45"
    assert "captured from this machine" in (tmp_path / "pros.toml").read_text("utf-8")
    before = hashes(tmp_path)
    monkeypatch.setattr(module, "local_offset", lambda: "+01:00")
    initialise(tmp_path, REPO)
    assert hashes(tmp_path) == before
    with pytest.raises(InitError, match="EXISTING_UTC_OFFSET_PRESERVED"):
        initialise(tmp_path, REPO, "+01:00")
    assert hashes(tmp_path) == before


def test_actual_local_offset_matches_python_clock():
    delta = datetime.now().astimezone().utcoffset()
    seconds = int(delta.total_seconds())
    minutes = abs(seconds) // 60
    expected = f"{'-' if seconds < 0 else '+'}{minutes // 60:02d}:{minutes % 60:02d}"
    assert module.local_offset() == expected


@pytest.mark.parametrize("offset", ["5:30", "+25:00", "bad-value", "+05:15"])
def test_invalid_offsets_do_not_create_a_workspace(tmp_path, offset):
    root = tmp_path / "new"
    with pytest.raises(InitError):
        initialise(root, REPO, offset)
    assert not root.exists()


def test_existing_config_with_escaping_paths_is_not_changed(tmp_path):
    initialise(tmp_path, REPO)
    path = tmp_path / "pros.toml"
    path.write_text(
        path.read_text("utf-8").replace("drafts = 'drafts'", "drafts = '../outside'"),
        encoding="utf-8",
    )
    before = hashes(tmp_path)
    assert main(["init", "--workspace", str(tmp_path), "--repo", REPO]) == 2
    assert hashes(tmp_path) == before


def test_a_linked_workspace_directory_cannot_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    directory_link(root / "drafts", outside)
    with pytest.raises(InitError):
        initialise(root, REPO)
    assert list(outside.iterdir()) == []
    assert not (root / "pros.toml").exists()


def test_interrupted_creation_can_be_resumed(tmp_path, monkeypatch):
    publish = module._publish_file
    calls = []

    def interrupted(path, **kwargs):
        calls.append(path)
        if len(calls) == 3:
            raise OSError("synthetic failure")
        return publish(path, **kwargs)

    monkeypatch.setattr(module, "_publish_file", interrupted)
    with pytest.raises(InitError):
        initialise(tmp_path, REPO)
    before = hashes(tmp_path)
    monkeypatch.setattr(module, "_publish_file", publish)
    initialise(tmp_path, REPO)
    assert_preserved(tmp_path, before)
