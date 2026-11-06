"""Offline publication identity, lexical duplicate policy and single-cycle reservations.

All methods that mutate history are called inside the ledger's write transaction. Hashes bind
local records; neither these records nor the typed phrase authenticate a person.
"""

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from shipyap.publication.approval import Candidate
from shipyap.publication.errors import PublicationError
from shipyap.publication.errors import PublicationErrorCode as E

POLICY = "lexical-v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")

SCHEMA = (
    """CREATE TABLE publication_bindings (
        approval_id TEXT PRIMARY KEY REFERENCES approvals(approval_id),
        story_id TEXT NOT NULL CHECK(length(story_id) = 64),
        cycle_id TEXT NOT NULL REFERENCES approvals(approval_id),
        role TEXT NOT NULL CHECK(role IN ('lead', 'follow_on')),
        CHECK((role = 'lead') = (approval_id = cycle_id))
    )""",
    """CREATE TABLE repost_grants (
        approval_id TEXT PRIMARY KEY REFERENCES approvals(approval_id),
        story_id TEXT NOT NULL CHECK(length(story_id) = 64),
        bundle_record_sha256 TEXT NOT NULL,
        history_sha256 TEXT NOT NULL,
        confirmed_at TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin IN ('review', 'service'))
    )""",
    """CREATE TABLE publication_reservations (
        approval_id TEXT PRIMARY KEY REFERENCES intents(approval_id),
        cycle_id TEXT NOT NULL REFERENCES approvals(approval_id),
        channel_id TEXT NOT NULL,
        reserved_at TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin IN ('review', 'service', 'legacy')),
        UNIQUE(cycle_id, channel_id)
    )""",
    *(
        f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'publication history is permanent and immutable'); END"
        for table in ("publication_bindings", "repost_grants", "publication_reservations")
        for action in ("UPDATE", "DELETE")
    ),
)


@dataclass(frozen=True)
class PriorPost:
    approval_id: str
    story_id: str
    cycle_id: str
    channel_id: str
    due_at: str
    text: str
    state: str
    post_id: str | None
    created_at: str
    updated_at: str
    rule: str = "story"
    score: float = 1.0


def tokens(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = "".join(
        ""
        if unicodedata.category(char) == "Cf"
        else " "
        if unicodedata.category(char).startswith("P")
        else char
        for char in normalized
    )
    return tuple(re.findall(r"\w+", normalized))


def similarity(left: str, right: str) -> tuple[str, float] | None:
    """Lexical near-identity, not semantic paraphrase detection. No model or network."""
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return None
    if a == b:
        return "normalized_equal", 1.0
    score = SequenceMatcher(None, a, b, autojunk=False).ratio()
    if score >= 0.90:
        return "token_similarity", score
    short, long = sorted((a, b), key=len)
    if len(short) >= 40:
        small = {short[i : i + 5] for i in range(len(short) - 4)}
        large = {long[i : i + 5] for i in range(len(long) - 4)}
        overlap = len(small & large) / len(small)
        if overlap >= 0.90:
            return "shingle_containment", overlap
    return None


def repost_phrase(story_id: str, candidate: Candidate) -> str:
    return f"REPOST {story_id[:16]} {candidate.approval_id[:12]}"


def fingerprint(posts: tuple[PriorPost, ...]) -> str:
    # Reconciliation may change state while a human reads. Identity/text, not that mutable
    # observation, is bound. A newly reserved matching post always changes this fingerprint.
    rows = sorted(
        (p.approval_id, p.story_id, p.channel_id, p.due_at, p.text, p.created_at) for p in posts
    )
    raw = json.dumps([POLICY, rows], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class History:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def bind(self, approval_id: str, story_id: str, cycle_id: str) -> None:
        if _HEX.fullmatch(story_id) is None:
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        role = "lead" if approval_id == cycle_id else "follow_on"
        row = (approval_id, story_id, cycle_id, role)
        stored = self.binding(approval_id)
        if stored is not None and stored != row:
            raise PublicationError(E.APPROVAL_CONFLICT)
        self.db.execute("INSERT OR IGNORE INTO publication_bindings VALUES (?, ?, ?, ?)", row)

    def binding(self, approval_id: str) -> tuple | None:
        row = self.db.execute(
            "SELECT approval_id, story_id, cycle_id, role FROM publication_bindings "
            "WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        return tuple(row) if row is not None else None

    def posts(self, story_id: str | None = None) -> tuple[PriorPost, ...]:
        # Unknown historical sends must not disappear from the comparison corpus.
        if self.db.execute(
            "SELECT 1 FROM intents i LEFT JOIN publication_bindings b USING(approval_id) "
            "WHERE b.approval_id IS NULL LIMIT 1"
        ).fetchone():
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        rows = self.db.execute(
            "SELECT a.approval_id, b.story_id, b.cycle_id, a.channel_id, a.due_at, a.text, "
            "i.state, i.provider_post_id, i.created_at, i.updated_at "
            "FROM approvals a JOIN intents i USING(approval_id) "
            "JOIN publication_bindings b USING(approval_id) "
            "WHERE (? IS NULL OR b.story_id = ?) ORDER BY i.created_at, a.approval_id",
            (story_id, story_id),
        ).fetchall()
        return tuple(PriorPost(*r) for r in rows)

    def conflicts(self, story_id: str, text: str, cycle_id: str) -> tuple[PriorPost, ...]:
        found = []
        for post in self.posts():
            if post.cycle_id == cycle_id:
                continue  # only the persisted lead/follow-on pair shares a cycle
            match = ("story", 1.0) if post.story_id == story_id else similarity(text, post.text)
            if match:
                found.append(
                    PriorPost(
                        post.approval_id,
                        post.story_id,
                        post.cycle_id,
                        post.channel_id,
                        post.due_at,
                        post.text,
                        post.state,
                        post.post_id,
                        post.created_at,
                        post.updated_at,
                        *match,
                    )
                )
        return tuple(found)

    def cycle_conflicts(self, cycle_id: str) -> tuple[PriorPost, ...]:
        found = {}
        for story, text in self.db.execute(
            "SELECT b.story_id, a.text FROM publication_bindings b JOIN approvals a USING(approval_id) WHERE b.cycle_id=?",
            (cycle_id,),
        ):
            for post in self.conflicts(story, text, cycle_id):
                found[post.approval_id] = post
        return tuple(found.values())

    def check(self, approval_id: str, lead_id: str | None = None) -> tuple[PriorPost, ...]:
        binding = self.binding(approval_id)
        if binding is None:
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        _, story, cycle, role = binding
        if (role == "follow_on") != (lead_id is not None) or (lead_id and lead_id != cycle):
            raise PublicationError(E.APPROVAL_MISMATCH)
        if lead_id and self.binding(lead_id) != (lead_id, story, cycle, "lead"):
            raise PublicationError(E.APPROVAL_MISMATCH)
        conflicts = self.cycle_conflicts(cycle)
        if conflicts:
            grant = self.db.execute(
                "SELECT story_id, bundle_record_sha256, history_sha256 FROM repost_grants "
                "WHERE approval_id=?",
                (cycle,),
            ).fetchone()
            bundle = self.db.execute(
                "SELECT bundle_record_sha256 FROM approvals WHERE approval_id=?", (cycle,)
            ).fetchone()[0]
            if grant is None or tuple(grant) != (story, bundle, fingerprint(conflicts)):
                code = (
                    E.STORY_ALREADY_SUBMITTED
                    if any(p.story_id == story for p in conflicts)
                    else E.NEAR_DUPLICATE_POST
                )
                raise PublicationError(code)
        return conflicts

    def reserve(self, approval_id: str, now: str, origin: str) -> None:
        binding = self.binding(approval_id)
        if binding is None:
            raise PublicationError(E.PUBLICATION_IDENTITY_REQUIRED)
        channel = self.db.execute(
            "SELECT channel_id FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()[0]
        if self.db.execute(
            "SELECT 1 FROM publication_reservations WHERE cycle_id=? AND channel_id=?",
            (binding[2], channel),
        ).fetchone():
            raise PublicationError(E.DUPLICATE_SUBMISSION)
        self.db.execute(
            "INSERT INTO publication_reservations VALUES (?, ?, ?, ?, ?)",
            (approval_id, binding[2], channel, now, origin),
        )
