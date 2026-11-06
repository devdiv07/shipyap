"""Owner approval: what is eligible to be shown, what an approval binds, and what is refused."""

import json
from datetime import timedelta

import pytest
from publication_fake import (
    CHANNEL,
    DUE,
    NOW,
    OTHER_CHANNEL,
    POST_TEXT,
    blocking_segments,
    make_bundle,
)

from shipyap.editorial.models import DraftStatus
from shipyap.publication.approval import (
    approval_id,
    approval_sheet,
    confirmation_phrase,
    parse_channel_id,
    parse_due_at,
    provider_time,
)
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.ledger import Ledger
from shipyap.publication.service import approve, load_candidate

E = PublicationErrorCode


def refused(call, *args, **kwargs) -> PublicationErrorCode:
    with pytest.raises(PublicationError) as raised:
        call(*args, **kwargs)
    return raised.value.code


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path)


@pytest.fixture
def ledger(tmp_path):
    with Ledger.open(tmp_path / "ledger.sqlite", create=True) as opened:
        yield opened


def test_a_ready_bundle_becomes_the_exact_candidate(bundle):
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    assert candidate.text == POST_TEXT  # draft.txt minus its single terminating newline
    assert (bundle / "draft.txt").read_bytes() == POST_TEXT.encode("utf-8") + b"\n"
    assert (candidate.channel_id, candidate.due_at) == (CHANNEL, DUE)


def test_exact_approval_is_recorded_and_binds_text_channel_and_time(bundle, ledger):
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    typed = confirmation_phrase(candidate.approval_id)
    approval = approve(ledger, candidate, typed, NOW)
    assert approval.approval_id == approval_id(POST_TEXT, CHANNEL, DUE)
    stored = ledger.get_approval(approval.approval_id)
    assert (stored.text, stored.channel_id, stored.due_at) == (POST_TEXT, CHANNEL, DUE)
    # Approving the identical post again is the same approval, not a second one.
    assert approve(ledger, candidate, typed, NOW + timedelta(minutes=1)) == stored


@pytest.mark.parametrize("typed", ["", "y", "yes", "APPROVE", "approve 000000000000", " "])
def test_anything_but_the_exact_phrase_is_not_approval(bundle, ledger, typed):
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    assert refused(approve, ledger, candidate, typed, NOW) == E.APPROVAL_DECLINED
    assert ledger.get_approval(candidate.approval_id) is None


def test_the_phrase_for_a_different_post_does_not_approve_this_one(bundle, ledger):
    first = load_candidate(bundle, CHANNEL, DUE, NOW)
    other_time = load_candidate(bundle, CHANNEL, "2026-11-03T09:30:00Z", NOW)
    assert confirmation_phrase(first.approval_id) != confirmation_phrase(other_time.approval_id)
    typed = confirmation_phrase(first.approval_id)
    assert refused(approve, ledger, other_time, typed, NOW) == E.APPROVAL_DECLINED
    assert refused(approve, ledger, first, typed + " ", NOW) == E.APPROVAL_DECLINED


def test_changing_text_channel_or_time_is_a_different_unapproved_post():
    base = approval_id("text", CHANNEL, DUE)
    assert approval_id("text ", CHANNEL, DUE) != base
    assert approval_id("text", OTHER_CHANNEL, DUE) != base
    assert approval_id("text", CHANNEL, "2026-11-02T09:30:01Z") != base
    assert approval_id("text", CHANNEL, DUE) == base


def test_ready_for_review_alone_is_not_approval(bundle, ledger):
    """The bundle says READY_FOR_REVIEW and UNAPPROVED; nothing is recorded until the owner acts."""
    load_candidate(bundle, CHANNEL, DUE, NOW)
    assert json.loads((bundle / "bundle-record.json").read_text())["review_state"] == "UNAPPROVED"
    assert ledger.list_intents() == []
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    assert ledger.get_approval(candidate.approval_id) is None


def test_a_draft_with_a_blocking_finding_is_refused(tmp_path):
    blocked = make_bundle(tmp_path, "blocked", blocking_segments())
    record = json.loads((blocked / "bundle-record.json").read_text())
    assert record["draft_status"] == DraftStatus.NEEDS_REVISION
    assert refused(load_candidate, blocked, CHANNEL, DUE, NOW) == E.BUNDLE_NOT_READY


def test_a_modified_bundle_is_refused(bundle):
    (bundle / "draft.txt").write_bytes(POST_TEXT.replace("12/12", "13/13").encode() + b"\n")
    assert refused(load_candidate, bundle, CHANNEL, DUE, NOW) == E.BUNDLE_INCOMPLETE


def test_a_directory_that_is_not_a_draft_bundle_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    assert refused(load_candidate, tmp_path / "empty", CHANNEL, DUE, NOW) == E.BUNDLE_INCOMPLETE
    assert refused(load_candidate, tmp_path / "missing", CHANNEL, DUE, NOW) == E.BUNDLE_INCOMPLETE


@pytest.mark.parametrize(
    "channel", ["", "A1" * 12, "a1" * 11, "a1" * 13, "g1" * 12, " " + "a1" * 12]
)
def test_an_invalid_channel_id_is_refused(bundle, channel):
    assert refused(load_candidate, bundle, channel, DUE, NOW) == E.CHANNEL_INVALID
    assert refused(parse_channel_id, channel) == E.CHANNEL_INVALID


@pytest.mark.parametrize(
    "due",
    [
        "",
        "2026-11-02",
        "2026-11-02T09:30:00",  # no zone
        "2026-11-02T09:30:00+00:00",
        "2026-11-02T10:30:00+01:00",
        "2026-11-02T09:30:00.000Z",
        "2026-13-02T09:30:00Z",
        "2026-02-30T09:30:00Z",
        "tomorrow",
        "2026-11-02t09:30:00z",
    ],
)
def test_the_time_must_be_an_explicit_utc_second_precision_timestamp(bundle, due):
    assert refused(load_candidate, bundle, CHANNEL, due, NOW) == E.SCHEDULE_INVALID


def test_the_time_must_be_in_the_future_with_a_minimum_lead():
    assert parse_due_at("2026-11-01T12:05:00Z", NOW) == "2026-11-01T12:05:00Z"
    assert refused(parse_due_at, "2026-11-01T12:04:59Z", NOW) == E.SCHEDULE_NOT_FUTURE
    assert refused(parse_due_at, "2026-11-01T12:00:00Z", NOW) == E.SCHEDULE_NOT_FUTURE
    assert refused(parse_due_at, "2025-01-01T00:00:00Z", NOW) == E.SCHEDULE_NOT_FUTURE


def test_approval_is_refused_if_the_time_passed_while_the_owner_deliberated(bundle, ledger):
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    late = NOW + timedelta(days=2)
    typed = confirmation_phrase(candidate.approval_id)
    assert refused(approve, ledger, candidate, typed, late) == E.SCHEDULE_NOT_FUTURE
    assert ledger.get_approval(candidate.approval_id) is None


def test_the_sheet_shows_the_exact_text_channel_and_time(bundle):
    candidate = load_candidate(bundle, CHANNEL, DUE, NOW)
    sheet = approval_sheet(candidate)
    assert POST_TEXT in sheet
    assert CHANNEL in sheet and DUE in sheet and "customScheduled" in sheet
    assert "NOT been done" in sheet  # no semantic check is claimed


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-02T09:30:00.000Z", "2026-11-02T09:30:00Z"),
        ("2026-11-02T09:30:00Z", "2026-11-02T09:30:00Z"),
        ("2026-11-02T10:30:00+01:00", "2026-11-02T09:30:00Z"),
        ("2026-11-02T09:30:00.500Z", None),
        ("2026-11-02T09:30:00", None),
        (None, None),
        (12, None),
        ("garbage", None),
    ],
)
def test_provider_times_are_compared_as_canonical_utc_seconds(value, expected):
    assert provider_time(value) == expected
