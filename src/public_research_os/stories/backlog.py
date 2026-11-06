"""The story backlog: story candidates and the evidence observed for them, in one local SQLite file
(standard library only).

A story is identified by its repository and the author's `story_key`, never by a commit, so a new
commit or a repeated scan cannot duplicate it. Each check-passing observation of a story (angle,
support, located references, pinned revision) is kept once, by content digest; observing the same
finding at a new revision adds evidence to the existing story and changes nothing else.

States: `available` (in the backlog, never offered) and `offered` (handed to editorial work once).
The only transition is available -> offered, and nothing resets it: a later scan, new evidence or
a preference change leaves an offered story offered. Neither state means drafted, approved,
scheduled or posted; nothing here links to the publisher ledger.

The file holds only these tables. Like the preferences file it carries its own `application_id`,
so the preferences store and the publisher ledger refuse it and it refuses theirs. Rows are never
deleted (triggers) and evidence is never edited. Anyone who can write the file can change it.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from public_research_os.evidence.report import canonical_json
from public_research_os.stories.errors import StoryError, StoryErrorCode
from public_research_os.stories.findings import EvidenceRef, angle_fingerprint

SCHEMA_VERSION = 1
APPLICATION_ID = 0x50525354  # "PRST"; the preferences file uses "PROS"
_E = StoryErrorCode


class StoryState(StrEnum):
    AVAILABLE = "available"
    OFFERED = "offered"


class RecordOutcome(StrEnum):
    NEW = "NEW"  # a story not seen before, recorded as available
    EVIDENCE_ADDED = "EVIDENCE_ADDED"  # known story, new observation (e.g. a new revision)
    UNCHANGED = "UNCHANGED"  # this exact observation is already recorded
    DUPLICATE_ANGLE = "DUPLICATE_ANGLE"  # the same angle is recorded under another key


_SCHEMA = (
    f"""CREATE TABLE stories (
        story_id TEXT PRIMARY KEY CHECK (length(story_id) = 64),
        repo_id TEXT NOT NULL CHECK (repo_id = lower(repo_id) AND instr(repo_id, '/') > 1),
        story_key TEXT NOT NULL CHECK (length(story_key) BETWEEN 3 AND 64),
        state TEXT NOT NULL CHECK (state IN ({", ".join(f"'{s.value}'" for s in StoryState)})),
        first_seen_at TEXT NOT NULL,
        offered_at TEXT,
        CHECK ((state = 'offered') = (offered_at IS NOT NULL)),
        UNIQUE (repo_id, story_key)
    )""",
    """CREATE TABLE story_evidence (
        seq INTEGER PRIMARY KEY,
        evidence_id TEXT NOT NULL UNIQUE CHECK (length(evidence_id) = 64),
        story_id TEXT NOT NULL REFERENCES stories (story_id),
        source_url TEXT NOT NULL,
        revision_sha TEXT NOT NULL CHECK (length(revision_sha) = 40),
        manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
        angle TEXT NOT NULL,
        angle_fingerprint TEXT NOT NULL,
        support TEXT NOT NULL,
        refs_json TEXT NOT NULL,
        observed_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER stories_no_delete BEFORE DELETE ON stories
       BEGIN SELECT RAISE(ABORT, 'stories are permanent'); END""",
    """CREATE TRIGGER stories_identity BEFORE UPDATE ON stories
       WHEN NEW.story_id IS NOT OLD.story_id OR NEW.repo_id IS NOT OLD.repo_id
         OR NEW.story_key IS NOT OLD.story_key OR NEW.first_seen_at IS NOT OLD.first_seen_at
       BEGIN SELECT RAISE(ABORT, 'story identity is immutable'); END""",
    """CREATE TRIGGER stories_transition BEFORE UPDATE ON stories
       WHEN NOT (OLD.state = 'available' AND NEW.state = 'offered')
       BEGIN SELECT RAISE(ABORT, 'the only story transition is available -> offered'); END""",
    """CREATE TRIGGER story_evidence_no_delete BEFORE DELETE ON story_evidence
       BEGIN SELECT RAISE(ABORT, 'story evidence is permanent'); END""",
    """CREATE TRIGGER story_evidence_no_update BEFORE UPDATE ON story_evidence
       BEGIN SELECT RAISE(ABORT, 'story evidence is immutable'); END""",
)


def _application_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    """(type, name) of every schema object except SQLite-managed ones (`sqlite_*`)."""
    return {
        (kind, name)
        for kind, name in connection.execute("SELECT type, name FROM sqlite_master")
        if not name.startswith("sqlite_")
    }


def _expected_objects() -> frozenset[tuple[str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA:
            connection.execute(statement)
        return frozenset(_application_objects(connection))
    finally:
        connection.close()


_EXPECTED_OBJECTS = _expected_objects()


def story_id(repo_id: str, story_key: str) -> str:
    """Stable across scans and revisions: the repository and the author's key, nothing else."""
    return hashlib.sha256(f"story-v1\n{repo_id}\n{story_key}".encode()).hexdigest()


@dataclass(frozen=True)
class Observation:
    """One check-passing finding at one pinned revision."""

    source_url: str
    revision_sha: str
    manifest_sha256: str
    angle: str
    support: str
    refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    source_url: str
    revision_sha: str
    manifest_sha256: str
    angle: str
    support: str
    refs: tuple[EvidenceRef, ...]
    observed_at: str


@dataclass(frozen=True)
class Story:
    story_id: str
    repo_id: str
    story_key: str
    state: StoryState
    first_seen_at: str
    offered_at: str | None
    evidence_count: int
    latest: Evidence  # the most recently recorded observation
    semantic_support: str = "UNKNOWN"  # never anything else: a located citation is not support


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _refs_json(refs: tuple[EvidenceRef, ...]) -> str:
    return canonical_json(
        [
            {
                "path": ref.path,
                "line_start": ref.line_start,
                "line_end": ref.line_end,
                "quote": ref.quote,
                "file_sha256": ref.file_sha256,
            }
            for ref in refs
        ]
    )


def evidence_id(sid: str, observation: Observation) -> str:
    content = canonical_json(
        {
            "story_id": sid,
            "source_url": observation.source_url,
            "revision_sha": observation.revision_sha,
            "manifest_sha256": observation.manifest_sha256,
            "angle": observation.angle,
            "support": observation.support,
            "refs": json.loads(_refs_json(observation.refs)),
        }
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class StoryBacklog:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    def open(cls, path: Path, *, create: bool) -> "StoryBacklog":
        """`create=False` refuses a missing file instead of silently starting an empty backlog."""
        try:
            if not create and not path.is_file():
                raise StoryError(_E.BACKLOG_UNUSABLE)
            connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        except (sqlite3.Error, OSError):
            raise StoryError(_E.BACKLOG_UNUSABLE) from None
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            if create:
                # Write lock first, so exactly one store type can claim an empty file.
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
                raise StoryError(_E.BACKLOG_UNUSABLE)
            if create:
                connection.execute("COMMIT")
            connection.execute("SELECT count(*) FROM stories").fetchone()
        except StoryError:
            connection.close()
            raise
        except sqlite3.Error:
            connection.close()
            raise StoryError(_E.BACKLOG_UNUSABLE) from None
        return cls(connection)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "StoryBacklog":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All writes inside commit together or not at all."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            raise StoryError(_E.BACKLOG_WRITE_FAILED) from None
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException as exc:
            try:
                self._db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise StoryError(_E.BACKLOG_WRITE_FAILED) from None
            raise

    def record(
        self, repo_id: str, story_key: str, observation: Observation, now: datetime
    ) -> tuple[RecordOutcome, str]:
        """Record one observation. Call inside `transaction()`. Never changes a story's state."""
        sid = story_id(repo_id, story_key)
        fingerprint = angle_fingerprint(observation.angle)
        clash = self._db.execute(
            "SELECT 1 FROM story_evidence e JOIN stories s ON s.story_id = e.story_id "
            "WHERE s.repo_id = ? AND e.angle_fingerprint = ? AND e.story_id != ? LIMIT 1",
            (repo_id, fingerprint, sid),
        ).fetchone()
        if clash:
            return RecordOutcome.DUPLICATE_ANGLE, sid
        eid = evidence_id(sid, observation)
        if self._db.execute(
            "SELECT 1 FROM story_evidence WHERE evidence_id = ?", (eid,)
        ).fetchone():
            return RecordOutcome.UNCHANGED, sid
        stamp = _stamp(now)
        known = self._db.execute("SELECT 1 FROM stories WHERE story_id = ?", (sid,)).fetchone()
        if not known:
            self._db.execute(
                "INSERT INTO stories (story_id, repo_id, story_key, state, first_seen_at, "
                "offered_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (sid, repo_id, story_key, StoryState.AVAILABLE.value, stamp),
            )
        self._db.execute(
            "INSERT INTO story_evidence (evidence_id, story_id, source_url, revision_sha, "
            "manifest_sha256, angle, angle_fingerprint, support, refs_json, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid,
                sid,
                observation.source_url,
                observation.revision_sha,
                observation.manifest_sha256,
                observation.angle,
                fingerprint,
                observation.support,
                _refs_json(observation.refs),
                stamp,
            ),
        )
        return (RecordOutcome.EVIDENCE_ADDED if known else RecordOutcome.NEW), sid

    def mark_offered(self, sid: str, now: datetime) -> Story:
        """Record that a story was handed to editorial work. Idempotent: offering an offered story
        keeps its first `offered_at`. This is not drafting, approval or publication."""
        try:
            self._db.execute(
                "UPDATE stories SET state = ?, offered_at = ? WHERE story_id = ? AND state = ?",
                (StoryState.OFFERED.value, _stamp(now), sid, StoryState.AVAILABLE.value),
            )
        except sqlite3.Error:
            raise StoryError(_E.BACKLOG_WRITE_FAILED) from None
        story = self.get(sid)
        if story is None:
            raise StoryError(_E.STORY_UNKNOWN)
        return story

    def get(self, sid: str) -> Story | None:
        found = self._stories("WHERE s.story_id = ?", (sid,))
        return found[0] if found else None

    def stories(self) -> tuple[Story, ...]:
        """Every story, in every state, ordered by first sighting, repository and key."""
        return self._stories("", ())

    def _stories(self, where: str, params: tuple[str, ...]) -> tuple[Story, ...]:
        try:
            rows = self._db.execute(
                "SELECT s.story_id, s.repo_id, s.story_key, s.state, s.first_seen_at, "
                "s.offered_at, (SELECT count(*) FROM story_evidence c WHERE c.story_id = "
                "s.story_id), e.evidence_id, e.source_url, e.revision_sha, e.manifest_sha256, "
                "e.angle, e.support, e.refs_json, e.observed_at FROM stories s "
                "JOIN story_evidence e ON e.seq = (SELECT max(seq) FROM story_evidence m "
                f"WHERE m.story_id = s.story_id) {where} "
                "ORDER BY s.first_seen_at, s.repo_id, s.story_key",
                params,
            ).fetchall()
        except sqlite3.Error:
            raise StoryError(_E.BACKLOG_UNUSABLE) from None
        try:
            return tuple(
                Story(
                    story_id=r[0],
                    repo_id=r[1],
                    story_key=r[2],
                    state=StoryState(r[3]),
                    first_seen_at=r[4],
                    offered_at=r[5],
                    evidence_count=r[6],
                    latest=Evidence(
                        evidence_id=r[7],
                        source_url=r[8],
                        revision_sha=r[9],
                        manifest_sha256=r[10],
                        angle=r[11],
                        support=r[12],
                        refs=tuple(EvidenceRef(**ref) for ref in json.loads(r[13])),
                        observed_at=r[14],
                    ),
                )
                for r in rows
            )
        except (ValueError, TypeError):
            raise StoryError(_E.BACKLOG_UNUSABLE) from None
