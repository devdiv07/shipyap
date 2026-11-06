"""Durable approvals and send intents in one local SQLite file (standard library only).

Guarantees this module provides, each enforced by the database and not only by callers:
- An approval row can never be updated or deleted (triggers), so approved text cannot change.
- There is at most ONE intent per approval (primary key), so one approval is never submitted
  twice, and `begin_send` commits the intent (`synchronous=FULL`) before the caller may send.
- Intents are never deleted, and an intent in a final state is never changed.
- State changes are conditional updates (`WHERE state IN (...)`), so a stale or repeated writer
  cannot move an intent backwards or sideways.

- Review decisions (schema 2, Assignment 06E) are append-only: never updated or deleted. The latest
  decision for a story is its current one; an approval it does not name is withdrawn
  (`approval_withdrawn`), and `submit` refuses it, also inside the transaction that commits the
  intent. An approval made in review is bound to the reviewed bundle (`reviewed_drafts`), and an
  approval row approved from another bundle is never reused by review (APPROVAL_CONFLICT). A
  rejection is final for that story.
- A file is upgraded or accepted only if every table and trigger definition matches this module's
  (whitespace aside), not only their names.

What it does not provide: protection against anyone who can write the file (they can drop the
triggers or forge a row), and any authentication of who approved. See `publication.approval`.

States and what they claim:
- SENDING      the intent is committed and one provider request may have been sent. If the process
               died here, the post is POTENTIALLY SENT and is never resent automatically.
- ACCEPTED     the provider returned a post id for the request. Not confirmed as scheduled.
- AMBIGUOUS    the request may or may not have been processed (timeout, transport or unclear reply).
- REJECTED     the provider answered with a typed, definitive refusal; no post was created.
- SCHEDULED    a later provider read-back shows the exact text, channel and time with `scheduled`.
- NOT_SCHEDULED  read-back shows `needs_approval` or `draft`: the post exists but will not publish.
- SENT         the provider reports the post as `sent`.
- PROVIDER_ERROR  the provider reports the post as `error`; nothing is resent automatically.
- MISMATCH     read-back text, channel or time differs from what was approved.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from public_research_os.publication.approval import Approval, Candidate, text_sha256
from public_research_os.publication.errors import PublicationError, PublicationErrorCode

SCHEMA_VERSION = 2


class IntentState(StrEnum):
    SENDING = "SENDING"
    ACCEPTED = "ACCEPTED"
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"
    SCHEDULED = "SCHEDULED"
    NOT_SCHEDULED = "NOT_SCHEDULED"
    SENT = "SENT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    MISMATCH = "MISMATCH"


FINAL_STATES = frozenset({IntentState.SENT, IntentState.REJECTED, IntentState.PROVIDER_ERROR})
# A lead (LinkedIn) intent in one of these lets its X follow-on be sent: Buffer accepted it.
LEAD_ACCEPTED = frozenset({IntentState.ACCEPTED, IntentState.SCHEDULED, IntentState.SENT})
_S = IntentState
_FROM_SENDING = (_S.SENDING,)
_FROM_KNOWN = (_S.ACCEPTED, _S.AMBIGUOUS, _S.SCHEDULED, _S.NOT_SCHEDULED, _S.MISMATCH)
_ALLOWED_FROM: dict[IntentState, tuple[IntentState, ...]] = {
    _S.ACCEPTED: _FROM_SENDING,
    _S.REJECTED: _FROM_SENDING,
    _S.AMBIGUOUS: (_S.SENDING, _S.AMBIGUOUS),
    _S.SCHEDULED: _FROM_KNOWN,
    _S.NOT_SCHEDULED: _FROM_KNOWN,
    _S.SENT: _FROM_KNOWN,
    _S.PROVIDER_ERROR: _FROM_KNOWN,
    _S.MISMATCH: _FROM_KNOWN,
}


class ReviewKind(StrEnum):
    REVISED = "REVISED"  # the owner saved an edited text; nothing is approved
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


_SCHEMA_V1 = (
    """CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY CHECK (length(approval_id) = 64),
        text TEXT NOT NULL CHECK (length(text) > 0),
        text_sha256 TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        due_at TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        bundle_record_sha256 TEXT NOT NULL,
        approved_at TEXT NOT NULL
    )""",
    f"""CREATE TABLE intents (
        approval_id TEXT PRIMARY KEY REFERENCES approvals (approval_id),
        state TEXT NOT NULL CHECK (state IN ({", ".join(f"'{s.value}'" for s in IntentState)})),
        provider_post_id TEXT,
        detail_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER approvals_no_update BEFORE UPDATE ON approvals
       BEGIN SELECT RAISE(ABORT, 'approvals are immutable'); END""",
    """CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
       BEGIN SELECT RAISE(ABORT, 'approvals are permanent'); END""",
    """CREATE TRIGGER intents_no_delete BEFORE DELETE ON intents
       BEGIN SELECT RAISE(ABORT, 'intents are permanent'); END""",
    f"""CREATE TRIGGER intents_final BEFORE UPDATE ON intents
        WHEN OLD.state IN ({", ".join(f"'{s.value}'" for s in sorted(FINAL_STATES))})
        BEGIN SELECT RAISE(ABORT, 'final intents are immutable'); END""",
    """CREATE TRIGGER intents_identity BEFORE UPDATE ON intents
       WHEN NEW.approval_id IS NOT OLD.approval_id OR NEW.created_at IS NOT OLD.created_at
       BEGIN SELECT RAISE(ABORT, 'intent identity is immutable'); END""",
)
# Added by schema 2. `seq` is the rowid: rows are never deleted, so it only grows.
_SCHEMA_V2 = (
    f"""CREATE TABLE review_decisions (
        seq INTEGER PRIMARY KEY,
        story_id TEXT NOT NULL CHECK (length(story_id) = 64),
        decision TEXT NOT NULL CHECK (decision IN ({", ".join(f"'{k.value}'" for k in ReviewKind)})),
        approval_id TEXT REFERENCES approvals (approval_id),
        text TEXT NOT NULL,
        text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
        draft_bundle TEXT NOT NULL,
        draft_record_sha256 TEXT NOT NULL CHECK (length(draft_record_sha256) = 64),
        origin_record_sha256 TEXT NOT NULL CHECK (length(origin_record_sha256) = 64),
        link_sha256 TEXT NOT NULL CHECK (length(link_sha256) = 64),
        decided_at TEXT NOT NULL,
        CHECK ((decision = 'APPROVED') = (approval_id IS NOT NULL)),
        CHECK (decision != 'APPROVED' OR length(text) > 0)
    )""",
    """CREATE TRIGGER review_decisions_no_update BEFORE UPDATE ON review_decisions
       BEGIN SELECT RAISE(ABORT, 'review decisions are immutable'); END""",
    """CREATE TRIGGER review_decisions_no_delete BEFORE DELETE ON review_decisions
       BEGIN SELECT RAISE(ABORT, 'review decisions are permanent'); END""",
)
_SCHEMA = (*_SCHEMA_V1, *_SCHEMA_V2)


def _application_objects(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
    """(type, name, definition) of every schema object, except SQLite-managed ones (e.g.
    `sqlite_stat1` after ANALYZE, `sqlite_autoindex_*` for keys), which are not application schema.
    The definition is the stored CREATE statement with whitespace runs collapsed, so an altered
    table (e.g. `ALTER TABLE ... ADD COLUMN`) or a replaced trigger no longer matches."""
    return {
        (kind, name, " ".join((sql or "").split()))
        for kind, name, sql in connection.execute("SELECT type, name, sql FROM sqlite_master")
        if not name.startswith("sqlite_")
    }


def _expected_objects(schema: tuple[str, ...]) -> frozenset[tuple[str, str, str]]:
    """Exactly the objects `schema` creates; anything else (view, index, trigger) is refused."""
    connection = sqlite3.connect(":memory:")
    try:
        for statement in schema:
            connection.execute(statement)
        return frozenset(_application_objects(connection))
    finally:
        connection.close()


_EXPECTED_OBJECTS = _expected_objects(_SCHEMA)
_V1_OBJECTS = _expected_objects(_SCHEMA_V1)


@dataclass(frozen=True)
class Intent:
    approval_id: str
    state: IntentState
    provider_post_id: str | None
    detail_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewSubject:
    """What a review decision is about: one story's draft as the owner saw it. `draft_bundle` is
    the directory name of the reviewed draft bundle (the 06D one or an owner revision of it)."""

    story_id: str
    draft_bundle: str
    draft_record_sha256: str
    origin_record_sha256: str  # the 06D draft bundle record the review started from
    link_sha256: str  # story-link.json


@dataclass(frozen=True)
class ReviewDecision:
    seq: int
    story_id: str
    decision: ReviewKind
    approval_id: str | None
    text: str
    text_sha256: str
    draft_bundle: str
    draft_record_sha256: str
    origin_record_sha256: str
    link_sha256: str
    decided_at: str


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    @classmethod
    def open(cls, path: Path, *, create: bool) -> "Ledger":
        """`create=False` refuses a missing file instead of silently starting an empty ledger."""
        try:
            if not create and not path.is_file():
                raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE)
            connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        except (sqlite3.Error, OSError):
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            # The write lock comes first: a competing initializer (or upgrader) waits here and
            # then sees the committed identity, so exactly one store type can claim an empty file.
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = _application_objects(connection)
            if version == 0 and not objects and create:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1 and objects == _V1_OBJECTS:
                # Schema 1 -> 2 only adds the review table; no existing row is touched.
                for statement in _SCHEMA_V2:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or objects != _EXPECTED_OBJECTS:
                raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE)  # incl. mixed files
            connection.execute("COMMIT")
            connection.execute("SELECT count(*) FROM approvals").fetchone()
            connection.execute("SELECT count(*) FROM intents").fetchone()
            connection.execute("SELECT count(*) FROM review_decisions").fetchone()
        except PublicationError:
            connection.close()
            raise
        except sqlite3.Error:
            connection.close()
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None
        return cls(connection)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- approvals ---------------------------------------------------------------------------

    def add_approval(self, candidate: Candidate, approved_at: datetime) -> Approval:
        """Record an approval. Approving the identical post again returns the existing row."""
        row = _approval_row(candidate, approved_at)
        try:
            self._db.execute("INSERT OR IGNORE INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED) from None
        stored = self.get_approval(candidate.approval_id)
        if stored is None:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED)
        return stored

    def get_approval(self, approval_id: str) -> Approval | None:
        try:
            row = self._db.execute(
                "SELECT approval_id, text, text_sha256, channel_id, due_at, workspace_id, "
                "bundle_record_sha256, approved_at FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None
        return None if row is None else Approval(*row)

    # --- review decisions (schema 2) ---------------------------------------------------------

    def review_decisions(self, story_id: str) -> list[ReviewDecision]:
        """Every decision for the story, oldest first; the last one is current."""
        try:
            rows = self._db.execute(
                f"SELECT {_DECISION_COLUMNS} FROM review_decisions WHERE story_id = ? ORDER BY seq",
                (story_id,),
            ).fetchall()
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None
        return [_decision(row) for row in rows]

    def add_review_decision(
        self,
        subject: ReviewSubject,
        kind: ReviewKind,
        text: str,
        expected_seq: int,
        now: datetime,
    ) -> ReviewDecision:
        """Append a REVISED or REJECTED decision. `expected_seq` is the seq of the current
        decision the owner saw (0: none); any other current decision refuses (REVIEW_STALE).
        Repeating the current decision returns it instead of adding a row."""
        if kind == ReviewKind.APPROVED:
            raise ValueError("approvals are recorded with add_reviewed_approval")
        return self._decide(subject, kind, text, None, expected_seq, now)

    def add_reviewed_approval(
        self,
        subject: ReviewSubject,
        candidate: Candidate,
        expected_seq: int,
        now: datetime,
        follow_on: Candidate | None = None,
    ) -> tuple[Approval, ReviewDecision]:
        """Record the approval and its APPROVED decision in ONE transaction, so an approval made in
        review never exists without the decision that can later withdraw it.

        `follow_on` (the X post derived from the same draft, same time) is recorded in the same
        transaction. No decision names it: it stands only while `candidate`'s approval does, which
        `begin_send(..., lead_approval_id=...)` checks."""
        if candidate.bundle_record_sha256 != subject.draft_record_sha256:
            raise ValueError("the candidate is not the reviewed draft")  # programming error
        if follow_on is not None and (
            follow_on.bundle_record_sha256 != candidate.bundle_record_sha256
            or follow_on.due_at != candidate.due_at
            or follow_on.channel_id == candidate.channel_id
        ):
            raise ValueError("the follow-on is not the same draft and time")  # programming error
        decision = self._decide(
            subject, ReviewKind.APPROVED, candidate.text, candidate, expected_seq, now, follow_on
        )
        approval = self.get_approval(candidate.approval_id)
        if approval is None:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED)
        return approval, decision

    def approval_withdrawn(self, approval_id: str) -> bool:
        """True when a story's review named this approval and its current decision no longer
        does (a later edit, rejection or other approval). An approval never recorded through
        review (publish-approve) has no decisions and is not withdrawn."""
        return any(current != approval_id for current, _ in self._current_reviews(approval_id))

    def reviewed_drafts(self, approval_id: str) -> frozenset[str]:
        """The draft bundle record fingerprints the current review decisions approved with this
        approval; empty when review never named it (a publish-approve approval)."""
        return frozenset(draft for _, draft in self._current_reviews(approval_id))

    def _current_reviews(self, approval_id: str) -> list[tuple[str | None, str]]:
        """(approval_id, draft_record_sha256) of the current decision of every story whose review
        ever named this approval."""
        try:
            return self._db.execute(
                "SELECT approval_id, draft_record_sha256 FROM review_decisions WHERE seq IN ("
                "SELECT max(seq) FROM review_decisions WHERE story_id IN ("
                "SELECT story_id FROM review_decisions WHERE approval_id = ?) GROUP BY story_id)",
                (approval_id,),
            ).fetchall()
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None

    def _decide(
        self,
        subject: ReviewSubject,
        kind: ReviewKind,
        text: str,
        candidate: Candidate | None,
        expected_seq: int,
        now: datetime,
        follow_on: Candidate | None = None,
    ) -> ReviewDecision:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._db.execute(
                    f"SELECT {_DECISION_COLUMNS} FROM review_decisions WHERE story_id = ? "
                    "ORDER BY seq DESC LIMIT 1",
                    (subject.story_id,),
                ).fetchone()
                latest = None if current is None else _decision(current)
                if latest is not None and _repeats(latest, subject, kind, candidate):
                    self._db.execute("COMMIT")
                    return latest  # the same action again: no second row
                if (latest.seq if latest else 0) != expected_seq:
                    raise PublicationError(PublicationErrorCode.REVIEW_STALE)
                if latest is not None and latest.decision == ReviewKind.REJECTED:
                    raise PublicationError(PublicationErrorCode.REVIEW_CLOSED)
                approval_id = None
                if candidate is not None:
                    approval_id = candidate.approval_id
                    for bound in (candidate, follow_on):
                        if bound is not None:
                            self._insert_approval(bound, now)
                cursor = self._db.execute(
                    "INSERT INTO review_decisions (story_id, decision, approval_id, text, "
                    "text_sha256, draft_bundle, draft_record_sha256, origin_record_sha256, "
                    "link_sha256, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        subject.story_id,
                        kind.value,
                        approval_id,
                        text,
                        text_sha256(text),
                        subject.draft_bundle,
                        subject.draft_record_sha256,
                        subject.origin_record_sha256,
                        subject.link_sha256,
                        stamp(now),
                    ),
                )
                row = self._db.execute(
                    f"SELECT {_DECISION_COLUMNS} FROM review_decisions WHERE seq = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED) from None
        return _decision(row)

    def _insert_approval(self, candidate: Candidate, now: datetime) -> None:
        row = _approval_row(candidate, now)
        existing = self._db.execute(
            "SELECT approval_id, text, text_sha256, channel_id, due_at, workspace_id, "
            "bundle_record_sha256 FROM approvals WHERE approval_id = ?",
            (candidate.approval_id,),
        ).fetchone()
        if existing is None:
            self._db.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
        elif tuple(existing) != row[:7]:
            # Same text, channel and time approved from another bundle (e.g. text restored
            # after an edit). The immutable row cannot name this draft, so nothing is recorded
            # rather than a decision the row contradicts.
            raise PublicationError(PublicationErrorCode.APPROVAL_CONFLICT)

    # --- intents -----------------------------------------------------------------------------

    def get_intent(self, approval_id: str) -> Intent | None:
        try:
            row = self._db.execute(
                "SELECT approval_id, state, provider_post_id, detail_code, created_at, "
                "updated_at FROM intents WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None
        return None if row is None else _intent(row)

    def list_intents(self) -> list[Intent]:
        try:
            rows = self._db.execute(
                "SELECT approval_id, state, provider_post_id, detail_code, created_at, "
                "updated_at FROM intents ORDER BY created_at, approval_id"
            ).fetchall()
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_UNUSABLE) from None
        return [_intent(row) for row in rows]

    def begin_send(
        self,
        approval_id: str,
        now: datetime,
        bundle_record_sha256: str | None = None,
        lead_approval_id: str | None = None,
    ) -> Intent:
        """Commit the SENDING intent. Returns only after the commit; the caller sends afterwards.

        Raises DUPLICATE_SUBMISSION if this approval has ever had an intent, in any state. The
        review checks run again inside this transaction (review decisions take the same write
        lock), so a rejection or edit committed after the caller's early checks still stops the
        send: APPROVAL_WITHDRAWN, or APPROVAL_NOT_REVIEWED_DRAFT when review approved it for a
        bundle other than `bundle_record_sha256`.

        With `lead_approval_id` (an X post following its LinkedIn post), the same review checks
        apply to the lead approval, and the lead must have an intent Buffer accepted
        (LEAD_NOT_ACCEPTED otherwise)."""
        stamped = stamp(now)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if self._db.execute(
                    "SELECT 1 FROM intents WHERE approval_id = ?", (approval_id,)
                ).fetchone():
                    raise PublicationError(PublicationErrorCode.DUPLICATE_SUBMISSION)
                if not self._db.execute(
                    "SELECT 1 FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone():
                    raise PublicationError(PublicationErrorCode.APPROVAL_NOT_FOUND)
                for bound in (approval_id, lead_approval_id):
                    if bound is None:
                        continue
                    if self.approval_withdrawn(bound):
                        raise PublicationError(PublicationErrorCode.APPROVAL_WITHDRAWN)
                    reviewed = self.reviewed_drafts(bound)
                    if reviewed and reviewed != {bundle_record_sha256}:
                        raise PublicationError(PublicationErrorCode.APPROVAL_NOT_REVIEWED_DRAFT)
                if lead_approval_id is not None:
                    lead = self._db.execute(
                        "SELECT state FROM intents WHERE approval_id = ?", (lead_approval_id,)
                    ).fetchone()
                    if lead is None or IntentState(lead[0]) not in LEAD_ACCEPTED:
                        raise PublicationError(PublicationErrorCode.LEAD_NOT_ACCEPTED)
                self._db.execute(
                    "INSERT INTO intents (approval_id, state, provider_post_id, detail_code, "
                    "created_at, updated_at) VALUES (?, 'SENDING', NULL, NULL, ?, ?)",
                    (approval_id, stamped, stamped),
                )
                self._db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED) from None
        return Intent(approval_id, IntentState.SENDING, None, None, stamped, stamped)

    def transition(
        self,
        approval_id: str,
        to_state: IntentState,
        now: datetime,
        *,
        post_id: str | None = None,
        code: str | None = None,
    ) -> None:
        allowed = _ALLOWED_FROM[to_state]
        marks = ", ".join("?" for _ in allowed)
        try:
            cursor = self._db.execute(
                "UPDATE intents SET state = ?, provider_post_id = COALESCE(provider_post_id, ?), "
                f"detail_code = ?, updated_at = ? WHERE approval_id = ? AND state IN ({marks})",
                (
                    to_state.value,
                    post_id,
                    code,
                    stamp(now),
                    approval_id,
                    *(s.value for s in allowed),
                ),
            )
        except sqlite3.Error:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED) from None
        if cursor.rowcount != 1:
            raise PublicationError(PublicationErrorCode.LEDGER_WRITE_FAILED)

    def _rollback(self) -> None:
        try:
            self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _intent(row: tuple) -> Intent:
    return Intent(row[0], IntentState(row[1]), row[2], row[3], row[4], row[5])


def _approval_row(candidate: Candidate, approved_at: datetime) -> tuple:
    return (
        candidate.approval_id,
        candidate.text,
        text_sha256(candidate.text),
        candidate.channel_id,
        candidate.due_at,
        candidate.workspace_id,
        candidate.bundle_record_sha256,
        stamp(approved_at),
    )


_DECISION_COLUMNS = (
    "seq, story_id, decision, approval_id, text, text_sha256, draft_bundle, draft_record_sha256, "
    "origin_record_sha256, link_sha256, decided_at"
)


def _decision(row: tuple) -> ReviewDecision:
    return ReviewDecision(row[0], row[1], ReviewKind(row[2]), *row[3:])


def _repeats(
    latest: ReviewDecision, subject: ReviewSubject, kind: ReviewKind, candidate: Candidate | None
) -> bool:
    """The requested decision is the current one already: same kind, same draft, same approval."""
    return (
        latest.decision == kind
        and latest.draft_record_sha256 == subject.draft_record_sha256
        and latest.draft_bundle == subject.draft_bundle
        and latest.approval_id == (candidate.approval_id if candidate else None)
    )
