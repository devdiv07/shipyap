"""Per-repository publishing mode in one local SQLite file (standard library only).

Each selected repository has exactly one mode:
- focus       preferred source of future story candidates.
- occasional  eligible, lower priority than focus.
- off         excluded from automatic selection. The record is kept.

A repository that was never set is not selected and not eligible. A mode says nothing about the
repository's age or recent commits, and focus never guarantees a post: evidence quality and human
approval stay mandatory. Nothing here reads GitHub; a valid name is not proof that the repository
exists.

The file holds only this table. It is separate from the publisher ledger (approvals and send
intents); `application_id` makes either module refuse the other's file. Rows are never deleted
(trigger), so switching a mode to `off` and back loses nothing. Anyone who can write the file can
change it, as with the ledger.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from public_research_os.ingestion.errors import IngestionError
from public_research_os.ingestion.request import parse_allow_repo
from public_research_os.preferences.errors import PreferencesError, PreferencesErrorCode

SCHEMA_VERSION = 1
APPLICATION_ID = 0x50524F53  # "PROS"
_E = PreferencesErrorCode


class Mode(StrEnum):
    FOCUS = "focus"
    OCCASIONAL = "occasional"
    OFF = "off"


ELIGIBLE_MODES = (Mode.FOCUS, Mode.OCCASIONAL)

_SCHEMA = (
    f"""CREATE TABLE repository_preferences (
        repo_id TEXT PRIMARY KEY CHECK (repo_id = lower(repo_id) AND instr(repo_id, '/') > 1),
        mode TEXT NOT NULL CHECK (mode IN ({", ".join(f"'{m.value}'" for m in Mode)})),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER repository_preferences_no_delete BEFORE DELETE ON repository_preferences
       BEGIN SELECT RAISE(ABORT, 'repository preferences are permanent'); END""",
    """CREATE TRIGGER repository_preferences_identity BEFORE UPDATE ON repository_preferences
       WHEN NEW.repo_id IS NOT OLD.repo_id OR NEW.created_at IS NOT OLD.created_at
       BEGIN SELECT RAISE(ABORT, 'repository identity is immutable'); END""",
)


def _application_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    """(type, name) of every schema object, except SQLite-managed ones (e.g. `sqlite_stat1` after
    ANALYZE, `sqlite_autoindex_*` for keys), which are not application schema."""
    return {
        (kind, name)
        for kind, name in connection.execute("SELECT type, name FROM sqlite_master")
        if not name.startswith("sqlite_")
    }


def _expected_objects() -> frozenset[tuple[str, str]]:
    """Exactly the objects `_SCHEMA` creates; anything else (view, index, trigger) is refused."""
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA:
            connection.execute(statement)
        return frozenset(_application_objects(connection))
    finally:
        connection.close()


_EXPECTED_OBJECTS = _expected_objects()


@dataclass(frozen=True)
class RepositoryPreference:
    repo_id: str  # normalized: "owner/repository", lower case
    mode: Mode
    created_at: str
    updated_at: str


def normalize_repository(value: str) -> str:
    """The stable identifier for an explicit `OWNER/REPOSITORY`, or a refusal.

    Validation is the ingestion allowlist's (GitHub-style names, no `.`/`..`, no `.git` suffix, no
    extra slashes or whitespace). GitHub names are case-insensitive, so the identifier is lower
    case. Nothing is guessed, completed or looked up."""
    if not isinstance(value, str):
        raise PreferencesError(_E.REPOSITORY_INVALID)
    try:
        ref = parse_allow_repo(value)
    except IngestionError:
        raise PreferencesError(_E.REPOSITORY_INVALID) from None
    return f"{ref.owner}/{ref.repo}".lower()


def parse_mode(value: str) -> Mode:
    try:
        return Mode(value)
    except ValueError:
        raise PreferencesError(_E.MODE_INVALID) from None


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class RepositoryPreferences:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    def open(cls, path: Path, *, create: bool) -> "RepositoryPreferences":
        """`create=False` refuses a missing file instead of silently starting an empty store."""
        try:
            if not create and not path.is_file():
                raise PreferencesError(_E.STORE_UNUSABLE)
            connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        except (sqlite3.Error, OSError):
            raise PreferencesError(_E.STORE_UNUSABLE) from None
        try:
            connection.execute("PRAGMA synchronous = FULL")
            if create:
                # The write lock comes first: a competing initializer waits here and then sees
                # the committed identity, so exactly one store type can claim an empty file.
                connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            objects = _application_objects(connection)
            if version == 0 and app_id == 0 and not objects and create:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif (
                version != SCHEMA_VERSION
                or app_id != APPLICATION_ID
                or objects != _EXPECTED_OBJECTS
            ):
                raise PreferencesError(_E.STORE_UNUSABLE)  # includes any mixed-store file
            if create:
                connection.execute("COMMIT")
            connection.execute("SELECT count(*) FROM repository_preferences").fetchone()
        except PreferencesError:
            connection.close()
            raise
        except sqlite3.Error:
            connection.close()
            raise PreferencesError(_E.STORE_UNUSABLE) from None
        return cls(connection)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "RepositoryPreferences":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def set_mode(self, repository: str, mode: str, now: datetime) -> RepositoryPreference:
        """Create or update the one row for `repository` in a single atomic statement. Setting the
        mode it already has changes nothing, including `updated_at`. Validation happens first, so
        a refused value leaves the store untouched."""
        repo_id = normalize_repository(repository)
        chosen = parse_mode(mode)
        stamp = _stamp(now)
        try:
            self._db.execute(
                "INSERT INTO repository_preferences (repo_id, mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (repo_id) DO UPDATE SET "
                "mode = excluded.mode, updated_at = excluded.updated_at "
                "WHERE repository_preferences.mode != excluded.mode",
                (repo_id, chosen.value, stamp, stamp),
            )
        except sqlite3.Error:
            raise PreferencesError(_E.STORE_WRITE_FAILED) from None
        stored = self.get(repo_id)
        if stored is None or stored.mode != chosen:
            raise PreferencesError(_E.STORE_WRITE_FAILED)
        return stored

    def get(self, repository: str) -> RepositoryPreference | None:
        rows = self._rows("WHERE repo_id = ?", (normalize_repository(repository),))
        return rows[0] if rows else None

    def list(self) -> tuple[RepositoryPreference, ...]:
        """Every saved repository, including `off`, ordered by identifier."""
        return self._rows("", ())

    def eligible(self) -> tuple[RepositoryPreference, ...]:
        """The repositories automatic story selection may draw from: `focus` and `occasional`,
        ordered by identifier. `off` and never-selected repositories are absent. The order is not
        a priority; callers weigh `mode` themselves. Eligibility is not a promise of a post."""
        marks = ", ".join("?" for _ in ELIGIBLE_MODES)
        return self._rows(f"WHERE mode IN ({marks})", tuple(m.value for m in ELIGIBLE_MODES))

    def _rows(self, where: str, params: tuple[str, ...]) -> tuple[RepositoryPreference, ...]:
        try:
            rows = self._db.execute(
                "SELECT repo_id, mode, created_at, updated_at FROM repository_preferences "
                f"{where} ORDER BY repo_id",
                params,
            ).fetchall()
        except sqlite3.Error:
            raise PreferencesError(_E.STORE_UNUSABLE) from None
        try:
            return tuple(RepositoryPreference(r[0], Mode(r[1]), r[2], r[3]) for r in rows)
        except ValueError:
            raise PreferencesError(_E.STORE_UNUSABLE) from None
