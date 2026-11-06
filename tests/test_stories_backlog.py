"""The story backlog store and the finding checks, offline. Every database is a temporary file."""

import sqlite3
from datetime import UTC, datetime

import pytest
from stories_fake import (
    ACTIVE,
    ACTIVE_FILES,
    ACTIVE_REVISION,
    HISTORIC_ID,
    active_finding,
    historic_finding,
    make_snapshot,
    ref,
    sha256_file,
)

from shipyap.editorial.sources import load_verified_snapshot
from shipyap.preferences.errors import PreferencesError
from shipyap.preferences.repositories import RepositoryPreferences
from shipyap.publication.errors import PublicationError
from shipyap.publication.ledger import Ledger
from shipyap.stories.backlog import (
    Observation,
    RecordOutcome,
    StoryBacklog,
    StoryState,
    story_id,
)
from shipyap.stories.errors import StoryError, StoryErrorCode
from shipyap.stories.findings import (
    Refusal,
    StoryFinding,
    check_finding,
)

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


@pytest.fixture
def snapshot(tmp_path):
    return load_verified_snapshot(make_snapshot(tmp_path, "snap"))


@pytest.fixture
def backlog(tmp_path):
    with StoryBacklog.open(tmp_path / "backlog.sqlite", create=True) as store:
        yield store


def observation(snapshot, **overrides) -> Observation:
    finding = StoryFinding.model_validate(historic_finding())
    check = check_finding(finding, snapshot)
    data = {
        "source_url": snapshot.source_url,
        "revision_sha": snapshot.revision_sha,
        "manifest_sha256": snapshot.manifest_sha256,
        "angle": finding.angle,
        "support": finding.support,
        "refs": check.refs,
    }
    data.update(overrides)
    return Observation(**data)


def record(store, obs, key="capture-is-not-scene", now=NOW):
    with store.transaction():
        return store.record(HISTORIC_ID, key, obs, now)


# --- story identity and recording ------------------------------------------------------------


def test_story_identity_depends_only_on_repository_and_key():
    assert story_id(HISTORIC_ID, "a-key") == story_id(HISTORIC_ID, "a-key")
    assert story_id(HISTORIC_ID, "a-key") != story_id(HISTORIC_ID, "b-key")
    assert story_id(HISTORIC_ID, "a-key") != story_id("acme/agent-evals", "a-key")


def test_first_record_is_new_and_available_with_pinned_evidence(backlog, snapshot):
    outcome, sid = record(backlog, observation(snapshot))
    assert outcome == RecordOutcome.NEW
    story = backlog.get(sid)
    assert story.state == StoryState.AVAILABLE and story.offered_at is None
    assert story.semantic_support == "UNKNOWN"
    assert (story.repo_id, story.story_key, story.evidence_count) == (
        HISTORIC_ID,
        "capture-is-not-scene",
        1,
    )
    assert story.latest.revision_sha == snapshot.revision_sha
    assert [(r.path, r.line_start, r.line_end, r.file_sha256) for r in story.latest.refs] == [
        ("README.md", 3, 4, snapshot.sources[0].sha256)
    ]


def test_recording_the_same_observation_again_changes_nothing(backlog, snapshot):
    _, sid = record(backlog, observation(snapshot))
    before = backlog.get(sid)
    assert record(backlog, observation(snapshot), now=LATER) == (RecordOutcome.UNCHANGED, sid)
    assert backlog.get(sid) == before


def test_a_new_revision_adds_evidence_to_the_same_story_without_resetting_it(backlog, snapshot):
    _, sid = record(backlog, observation(snapshot))
    backlog.mark_offered(sid, NOW)
    newer = observation(snapshot, revision_sha="9" * 40, manifest_sha256="f" * 64)
    assert record(backlog, newer, now=LATER) == (RecordOutcome.EVIDENCE_ADDED, sid)
    story = backlog.get(sid)
    assert len(backlog.stories()) == 1
    assert (story.state, story.offered_at, story.evidence_count) == (
        StoryState.OFFERED,
        "2026-10-23T09:00:00Z",
        2,
    )
    assert story.latest.revision_sha == "9" * 40
    assert story.first_seen_at == "2026-10-23T09:00:00Z"


def test_the_same_angle_under_another_key_is_a_duplicate_and_not_recorded(backlog, snapshot):
    record(backlog, observation(snapshot))
    spaced = observation(
        snapshot, angle="A  screenshot proves CAPTURE, not that the right scene was shown."
    )
    outcome, _ = record(backlog, spaced, key="another-key")
    assert outcome == RecordOutcome.DUPLICATE_ANGLE
    assert len(backlog.stories()) == 1


def test_a_different_angle_on_the_same_project_is_a_separate_story(backlog, snapshot):
    record(backlog, observation(snapshot))
    other = observation(
        snapshot, angle="One synthetic configuration is too narrow to generalize from."
    )
    outcome, sid = record(backlog, other, key="one-configuration-only")
    assert outcome == RecordOutcome.NEW
    assert len(backlog.stories()) == 2 and backlog.get(sid).story_key == "one-configuration-only"


def test_a_failed_transaction_records_nothing(backlog, snapshot):
    with pytest.raises(RuntimeError), backlog.transaction():
        backlog.record(HISTORIC_ID, "capture-is-not-scene", observation(snapshot), NOW)
        raise RuntimeError("interrupted")
    assert backlog.stories() == ()


# --- offering --------------------------------------------------------------------------------


def test_offering_is_recorded_once_and_is_idempotent(backlog, snapshot):
    _, sid = record(backlog, observation(snapshot))
    first = backlog.mark_offered(sid, NOW)
    again = backlog.mark_offered(sid, LATER)
    assert first.state == again.state == StoryState.OFFERED
    assert again.offered_at == "2026-10-23T09:00:00Z"


def test_offering_an_unknown_story_is_refused(backlog):
    with pytest.raises(StoryError) as caught:
        backlog.mark_offered("0" * 64, NOW)
    assert caught.value.code == StoryErrorCode.STORY_UNKNOWN


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM stories",
        "DELETE FROM story_evidence",
        "UPDATE story_evidence SET angle = 'changed angle text here'",
        "UPDATE stories SET story_key = 'renamed-key'",
        "UPDATE stories SET state = 'available', offered_at = NULL",
    ],
)
def test_history_cannot_be_deleted_edited_or_reset(backlog, snapshot, statement):
    _, sid = record(backlog, observation(snapshot))
    backlog.mark_offered(sid, NOW)
    before = backlog.stories()
    with pytest.raises(sqlite3.DatabaseError):
        backlog._db.execute(statement)
    assert backlog.stories() == before


# --- persistence and file identity -----------------------------------------------------------


def test_the_backlog_survives_reopening(tmp_path, snapshot):
    path = tmp_path / "backlog.sqlite"
    with StoryBacklog.open(path, create=True) as store:
        _, sid = record(store, observation(snapshot))
    with StoryBacklog.open(path, create=False) as store:
        assert [s.story_id for s in store.stories()] == [sid]


def test_a_missing_backlog_is_refused_without_creating_it(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(StoryError) as caught:
        StoryBacklog.open(path, create=False)
    assert caught.value.code == StoryErrorCode.BACKLOG_UNUSABLE
    assert not path.exists()


def test_backlog_preferences_and_ledger_files_refuse_each_other(tmp_path):
    backlog_path = tmp_path / "backlog.sqlite"
    StoryBacklog.open(backlog_path, create=True).close()
    preferences_path = tmp_path / "preferences.sqlite"
    RepositoryPreferences.open(preferences_path, create=True).close()
    ledger_path = tmp_path / "ledger.sqlite"
    Ledger.open(ledger_path, create=True).close()
    digests = {p: sha256_file(p) for p in (backlog_path, preferences_path, ledger_path)}

    for other in (preferences_path, ledger_path):
        with pytest.raises(StoryError):
            StoryBacklog.open(other, create=True)
    with pytest.raises(PreferencesError):
        RepositoryPreferences.open(backlog_path, create=True)
    with pytest.raises(PublicationError):
        Ledger.open(backlog_path, create=True)
    assert {p: sha256_file(p) for p in digests} == digests


# --- finding checks --------------------------------------------------------------------------


def check(snapshot, **overrides):
    return check_finding(StoryFinding.model_validate(historic_finding(**overrides)), snapshot)


def test_a_located_finding_passes_with_pinned_references(snapshot):
    result = check(snapshot)
    assert result.refusal is None
    assert [r.label() for r in result.refs] == ["README.md:3-4"]


@pytest.mark.parametrize(
    ("refs", "refusal", "detail"),
    [
        ([ref("MISSING.md", 1, 1)], Refusal.REF_PATH_NOT_IN_SNAPSHOT, "MISSING.md:1-1"),
        ([ref("README.md", 5, 6)], Refusal.REF_RANGE_INVALID, "README.md:5-6"),
        ([ref("README.md", 3, 4, "proves the right scene")], Refusal.REF_QUOTE_NOT_FOUND, None),
    ],
)
def test_unlocated_references_are_refused(snapshot, refs, refusal, detail):
    result = check(snapshot, source_refs=refs)
    assert result.refusal == refusal and result.refs == ()
    assert detail is None or result.detail == detail


@pytest.mark.parametrize(
    "support",
    [
        "The README reports the scene confirmed for 7/10 of the artifacts.",  # invented measure
        "The README reports 3/10 confirmed, a 30% rate across artifacts.",  # derived figure
    ],
)
def test_a_number_absent_from_the_cited_lines_is_refused(snapshot, support):
    assert check(snapshot, support=support).refusal == Refusal.NUMBER_NOT_IN_EVIDENCE


def test_numbers_must_come_from_the_cited_lines_not_elsewhere_in_the_file(snapshot):
    # 3/10 is on line 4; citing line 3 alone does not support it.
    result = check(snapshot, source_refs=[ref("README.md", 3, 3)])
    assert result.refusal == Refusal.NUMBER_NOT_IN_EVIDENCE


def test_a_number_inside_a_longer_source_number_is_not_evidence(tmp_path):
    snap = load_verified_snapshot(
        make_snapshot(tmp_path, "active", ACTIVE, ACTIVE_REVISION, ACTIVE_FILES)
    )
    finding = active_finding(
        angle="Every run in these notes used model 45 for consistency.",
        source_refs=[ref("NOTES.md", 4, 4)],
        support="The notes name the single model used for every run in the evaluation.",
    )
    result = check_finding(StoryFinding.model_validate(finding), snap)
    assert result.refusal == Refusal.NUMBER_NOT_IN_EVIDENCE
