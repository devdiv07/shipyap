"""The SQLite ledger: immutable approvals, one intent per approval, guarded state changes."""

import sqlite3
from datetime import timedelta

import pytest
from publication_fake import CHANNEL, DUE, NOW, POST_ID, POST_TEXT

from shipyap.publication.approval import Candidate
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.ledger import FINAL_STATES, IntentState, Ledger

E = PublicationErrorCode
S = IntentState


def refused(call, *args, **kwargs) -> PublicationErrorCode:
    with pytest.raises(PublicationError) as raised:
        call(*args, **kwargs)
    return raised.value.code


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.sqlite"


@pytest.fixture
def ledger(path):
    with Ledger.open(path, create=True) as opened:
        yield opened


@pytest.fixture
def candidate():
    return Candidate(POST_TEXT, CHANNEL, DUE, "demo_workspace", "e" * 64)


@pytest.fixture
def approved(ledger, candidate):
    return ledger.add_approval(candidate, NOW).approval_id


def raw(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def test_the_approved_text_round_trips_byte_for_byte(path, ledger, candidate):
    exotic = Candidate("é \U0001f600 一\n\nline", CHANNEL, DUE, "demo_workspace", "e" * 64)
    ledger.add_approval(exotic, NOW)
    stored = Ledger.open(path, create=False).get_approval(exotic.approval_id)
    assert stored.text.encode("utf-8") == exotic.text.encode("utf-8")


def test_an_approval_row_cannot_be_changed_or_removed(path, approved):
    db = raw(path)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute("UPDATE approvals SET text = 'changed'")
    with pytest.raises(sqlite3.DatabaseError, match="permanent"):
        db.execute("DELETE FROM approvals")


def test_an_intent_is_committed_before_return_and_visible_to_another_connection(
    path, ledger, approved
):
    ledger.begin_send(approved, NOW)
    other = Ledger.open(path, create=False)
    intent = other.get_intent(approved)
    assert intent.state == S.SENDING and intent.provider_post_id is None


def test_a_second_intent_for_the_same_approval_is_refused_in_every_state(ledger, approved):
    ledger.begin_send(approved, NOW)
    assert refused(ledger.begin_send, approved, NOW) == E.DUPLICATE_SUBMISSION
    ledger.transition(approved, S.REJECTED, NOW, code="InvalidInputError")
    assert refused(ledger.begin_send, approved, NOW) == E.DUPLICATE_SUBMISSION
    assert len(ledger.list_intents()) == 1


def test_an_intent_needs_an_approval(ledger):
    assert refused(ledger.begin_send, "f" * 64, NOW) == E.APPROVAL_NOT_FOUND
    assert ledger.list_intents() == []


def test_the_database_itself_rejects_a_second_intent_row(path, approved, ledger):
    ledger.begin_send(approved, NOW)
    with pytest.raises(sqlite3.IntegrityError):
        raw(path).execute(
            "INSERT INTO intents VALUES (?, 'SENDING', NULL, NULL, 'x', 'x')", (approved,)
        )


def test_intents_are_never_deleted_and_final_ones_never_change(path, ledger, approved):
    ledger.begin_send(approved, NOW)
    ledger.transition(approved, S.ACCEPTED, NOW, post_id=POST_ID, code="ACCEPTED")
    ledger.transition(approved, S.SENT, NOW, code="PROVIDER_REPORTS_SENT")
    db = raw(path)
    with pytest.raises(sqlite3.DatabaseError, match="permanent"):
        db.execute("DELETE FROM intents")
    with pytest.raises(sqlite3.DatabaseError, match="final intents"):
        db.execute("UPDATE intents SET state = 'SCHEDULED'")
    assert refused(ledger.transition, approved, S.SCHEDULED, NOW) == E.LEDGER_WRITE_FAILED


@pytest.mark.parametrize(
    ("start", "target", "allowed"),
    [
        (S.SENDING, S.ACCEPTED, True),
        (S.SENDING, S.REJECTED, True),
        (S.SENDING, S.AMBIGUOUS, True),
        (S.SENDING, S.SCHEDULED, False),  # nothing is confirmed without a read-back
        (S.SENDING, S.SENT, False),
        (S.ACCEPTED, S.SCHEDULED, True),
        (S.ACCEPTED, S.REJECTED, False),
        (S.ACCEPTED, S.SENDING, None),
        (S.AMBIGUOUS, S.SCHEDULED, True),
        (S.AMBIGUOUS, S.ACCEPTED, False),
        (S.SCHEDULED, S.SENT, True),
        (S.SCHEDULED, S.AMBIGUOUS, False),
        (S.NOT_SCHEDULED, S.SCHEDULED, True),
    ],
)
def test_state_changes_follow_the_allowed_graph(ledger, approved, start, target, allowed):
    ledger.begin_send(approved, NOW)
    path_to = {
        S.SENDING: [],
        S.ACCEPTED: [S.ACCEPTED],
        S.AMBIGUOUS: [S.AMBIGUOUS],
        S.SCHEDULED: [S.ACCEPTED, S.SCHEDULED],
        S.NOT_SCHEDULED: [S.ACCEPTED, S.NOT_SCHEDULED],
    }[start]
    for step in path_to:
        ledger.transition(approved, step, NOW)
    if allowed is None:  # SENDING is not a reachable target at all
        with pytest.raises(KeyError):
            ledger.transition(approved, target, NOW)
    elif allowed:
        ledger.transition(approved, target, NOW)
        assert ledger.get_intent(approved).state == target
    else:
        assert refused(ledger.transition, approved, target, NOW) == E.LEDGER_WRITE_FAILED
        assert ledger.get_intent(approved).state == start


def test_the_post_id_once_recorded_is_not_replaced(ledger, approved):
    ledger.begin_send(approved, NOW)
    ledger.transition(approved, S.ACCEPTED, NOW, post_id=POST_ID)
    ledger.transition(approved, S.SCHEDULED, NOW, post_id="f0" * 12)
    assert ledger.get_intent(approved).provider_post_id == POST_ID


def test_final_states_are_the_expected_ones():
    assert FINAL_STATES == {S.SENT, S.REJECTED, S.PROVIDER_ERROR}


def test_a_missing_ledger_is_not_silently_created_for_scheduling(path):
    assert refused(Ledger.open, path, create=False) == E.LEDGER_UNUSABLE
    assert not path.exists()


def test_a_foreign_database_is_refused(path):
    other = sqlite3.connect(path)
    other.execute("CREATE TABLE unrelated (x)")
    other.commit()
    other.close()
    assert refused(Ledger.open, path, create=True) == E.LEDGER_UNUSABLE


def test_a_file_that_is_not_a_database_is_refused(path):
    path.write_bytes(b"not a database" * 100)
    assert refused(Ledger.open, path, create=True) == E.LEDGER_UNUSABLE


def test_timestamps_are_utc_seconds(ledger, approved):
    ledger.begin_send(approved, NOW + timedelta(seconds=5))
    assert ledger.get_intent(approved).created_at == "2026-11-01T12:00:05Z"
