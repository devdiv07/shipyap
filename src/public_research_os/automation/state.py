"""Local evidence cursors and durable generation attempts, separate from the story backlog.

SQLite FULL synchronous commits precede billable work. The independent SQLite lock is held for
the entire manual run; the OS releases it on process exit, including a crash. Local disks only.
Attempts are never deleted or automatically reset. This is not a scheduler or a job framework.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from public_research_os.automation.config import AutomationError


@contextmanager
def run_lock(path: Path):
    connection = sqlite3.connect(path, timeout=0)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            raise AutomationError("RUN_LOCKED") from None
        yield
    finally:
        connection.close()


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS progress (
                repo TEXT PRIMARY KEY, revision TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                repo TEXT NOT NULL, revision TEXT NOT NULL, directory TEXT NOT NULL,
                PRIMARY KEY(repo, revision));
            CREATE TABLE IF NOT EXISTS generation_attempts (
                repo TEXT NOT NULL, revision TEXT NOT NULL, request_hash TEXT NOT NULL,
                request_json TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                estimated_usd TEXT NOT NULL, status TEXT NOT NULL, reply_json TEXT,
                PRIMARY KEY(repo, revision));
            CREATE TABLE IF NOT EXISTS findings_dirs (
                repo TEXT NOT NULL, revision TEXT NOT NULL, directory TEXT NOT NULL,
                PRIMARY KEY(repo, revision));
            CREATE TABLE IF NOT EXISTS set_aside (
                story_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, set_aside_at TEXT NOT NULL,
                PRIMARY KEY(story_id, manifest_sha256));
            CREATE TABLE IF NOT EXISTS spending (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, request_hash TEXT NOT NULL,
                estimated_usd TEXT NOT NULL, attempted_at TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def findings_dir(self, repo: str, revision: str) -> Path | None:
        row = self.db.execute(
            "SELECT directory FROM findings_dirs WHERE repo=? AND revision=?", (repo, revision)
        ).fetchone()
        return Path(row[0]) if row else None

    def set_findings_dir(self, repo: str, revision: str, directory: Path) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO findings_dirs VALUES (?, ?, ?)",
                (repo, revision, str(directory)),
            )

    def snapshot_dir(self, repo: str, revision: str) -> Path | None:
        row = self.db.execute(
            "SELECT directory FROM snapshots WHERE repo=? AND revision=?", (repo, revision)
        ).fetchone()
        return Path(row[0]) if row else None

    def reexamine(self, repo: str, story_ids_and_manifests, now: str) -> None:
        """The owner asked to read `repo` again with the current selection. Its finished
        generation attempt is kept under an archived key (history and spending stay), its cursor
        is cleared, and its waiting stories are set aside for the evidence they were found in:
        a story the new reading finds again (new evidence) becomes eligible again. An unfinished
        attempt is never touched: it needs recovery or the owner first."""
        with self.db:
            pending = self.db.execute(
                "SELECT revision FROM generation_attempts WHERE repo=? "
                "AND status NOT IN ('COMPLETED', 'NO_FINDINGS')",
                (repo,),
            ).fetchone()
            if pending is not None:
                raise ValueError("unfinished generation attempt")
            for (revision,) in self.db.execute(
                "SELECT revision FROM generation_attempts WHERE repo=? AND revision NOT LIKE '%@%'",
                (repo,),
            ).fetchall():
                self.db.execute(
                    "UPDATE generation_attempts SET revision=? WHERE repo=? AND revision=?",
                    (f"{revision}@archived-{now}", repo, revision),
                )
            self.db.execute("DELETE FROM progress WHERE repo=?", (repo,))
            self.db.execute("DELETE FROM findings_dirs WHERE repo=?", (repo,))
            self.db.executemany(
                "INSERT OR IGNORE INTO set_aside VALUES (?, ?, ?)",
                [(sid, manifest, now) for sid, manifest in story_ids_and_manifests],
            )

    def set_aside(self) -> set[tuple[str, str]]:
        return {
            (r[0], r[1]) for r in self.db.execute("SELECT story_id, manifest_sha256 FROM set_aside")
        }

    def cursor(self, repo: str) -> str | None:
        row = self.db.execute("SELECT revision FROM progress WHERE repo=?", (repo,)).fetchone()
        return row[0] if row else None

    def complete(self, repo: str, revision: str, status: str | None = None):
        with self.db:
            if status:
                self.db.execute(
                    "UPDATE generation_attempts SET status=? WHERE repo=? AND revision=?",
                    (status, repo, revision),
                )
            self.db.execute("INSERT OR REPLACE INTO progress VALUES (?, ?)", (repo, revision))

    def remember(self, repo: str, revision: str, directory: Path):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                (repo, revision, str(directory)),
            )

    def snapshots(self) -> list[Path]:
        return [
            Path(r[0]) for r in self.db.execute("SELECT directory FROM snapshots ORDER BY rowid")
        ]

    def pending(self, repo: str):
        return self.db.execute(
            "SELECT * FROM generation_attempts WHERE repo=? "
            "AND status NOT IN ('COMPLETED', 'NO_FINDINGS') ORDER BY rowid LIMIT 1",
            (repo,),
        ).fetchone()

    def attempt(self, repo, revision, digest, request, tokens, estimate):
        with self.db:
            self.db.execute(
                "INSERT INTO generation_attempts VALUES (?, ?, ?, ?, ?, ?, 'UNCERTAIN', NULL)",
                (repo, revision, digest, json.dumps(request), tokens, str(estimate)),
            )

    def responded(self, repo, revision, reply):
        with self.db:
            self.db.execute(
                "UPDATE generation_attempts SET status='RESPONDED', reply_json=? "
                "WHERE repo=? AND revision=?",
                (json.dumps(reply), repo, revision),
            )

    def failed(self, repo, revision):
        with self.db:
            self.db.execute(
                "UPDATE generation_attempts SET status='FAILED' WHERE repo=? AND revision=?",
                (repo, revision),
            )
