"""Offline, non-overwriting workspace initialization. No credentials or processes."""

import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from shipyap.automation.config import Config, load_config
from shipyap.automation.state import State
from shipyap.preferences.repositories import RepositoryPreferences, normalize_repository
from shipyap.publication.ledger import Ledger
from shipyap.stories.backlog import StoryBacklog

DEMO_MARKER = ".pros-demo"
_OFFSET = re.compile(r"[+-](0\d|1[0-4]):(00|30|45)")


class InitError(Exception):
    """Only fixed codes are printed; no raw config, filesystem or SQLite errors."""


def local_offset() -> str:
    delta = datetime.now().astimezone().utcoffset()
    if delta is None:
        raise InitError("LOCAL_OFFSET_UNAVAILABLE")
    seconds = int(delta.total_seconds())
    if seconds % 60:
        raise InitError("LOCAL_OFFSET_UNSUPPORTED")
    minutes = abs(seconds) // 60
    return f"{'-' if seconds < 0 else '+'}{minutes // 60:02d}:{minutes % 60:02d}"


def _real_path(path: Path, root: Path) -> None:
    if not path.resolve().is_relative_to(root):
        raise InitError("WORKSPACE_PATH_INVALID")
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise InitError("WORKSPACE_PATH_INVALID")


def _connection(path: Path) -> sqlite3.Connection:
    # Immutable readers neither migrate databases nor create/update journal/shm sidecars.
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def _identity(db: sqlite3.Connection) -> tuple:
    objects = tuple(
        sorted(
            (kind, name, re.sub(r"\s+", " ", sql or "").strip())
            for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master")
            if not name.startswith("sqlite_")
        )
    )
    return (
        db.execute("PRAGMA user_version").fetchone()[0],
        db.execute("PRAGMA application_id").fetchone()[0],
        objects,
    )


def _create(path: Path, kind: str) -> None:
    if kind == "automation":
        State(path).close()
    elif kind == "lock":
        sqlite3.connect(path).close()
    else:
        factory = {"preferences": RepositoryPreferences, "backlog": StoryBacklog, "ledger": Ledger}[
            kind
        ]
        factory.open(path, create=True).close()


def _expected(kind: str) -> tuple:
    if kind == "automation":
        state = State(Path(":memory:"))
        try:
            return _identity(state.db)
        finally:
            state.close()
    if kind == "lock":
        db = sqlite3.connect(":memory:")
        try:
            return _identity(db)
        finally:
            db.close()
    factory = {"preferences": RepositoryPreferences, "backlog": StoryBacklog, "ledger": Ledger}[
        kind
    ]
    store = factory.open(Path(":memory:"), create=True)
    try:
        return _identity(store._db)
    finally:
        store.close()


def _validate(path: Path, kind: str) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise InitError("WORKSPACE_BUSY")
    db = _connection(path)
    try:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)] or _identity(db) != _expected(
            kind
        ):
            raise InitError("STORE_INCOMPATIBLE")
    finally:
        db.close()


def _publish_file(
    path: Path, *, data: bytes | None = None, kind: str | None = None, repo: str | None = None
) -> None:
    # Complete a new file privately, then publish it with a no-overwrite hard link. Never
    # replace an existing config/database, including one created by a competing initializer.
    with tempfile.NamedTemporaryFile(prefix=".pros-init-", dir=path.parent, delete=False) as handle:
        stage = Path(handle.name)
        if data is not None:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    try:
        if kind is not None:
            _create(stage, kind)
            if repo is not None:
                with RepositoryPreferences.open(stage, create=False) as store:
                    store.set_mode(repo, "focus", datetime.now(UTC))
        os.link(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def _config_text(config: Config, offset_source: str) -> bytes:
    return (
        f"# Created by pros init. No credentials belong in this file.\n"
        f"workspace_root = {json.dumps(str(config.workspace_root), ensure_ascii=False)}\n"
        f"workspace_id = {json.dumps(config.workspace_id)}\n"
        "preferences = 'repository-preferences.sqlite'\nbacklog = 'story-backlog.sqlite'\n"
        "drafts = 'drafts'\nsnapshots = 'snapshots'\nstate = 'automation'\n"
        "model = 'claude-sonnet-5-5'\nmax_cost_usd = '1.00'\nmax_new_drafts = 1\n"
        "weekly_draft_cap = 4\nposting_slots = ['Mon 09:30', 'Wed 09:30', 'Fri 09:30']\n"
        f"# Fixed UTC offset {offset_source}; edit it when your clock/DST changes.\n"
        f"utc_offset = '{config.utc_offset}'\n"
        "buffer_channel_id = '' # Optional LinkedIn destination; scheduling requires your confirmation.\n"
        "x_channel_id = '' # Optional X destination.\n\n"
        "# Optional: your own true statements, required before first-person claims.\n"
        "# [author]\n# name = 'Your Name'\n# [[author.repos]]\n# repo = 'owner/repo'\n"
        "# relationship = 'primary_author'\n# statements = ['I built this repository to ...']\n"
        "# learned = []\n# do_not_claim = []\n# hashtags = []\n"
    ).encode()


def initialise(workspace: Path, repo: str, utc_offset: str | None = None) -> Config:
    try:
        repo = normalize_repository(repo)
        root = workspace.absolute()
        if root.is_symlink() or (
            root.exists() and getattr(root.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise InitError("WORKSPACE_PATH_INVALID")
        root = root.resolve()
        if (root / DEMO_MARKER).exists():
            raise InitError("DEMO_WORKSPACE")
        config_path = root / "pros.toml"
        if utc_offset is not None and not _OFFSET.fullmatch(utc_offset):
            raise InitError("UTC_OFFSET_INVALID")
        if config_path.exists():
            _real_path(config_path, root)
            config = load_config(config_path)
            if config.workspace_root != root:
                raise InitError("WORKSPACE_ROOT_MISMATCH")
            if utc_offset is not None and config.utc_offset != utc_offset:
                raise InitError("EXISTING_UTC_OFFSET_PRESERVED")
        else:
            offset = utc_offset if utc_offset is not None else local_offset()
            if not _OFFSET.fullmatch(offset):
                raise InitError("UTC_OFFSET_INVALID")
            config = Config(
                root,
                "ws_" + uuid4().hex,
                root / "repository-preferences.sqlite",
                root / "story-backlog.sqlite",
                root / "drafts",
                root / "snapshots",
                root / "automation",
                utc_offset=offset,
            )
        stores = {
            config.preferences: "preferences",
            config.backlog: "backlog",
            config.state / "automation.sqlite": "automation",
            config.state / "run-lock.sqlite": "lock",
            config.state / "review-ledger.sqlite": "ledger",
        }
        directories = (
            config.drafts,
            config.snapshots,
            config.state,
            config.state / "fact-checks",
            config.state / "eval-cases",
        )
        paths = [*stores, *directories, config_path]
        if len(set(paths)) != len(paths):
            raise InitError("WORKSPACE_PATH_INVALID")
        for path in paths:
            _real_path(path, root)
            for parent in path.parents:
                if parent == root.parent:
                    break
                if parent.exists() and not parent.is_dir():
                    raise InitError("WORKSPACE_PATH_INVALID")
            if path in stores and path.exists():
                _validate(path, stores[path])
            elif path in directories and path.exists() and not path.is_dir():
                raise InitError("WORKSPACE_PATH_INVALID")
        if config.preferences.exists():
            db = _connection(config.preferences)
            try:
                if (
                    db.execute(
                        "SELECT 1 FROM repository_preferences WHERE repo_id=?", (repo,)
                    ).fetchone()
                    is None
                ):
                    raise InitError("EXISTING_PREFERENCES_PRESERVED_USE_REPO_SET")
            finally:
                db.close()
        for directory in (root, *directories, *(p.parent for p in stores)):
            directory.mkdir(parents=True, exist_ok=True)
        for path, kind in stores.items():
            if not path.exists():
                _publish_file(path, kind=kind, repo=repo if kind == "preferences" else None)
        if not config_path.exists():
            _publish_file(
                config_path,
                data=_config_text(
                    config,
                    "supplied with --utc-offset"
                    if utc_offset is not None
                    else "captured from this machine at initialization",
                ),
            )
        return load_config(config_path)
    except InitError:
        raise
    except Exception:
        raise InitError("WORKSPACE_INIT_FAILED") from None
