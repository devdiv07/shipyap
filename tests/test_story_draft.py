"""`drafting/story_draft.py` at the library level: the failure path `pros run` relies on when a
draft bundle exists but linking or the offer fails.

Offline: `FakeDrafter` stands in for the provider. All content is synthetic.
"""

from datetime import UTC, datetime

import pytest
from drafting_fake import FakeDrafter, model_text, reply
from editorial_fake import seg
from stories_fake import HISTORIC, findings, historic_finding, make_snapshot, ref, write_json

from shipyap.drafting.story_draft import (
    OutputState,
    StoryDraftFailed,
    plan_story_draft,
    recover_story_draft,
    run_story_draft,
)
from shipyap.preferences.repositories import RepositoryPreferences
from shipyap.stories.backlog import StoryBacklog, StoryState
from shipyap.stories.discovery import run_discovery
from shipyap.stories.errors import StoryError, StoryErrorCode

NOW = datetime(2026, 10, 27, tzinfo=UTC)
SEGMENTS = (
    seg(
        "S01",
        "A screenshot proves capture, not that the right scene was shown.",
        "SOURCE",
        src=[ref("README.md", 3, 3, "proves capture, not that the right scene was shown")],
    ),
    seg(
        "S02",
        "Who proposed this angle is not recorded, and it has not been verified.",
        "AUTHOR",
        auth=["limitations.1"],
        brk=True,
    ),
)


def test_a_failed_offer_raises_story_draft_failed_and_recovery_needs_no_request(
    tmp_path, monkeypatch
):
    preferences, backlog, drafts = (
        tmp_path / "preferences.sqlite",
        tmp_path / "backlog.sqlite",
        tmp_path / "drafts",
    )
    drafts.mkdir()
    snapshot = make_snapshot(tmp_path, "historic")
    with RepositoryPreferences.open(preferences, create=True) as store:
        store.set_mode(HISTORIC, "focus", NOW)
    found = write_json(tmp_path / "findings.json", findings(snapshot, [historic_finding()]))
    run_discovery(preferences, backlog, [snapshot], [found], NOW)

    def plan():
        return plan_story_draft(
            preferences, backlog, [snapshot], drafts, model="claude-opus-5-5", budget="5.00"
        )

    def broken(self, sid, now):
        raise StoryError(StoryErrorCode.BACKLOG_WRITE_FAILED)

    model = FakeDrafter(reply(model_text(SEGMENTS)))
    first = plan()
    assert first is not None and first.state == OutputState.FRESH
    with monkeypatch.context() as patch:
        patch.setattr(StoryBacklog, "mark_offered", broken)
        with pytest.raises(StoryDraftFailed) as failed:
            run_story_draft(first, model, backlog, NOW)
    assert isinstance(failed.value.cause, StoryError)
    assert failed.value.cause.code == StoryErrorCode.BACKLOG_WRITE_FAILED
    assert failed.value.model_run is not None and len(model.generate_calls) == 1
    with StoryBacklog.open(backlog, create=False) as store:
        (story,) = store.stories()
    assert story.state == StoryState.AVAILABLE

    again = plan()
    assert again is not None and again.state not in (OutputState.FRESH, OutputState.BRIEF_READY)
    recovered = recover_story_draft(again, backlog, NOW)
    assert recovered.model_run is None and len(model.generate_calls) == 1
    with StoryBacklog.open(backlog, create=False) as store:
        (story,) = store.stories()
    assert story.state == StoryState.OFFERED
