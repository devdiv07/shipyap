"""Repository publishing modes: normalization, persistence, eligibility and isolation from the
publisher ledger. Offline; every database is a temporary local file."""

import hashlib
import sqlite3
from datetime import timedelta

import pytest
from publication_fake import CHANNEL, DUE, NOW, POST_TEXT

from shipyap.preferences import repositories as preferences_module
from shipyap.preferences.errors import PreferencesError, PreferencesErrorCode
from shipyap.preferences.repositories import (
    Mode,
    RepositoryPreferences,
    normalize_repository,
)
from shipyap.publication import ledger as ledger_module
from shipyap.publication.approval import Candidate
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.ledger import IntentState, Ledger

E = PreferencesErrorCode
HISTORIC = "sample-owner/capture-lab"


def refused(call, *args, **kwargs) -> PreferencesErrorCode:
    with pytest.raises(PreferencesError) as raised:
        call(*args, **kwargs)
    return raised.value.code


@pytest.fixture
def path(tmp_path):
    return tmp_path / "preferences.sqlite"


@pytest.fixture
def store(path):
    with RepositoryPreferences.open(path, create=True) as opened:
        yield opened


def rows(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT * FROM repository_preferences ORDER BY repo_id"
        ).fetchall()
    finally:
        connection.close()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- persistence -------------------------------------------------------------------------------


def test_a_focus_repository_survives_closing_and_reopening_the_storage(path):
    with RepositoryPreferences.open(path, create=True) as first:
        first.set_mode(HISTORIC, "focus", NOW)
    with RepositoryPreferences.open(path, create=False) as second:
        (saved,) = second.list()
    assert (saved.repo_id, saved.mode) == (HISTORIC, Mode.FOCUS)


def test_several_repositories_keep_their_own_modes_in_a_deterministic_order(store):
    store.set_mode("zed/last", "off", NOW)
    store.set_mode("acme/first", "focus", NOW)
    store.set_mode("acme/second", "focus", NOW)
    store.set_mode("mid/occasional", "occasional", NOW)
    listed = [(item.repo_id, item.mode) for item in store.list()]
    assert listed == [
        ("acme/first", Mode.FOCUS),
        ("acme/second", Mode.FOCUS),
        ("mid/occasional", Mode.OCCASIONAL),
        ("zed/last", Mode.OFF),
    ]


def test_a_mode_can_change_focus_occasional_off_and_back_to_focus(path, store):
    created = store.set_mode(HISTORIC, "focus", NOW)
    seen = [created.mode]
    for mode in ("occasional", "off", "focus"):
        seen.append(store.set_mode(HISTORIC, mode, NOW + timedelta(hours=1)).mode)
    assert seen == [Mode.FOCUS, Mode.OCCASIONAL, Mode.OFF, Mode.FOCUS]
    (only,) = rows(path)
    assert only[0] == HISTORIC and only[1] == "focus"
    assert only[2] == created.created_at  # the record's identity survives every change


def test_setting_the_same_mode_again_is_idempotent(path, store):
    first = store.set_mode(HISTORIC, "occasional", NOW)
    again = store.set_mode(HISTORIC, "occasional", NOW + timedelta(days=1))
    assert again == first  # not even updated_at moves
    assert len(rows(path)) == 1
    store.set_mode(HISTORIC, "occasional", NOW + timedelta(days=2))
    assert len(rows(path)) == 1


def test_a_record_set_to_off_is_retained_and_never_deleted(path, store):
    store.set_mode(HISTORIC, "off", NOW)
    assert [item.repo_id for item in store.list()] == [HISTORIC]
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="permanent"):
            connection.execute("DELETE FROM repository_preferences")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE repository_preferences SET repo_id = 'a/b'")
    finally:
        connection.close()


# --- identity ----------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["Sample-Owner/Capture-Lab", "SAMPLE-OWNER/CAPTURE-LAB"])
def test_identifiers_differing_only_by_case_are_the_same_repository(path, store, variant):
    store.set_mode(HISTORIC, "focus", NOW)
    changed = store.set_mode(variant, "off", NOW + timedelta(hours=1))
    assert changed.repo_id == HISTORIC
    assert store.get(variant.swapcase()) == changed
    assert len(rows(path)) == 1


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "owner",
        "owner/",
        "/repo",
        "owner/repo/extra",
        "../repo",
        "owner/..",
        "owner/.",
        "owner/../x",
        "owner\\repo",
        "owner/repo ",
        " owner/repo",
        "owner/re po",
        "owner/repo\n",
        "owner/repo.git",
        "-owner/repo",
        "own_er/repo",
        "https://github.com/owner/repo",
        "owner/repo?x=1",
        "owner/ré",
    ],
    ids=lambda value: repr(value),
)
def test_malformed_and_traversal_like_identifiers_are_refused(path, store, value):
    store.set_mode("acme/kept", "focus", NOW)
    before = rows(path)
    assert refused(store.set_mode, value, "focus", NOW) == E.REPOSITORY_INVALID
    assert refused(normalize_repository, value) == E.REPOSITORY_INVALID
    assert rows(path) == before


@pytest.mark.parametrize("value", ["", "FOCUS", "Off", "all", "on", "focus ", None, 1])
def test_an_invalid_mode_is_refused_without_changing_stored_data(path, store, value):
    store.set_mode(HISTORIC, "focus", NOW)
    before = rows(path)
    assert refused(store.set_mode, HISTORIC, value, NOW) == E.MODE_INVALID
    assert refused(store.set_mode, "acme/new", value, NOW) == E.MODE_INVALID
    assert rows(path) == before


def test_the_database_itself_rejects_a_mode_or_identifier_that_bypasses_the_code(path, store):
    connection = sqlite3.connect(path)
    try:
        for repo, mode in (("acme/x", "everything"), ("Acme/X", "focus"), ("nosep", "focus")):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO repository_preferences VALUES (?, ?, 'a', 'b')", (repo, mode)
                )
    finally:
        connection.close()
    assert rows(path) == []


def test_a_new_store_selects_no_repository_at_all(store):
    assert store.list() == ()
    assert store.eligible() == ()


# --- eligibility -------------------------------------------------------------------------------


def test_eligibility_includes_focus_and_occasional_and_excludes_off_and_unselected(store):
    store.set_mode("acme/focus-one", "focus", NOW)
    store.set_mode("acme/focus-two", "focus", NOW)
    store.set_mode("acme/sometimes", "occasional", NOW)
    store.set_mode("acme/muted", "off", NOW)
    eligible = store.eligible()
    assert [(item.repo_id, item.mode) for item in eligible] == [
        ("acme/focus-one", Mode.FOCUS),
        ("acme/focus-two", Mode.FOCUS),
        ("acme/sometimes", Mode.OCCASIONAL),
    ]
    assert store.get("acme/never-selected") is None
    assert "acme/never-selected" not in {item.repo_id for item in eligible}


def test_eligibility_follows_a_change_of_mode(store):
    store.set_mode(HISTORIC, "focus", NOW)
    assert [item.repo_id for item in store.eligible()] == [HISTORIC]
    store.set_mode(HISTORIC, "off", NOW)
    assert store.eligible() == ()
    assert [item.repo_id for item in store.list()] == [HISTORIC]
    store.set_mode(HISTORIC, "occasional", NOW)
    assert [item.mode for item in store.eligible()] == [Mode.OCCASIONAL]


def test_a_historical_repository_stays_focus_with_no_network_and_no_activity_data(
    path, store, monkeypatch
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    saved = store.set_mode(HISTORIC, "focus", NOW - timedelta(days=3650))
    assert saved.mode is Mode.FOCUS
    columns = {
        row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(repository_preferences)")
    }
    assert columns == {"repo_id", "mode", "created_at", "updated_at"}  # no age or commit fields
    assert [item.repo_id for item in store.eligible()] == [HISTORIC]


# --- storage safety and isolation from the publisher ------------------------------------------


def test_a_missing_file_is_refused_when_not_creating_it(tmp_path):
    missing = tmp_path / "absent.sqlite"
    assert refused(RepositoryPreferences.open, missing, create=False) == E.STORE_UNUSABLE
    assert not missing.exists()


def test_a_foreign_or_corrupt_file_is_refused_and_left_unchanged(tmp_path):
    foreign = tmp_path / "foreign.sqlite"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE other (x)")
    connection.commit()
    connection.close()
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"not a database" * 50)
    for target in (foreign, garbage):
        before = digest(target)
        assert refused(RepositoryPreferences.open, target, create=True) == E.STORE_UNUSABLE
        assert digest(target) == before


def test_preference_changes_leave_publisher_records_untouched_and_files_are_not_shared(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    candidate = Candidate(POST_TEXT, CHANNEL, DUE, "demo_workspace", "e" * 64, "a" * 64)
    with Ledger.open(ledger_path, create=True) as ledger:
        approval = ledger.add_approval(candidate, NOW)
        ledger.begin_send(approval.approval_id, NOW)
    before = digest(ledger_path)

    prefs_path = tmp_path / "preferences.sqlite"
    with RepositoryPreferences.open(prefs_path, create=True) as store:
        for mode in ("focus", "occasional", "off", "focus"):
            store.set_mode(HISTORIC, mode, NOW)

    assert digest(ledger_path) == before
    with Ledger.open(ledger_path, create=False) as ledger:
        assert ledger.get_approval(approval.approval_id) == approval
        intent = ledger.get_intent(approval.approval_id)
        assert intent is not None and intent.state is IntentState.SENDING

    # Neither module accepts the other's file, and trying changes neither file.
    prefs_before = digest(prefs_path)
    assert refused(RepositoryPreferences.open, ledger_path, create=True) == E.STORE_UNUSABLE
    with pytest.raises(PublicationError):
        Ledger.open(prefs_path, create=False)
    assert digest(ledger_path) == before
    assert digest(prefs_path) == prefs_before


# --- first-use initialization is one atomic claim ----------------------------------------------

PREFERENCES_TABLES = {"repository_preferences"}
LEDGER_TABLES = {
    "approvals",
    "intents",
    "review_decisions",
    "publication_bindings",
    "repost_grants",
    "publication_reservations",
}  # the last since schema 2 (06E)
# store name -> (opener, typed error, refusal code, the tables it owns)
STORES = {
    "preferences": (RepositoryPreferences, PreferencesError, E.STORE_UNUSABLE, PREFERENCES_TABLES),
    "ledger": (Ledger, PublicationError, PublicationErrorCode.LEDGER_UNUSABLE, LEDGER_TABLES),
}
RACES = [("preferences", "ledger"), ("ledger", "preferences")]


@pytest.fixture
def hooks(monkeypatch):
    """Run a callback right before a connection executes the given SQL, once. No threads, no waits:
    the interleaving of two initializers is exactly the one the callback performs."""
    registry = {}
    real_connect = sqlite3.connect

    class Hooked(sqlite3.Connection):
        def execute(self, sql, *args):
            hook = registry.pop(sql, None)
            if hook is not None:
                hook()
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Hooked, **k))
    return registry


def state(path):
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        marks = (
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute("PRAGMA application_id").fetchone()[0],
        )
        return tables, marks
    finally:
        connection.close()


def mixed_file(path, application_id):
    """What the original race produced: both stores' tables in one file."""
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in (*preferences_module._SCHEMA, *ledger_module._SCHEMA):
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {application_id}")
        connection.execute("PRAGMA user_version = 1")
    finally:
        connection.close()


@pytest.mark.parametrize(("winner", "loser"), RACES, ids=["preferences_wins", "ledger_wins"])
def test_two_stores_racing_for_one_empty_file_cannot_both_claim_it(path, hooks, winner, loser):
    opened = []
    # The loser has looked at the empty file and is about to start its write transaction when the
    # winner initializes the file completely.
    hooks["BEGIN IMMEDIATE"] = lambda: opened.append(STORES[winner][0].open(path, create=True))
    loser_store, loser_error, loser_code, _ = STORES[loser]
    with pytest.raises(loser_error) as raised:
        loser_store.open(path, create=True)
    assert raised.value.code == loser_code
    assert not hooks  # the interleaving really happened
    opened[0].close()

    winner_store, _, _, winner_tables = STORES[winner]
    tables, _ = state(path)
    assert tables == winner_tables  # no mixed-store schema
    winner_store.open(path, create=False).close()
    for create in (False, True):
        with pytest.raises(loser_error) as again:
            loser_store.open(path, create=create)
        assert again.value.code == loser_code
    assert state(path)[0] == winner_tables


@pytest.mark.parametrize("store_name", STORES)
def test_the_write_lock_is_held_from_the_identity_check_through_the_commit(path, hooks, store_name):
    opener = STORES[store_name][0]

    def probe():
        other = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()

    hooks["COMMIT"] = probe  # after the schema and identity writes: nothing ended the transaction
    opener.open(path, create=True).close()
    assert not hooks
    assert state(path)[0] == STORES[store_name][3]


@pytest.mark.parametrize("store_name", STORES)
def test_a_failed_initialization_rolls_back_everything_and_releases_the_lock(
    path, hooks, store_name
):
    opener, error, code, _ = STORES[store_name]

    def fail():
        raise sqlite3.OperationalError("injected")

    hooks["COMMIT"] = fail
    with pytest.raises(error) as raised:
        opener.open(path, create=True)
    assert raised.value.code == code
    assert state(path) == (set(), (0, 0))  # no table, no identity marker

    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")  # the lock was released
        other.execute("ROLLBACK")
    finally:
        other.close()
    opener.open(path, create=True).close()  # still unclaimed, so a retry initializes normally


@pytest.mark.parametrize(
    "application_id",
    [0, preferences_module.APPLICATION_ID],
    ids=["ledger_marker", "preferences_marker"],
)
@pytest.mark.parametrize("store_name", STORES)
def test_neither_store_accepts_a_file_holding_both_stores_schemas(path, store_name, application_id):
    mixed_file(path, application_id)
    before = digest(path)
    opener, error, code, _ = STORES[store_name]
    for create in (False, True):
        with pytest.raises(error) as raised:
            opener.open(path, create=create)
        assert raised.value.code == code
    assert digest(path) == before


@pytest.mark.parametrize(("owner", "other"), RACES, ids=["preferences_owns", "ledger_owns"])
def test_an_initialized_store_keeps_its_records_and_refuses_the_other_store(path, owner, other):
    if owner == "preferences":
        with RepositoryPreferences.open(path, create=True) as store:
            store.set_mode(HISTORIC, "focus", NOW)
    else:
        candidate = Candidate(POST_TEXT, CHANNEL, DUE, "demo_workspace", "e" * 64, "a" * 64)
        with Ledger.open(path, create=True) as ledger:
            ledger.begin_send(ledger.add_approval(candidate, NOW).approval_id, NOW)
    before = digest(path)

    other_store, other_error, other_code, _ = STORES[other]
    for create in (False, True):
        with pytest.raises(other_error) as raised:
            other_store.open(path, create=create)
        assert raised.value.code == other_code
    assert digest(path) == before

    # Opening with create=True or False is still normal for the owner, and changes nothing.
    for create in (False, True):
        with STORES[owner][0].open(path, create=create) as store:
            if owner == "preferences":
                assert [(p.repo_id, p.mode) for p in store.list()] == [(HISTORIC, Mode.FOCUS)]
            else:
                (intent,) = store.list_intents()
                assert intent.state is IntentState.SENDING


# --- SQLite-managed internal tables are not application tables ---------------------------------


def analyze(path):
    """The real SQLite ANALYZE statement, which creates the internal `sqlite_stat1` table."""
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("ANALYZE")
        names = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "sqlite_stat1" in names
    finally:
        connection.close()


def stored_records(path, owner):
    connection = sqlite3.connect(path)
    try:
        tables = ("repository_preferences",) if owner == "preferences" else ("approvals", "intents")
        return {t: connection.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    finally:
        connection.close()


@pytest.mark.parametrize("owner", STORES)
def test_a_store_with_records_survives_analyze_and_opens_with_either_setting(path, owner):
    if owner == "preferences":
        with RepositoryPreferences.open(path, create=True) as store:
            store.set_mode(HISTORIC, "focus", NOW)
            store.set_mode("owner/other", "off", NOW)
    else:
        candidate = Candidate(POST_TEXT, CHANNEL, DUE, "demo_workspace", "e" * 64, "a" * 64)
        with Ledger.open(path, create=True) as ledger:
            ledger.begin_send(ledger.add_approval(candidate, NOW).approval_id, NOW)
    before = stored_records(path, owner)
    assert all(before.values())

    analyze(path)

    opener, _, _, owned = STORES[owner]
    for create in (False, True):
        with opener.open(path, create=create) as store:
            if owner == "preferences":
                assert [(p.repo_id, p.mode) for p in store.list()] == [
                    ("owner/other", Mode.OFF),
                    (HISTORIC, Mode.FOCUS),
                ]
            else:
                (intent,) = store.list_intents()
                assert intent.state is IntentState.SENDING
        assert stored_records(path, owner) == before
    assert state(path)[0] == owned | {"sqlite_stat1"}


@pytest.mark.parametrize("owner", STORES)
def test_analyze_does_not_make_the_other_store_accept_the_file(path, owner):
    with STORES[owner][0].open(path, create=True):
        pass
    analyze(path)
    other = "ledger" if owner == "preferences" else "preferences"
    other_store, other_error, other_code, _ = STORES[other]
    for create in (False, True):
        with pytest.raises(other_error) as raised:
            other_store.open(path, create=create)
        assert raised.value.code == other_code


@pytest.mark.parametrize(("owner", "other"), RACES, ids=["preferences_claims", "ledger_claims"])
def test_an_empty_database_with_only_analyze_statistics_is_still_unclaimed(path, owner, other):
    analyze(path)  # a pristine database, then the real ANALYZE: only `sqlite_stat1` exists
    assert state(path) == ({"sqlite_stat1"}, (0, 0))

    opener, error, code, owned = STORES[owner]
    with pytest.raises(error) as raised:
        opener.open(path, create=False)  # uninitialized, so not accepted as a store
    assert raised.value.code == code

    opener.open(path, create=True).close()
    assert state(path)[0] == owned | {"sqlite_stat1"}

    other_store, other_error, other_code, _ = STORES[other]
    for create in (False, True):
        with pytest.raises(other_error) as refused_again:
            other_store.open(path, create=create)
        assert refused_again.value.code == other_code
        opener.open(path, create=create).close()
    assert state(path)[0] == owned | {"sqlite_stat1"}


@pytest.mark.parametrize("store_name", STORES)
@pytest.mark.parametrize(
    "statement",
    ["CREATE TABLE stray (x)", "CREATE VIEW stray AS SELECT 1"],
    ids=["table", "view"],
)
def test_a_user_defined_object_in_an_unclaimed_database_is_still_refused(
    path, store_name, statement
):
    analyze(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute(statement)
    finally:
        connection.close()
    before = digest(path)
    opener, error, code, _ = STORES[store_name]
    for create in (False, True):
        with pytest.raises(error) as raised:
            opener.open(path, create=create)
        assert raised.value.code == code
    assert digest(path) == before


def schema_objects(path):
    connection = sqlite3.connect(path)
    try:
        return set(connection.execute("SELECT type, name FROM sqlite_master"))
    finally:
        connection.close()


# store name -> a table and column it owns, for attaching stray objects
OWNED_COLUMN = {"preferences": ("repository_preferences", "mode"), "ledger": ("intents", "state")}
STRAY_OBJECTS = {
    "view": "CREATE VIEW stray AS SELECT 1 AS x",
    "index": "CREATE INDEX stray ON {table} ({column})",
    "trigger": "CREATE TRIGGER stray AFTER INSERT ON {table} BEGIN SELECT 1; END",
}


@pytest.mark.parametrize("kind", STRAY_OBJECTS)
@pytest.mark.parametrize("owner", STORES)
def test_an_initialized_store_with_an_unexpected_schema_object_is_refused(path, owner, kind):
    if owner == "preferences":
        with RepositoryPreferences.open(path, create=True) as store:
            store.set_mode(HISTORIC, "focus", NOW)
    else:
        candidate = Candidate(POST_TEXT, CHANNEL, DUE, "demo_workspace", "e" * 64, "a" * 64)
        with Ledger.open(path, create=True) as ledger:
            ledger.begin_send(ledger.add_approval(candidate, NOW).approval_id, NOW)
    analyze(path)
    opener, error, code, _ = STORES[owner]
    module = preferences_module if owner == "preferences" else ledger_module
    # The store's own triggers and SQLite's own indexes (primary keys) and statistics are accepted.
    legitimate = schema_objects(path)
    own_triggers = {s.split()[2] for s in module._SCHEMA if s.startswith("CREATE TRIGGER")}
    assert own_triggers and {("trigger", t) for t in own_triggers} <= legitimate
    assert any(kind == "index" and name.startswith("sqlite_") for kind, name in legitimate)
    for create in (False, True):
        opener.open(path, create=create).close()
    records = stored_records(path, owner)

    table, column = OWNED_COLUMN[owner]
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute(STRAY_OBJECTS[kind].format(table=table, column=column))
    finally:
        connection.close()
    before = digest(path)
    for create in (False, True):
        with pytest.raises(error) as raised:
            opener.open(path, create=create)
        assert raised.value.code == code
    assert digest(path) == before
    assert stored_records(path, owner) == records


@pytest.mark.parametrize("store_name", STORES)
@pytest.mark.parametrize("application_id", [0, preferences_module.APPLICATION_ID])
def test_a_mixed_schema_is_still_refused_after_analyze(path, store_name, application_id):
    mixed_file(path, application_id)
    analyze(path)
    before = digest(path)
    opener, error, code, _ = STORES[store_name]
    for create in (False, True):
        with pytest.raises(error) as raised:
            opener.open(path, create=create)
        assert raised.value.code == code
    assert digest(path) == before
