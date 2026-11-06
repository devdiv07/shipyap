"""Approve -> schedule -> reconcile through the real service, ledger and Buffer adapter, over a
scripted transport. The assertions that matter are about what was SENT (counted request bodies)
and what the ledger is allowed to claim."""

from datetime import timedelta

import pytest
from publication_fake import (
    CHANNEL,
    DUE,
    NOW,
    ORG,
    OTHER_CHANNEL,
    POST_ID,
    POST_TEXT,
    FakeTransport,
    channel_reply,
    create_ok,
    listing_reply,
    make_bundle,
    node,
    post_reply,
    reply,
    timeout,
)

from shipyap.publication.approval import confirmation_phrase
from shipyap.publication.buffer import BufferPublisher, TransportReply
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.ledger import IntentState, Ledger
from shipyap.publication.service import (
    INTERRUPTED_AFTER,
    approve,
    describe,
    load_candidate,
    reconcile,
    submit,
)

E = PublicationErrorCode
S = IntentState


def refused(call, *args, **kwargs) -> PublicationErrorCode:
    with pytest.raises(PublicationError) as raised:
        call(*args, **kwargs)
    return raised.value.code


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(tmp_path)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.sqlite"


@pytest.fixture
def ledger(path):
    with Ledger.open(path, create=True) as opened:
        yield opened


@pytest.fixture
def candidate(bundle):
    return load_candidate(bundle, CHANNEL, DUE, NOW)


@pytest.fixture
def approval_id(ledger, candidate):
    return approve(ledger, candidate, confirmation_phrase(candidate.approval_id), NOW).approval_id


def provider(*script):
    transport = FakeTransport(*script)
    return BufferPublisher(transport), transport


def creates(transport) -> int:
    return transport.operations().count("mutation")


# --- the happy path, and what it does not claim ------------------------------------------------


def test_exact_approval_leads_to_one_create_and_an_accepted_unconfirmed_intent(
    ledger, candidate, approval_id
):
    buffer_, transport = provider(channel_reply(), create_ok())
    result = submit(ledger, buffer_, candidate, approval_id, NOW)
    assert transport.operations() == ["query", "mutation"]  # destination read, then one create
    assert (result.intent.state, result.intent.provider_post_id) == (S.ACCEPTED, POST_ID)
    assert "NOT confirmed" in describe(result.intent.state)
    assert "scheduled" not in describe(S.ACCEPTED).replace("NOT confirmed as scheduled", "")


def test_the_intent_is_committed_before_the_request_is_made(path, ledger, candidate, approval_id):
    seen = []

    def inspect(_body):
        seen.append(Ledger.open(path, create=False).get_intent(approval_id).state)
        return create_ok()

    buffer_, _ = provider(channel_reply(), inspect)
    submit(ledger, buffer_, candidate, approval_id, NOW)
    assert seen == [S.SENDING]  # visible to another connection while the request is in flight


# --- refusals before anything is sent ----------------------------------------------------------


def test_an_unapproved_post_is_refused_and_nothing_is_sent(ledger, candidate):
    buffer_, transport = provider()
    code = refused(submit, ledger, buffer_, candidate, candidate.approval_id, NOW)
    assert code == E.APPROVAL_NOT_FOUND
    assert transport.bodies == [] and ledger.list_intents() == []


def test_approval_of_a_different_post_does_not_cover_this_one(tmp_path, ledger, approval_id):
    """A bundle the owner never approved cannot be sent under an existing approval id."""
    other = make_bundle(tmp_path, "other", _other_segments())
    changed = load_candidate(other, CHANNEL, DUE, NOW)
    buffer_, transport = provider()
    assert refused(submit, ledger, buffer_, changed, approval_id, NOW) == E.APPROVAL_MISMATCH
    assert transport.bodies == [] and ledger.list_intents() == []


def _other_segments():
    from editorial_fake import CLEAN_SEGMENTS, seg

    return (
        *CLEAN_SEGMENTS[:-1],
        seg("S05", "The material describes one configuration.", "FRAMING"),
    )


def test_text_changed_on_disk_after_approval_is_refused(bundle, ledger, approval_id):
    (bundle / "draft.txt").write_bytes((POST_TEXT + " Extra.").encode() + b"\n")
    buffer_, transport = provider()
    # The edit also breaks the bundle's own digests, so it cannot even become a candidate.
    assert refused(load_candidate, bundle, CHANNEL, DUE, NOW) == E.BUNDLE_INCOMPLETE
    assert transport.bodies == [] and ledger.list_intents() == []


@pytest.mark.parametrize(
    ("channel", "due"),
    [
        (OTHER_CHANNEL, DUE),
        (CHANNEL, "2026-11-02T09:31:00Z"),
        (OTHER_CHANNEL, "2026-11-03T09:30:00Z"),
    ],
)
def test_channel_or_time_changed_after_approval_is_refused(
    bundle, ledger, candidate, approval_id, channel, due
):
    changed = load_candidate(bundle, channel, due, NOW)
    buffer_, transport = provider()
    assert refused(submit, ledger, buffer_, changed, approval_id, NOW) == E.APPROVAL_MISMATCH
    assert transport.bodies == [] and ledger.list_intents() == []


def test_a_forged_candidate_with_the_right_id_but_other_text_is_refused(
    ledger, candidate, approval_id
):
    from dataclasses import replace

    forged = replace(candidate, text=candidate.text + "!")
    buffer_, transport = provider()
    assert refused(submit, ledger, buffer_, forged, approval_id, NOW) == E.APPROVAL_MISMATCH
    assert transport.bodies == []


def test_a_time_that_has_arrived_by_send_time_is_refused(ledger, candidate, approval_id):
    buffer_, transport = provider()
    late = NOW + timedelta(days=1, hours=21)  # the approved time has already passed
    assert refused(submit, ledger, buffer_, candidate, approval_id, late) == E.SCHEDULE_NOT_FUTURE
    assert transport.bodies == [] and ledger.list_intents() == []


@pytest.mark.parametrize(
    ("reply_", "code"),
    [
        (channel_reply(channel=OTHER_CHANNEL), E.DESTINATION_MISMATCH),
        (channel_reply(service="instagram"), E.DESTINATION_MISMATCH),
        (channel_reply(disconnected=True), E.DESTINATION_UNHEALTHY),
        (TransportReply(500, b""), E.PROVIDER_UNAVAILABLE),
    ],
)
def test_a_bad_destination_is_refused_before_an_intent_exists(
    ledger, candidate, approval_id, reply_, code
):
    buffer_, transport = provider(reply_)
    assert refused(submit, ledger, buffer_, candidate, approval_id, NOW) == code
    assert creates(transport) == 0 and ledger.list_intents() == []


def test_a_duplicate_submission_is_refused_without_any_request(ledger, candidate, approval_id):
    buffer_, transport = provider(channel_reply(), create_ok())
    submit(ledger, buffer_, candidate, approval_id, NOW)
    again, second = provider()
    assert refused(submit, ledger, again, candidate, approval_id, NOW) == E.DUPLICATE_SUBMISSION
    assert second.bodies == []
    assert creates(transport) == 1 and len(ledger.list_intents()) == 1


# --- failure handling --------------------------------------------------------------------------


def test_a_timeout_makes_one_attempt_and_records_ambiguous(ledger, candidate, approval_id):
    buffer_, transport = provider(channel_reply(), timeout())
    result = submit(ledger, buffer_, candidate, approval_id, NOW)
    assert (result.intent.state, result.code) == (S.AMBIGUOUS, "TIMEOUT")
    assert creates(transport) == 1 and transport.script == []
    # ...and it can never be sent again, by this approval or by a repeated command.
    again, second = provider()
    assert refused(submit, ledger, again, candidate, approval_id, NOW) == E.DUPLICATE_SUBMISSION
    assert second.bodies == []


@pytest.mark.parametrize(
    ("script", "state", "code"),
    [
        (
            reply({"data": {"createPost": {"__typename": "InvalidInputError"}}}),
            S.REJECTED,
            "InvalidInputError",
        ),
        (TransportReply(429, b"{}"), S.REJECTED, "HTTP_429_RATE_LIMITED"),
        (TransportReply(503, b""), S.AMBIGUOUS, "HTTP_STATUS"),
        (TransportReply(200, b"garbage"), S.AMBIGUOUS, "UNPARSEABLE_RESPONSE"),
    ],
)
def test_other_outcomes_are_recorded_and_still_never_retried(
    ledger, candidate, approval_id, script, state, code
):
    buffer_, transport = provider(channel_reply(), script)
    result = submit(ledger, buffer_, candidate, approval_id, NOW)
    assert (result.intent.state, result.code) == (state, code)
    assert creates(transport) == 1


class Crash(BaseException):
    """Stands in for the process dying (power loss, kill) in the middle of the request."""


def test_a_crash_after_the_commit_leaves_a_sending_intent_that_is_never_resent(
    path, ledger, candidate, approval_id
):
    buffer_, _ = provider(channel_reply(), Crash())
    with pytest.raises(Crash):
        submit(ledger, buffer_, candidate, approval_id, NOW)

    restarted = Ledger.open(path, create=False)  # a new process, a new connection
    assert restarted.get_intent(approval_id).state == S.SENDING
    retry, transport = provider()
    assert refused(submit, restarted, retry, candidate, approval_id, NOW) == E.DUPLICATE_SUBMISSION
    assert transport.bodies == []


def test_reconciling_an_interrupted_intent_marks_it_ambiguous_and_never_sends(
    path, ledger, candidate, approval_id
):
    buffer_, _ = provider(channel_reply(), Crash())
    with pytest.raises(Crash):
        submit(ledger, buffer_, candidate, approval_id, NOW)

    soon = NOW + timedelta(seconds=30)
    reader, transport = provider()
    (item,) = reconcile(ledger, reader, soon)
    assert (item.after, item.note) == (S.SENDING, "IN_FLIGHT")  # may still be running

    later = NOW + INTERRUPTED_AFTER + timedelta(seconds=1)
    reader, transport = provider(listing_reply())
    (item,) = reconcile(ledger, reader, later, organization_id=ORG)
    intent = ledger.get_intent(approval_id)
    assert (intent.state, intent.detail_code) == (S.AMBIGUOUS, "INTERRUPTED")
    assert creates(transport) == 0
    assert item.note == "NOT_FOUND"


def test_a_failed_outcome_write_leaves_the_intent_sending(
    ledger, candidate, approval_id, monkeypatch
):
    buffer_, transport = provider(channel_reply(), create_ok())

    def broken(*_args, **_kwargs):
        raise PublicationError(E.LEDGER_WRITE_FAILED)

    monkeypatch.setattr(ledger, "transition", broken)
    assert refused(submit, ledger, buffer_, candidate, approval_id, NOW) == E.LEDGER_WRITE_FAILED
    assert ledger.get_intent(approval_id).state == S.SENDING
    assert creates(transport) == 1


# --- reconciliation: only provider read-back confirms anything ---------------------------------


def accepted(ledger, candidate, approval_id):
    buffer_, _ = provider(channel_reply(), create_ok())
    submit(ledger, buffer_, candidate, approval_id, NOW)


def run_reconcile(ledger, *script, **kwargs):
    reader, transport = provider(*script)
    results = reconcile(ledger, reader, NOW + timedelta(minutes=1), **kwargs)
    assert creates(transport) == 0  # reconciliation never sends
    assert all(op == "query" for op in transport.operations())
    return results


def test_accepted_but_unconfirmed_is_not_reported_as_scheduled(ledger, candidate, approval_id):
    accepted(ledger, candidate, approval_id)
    intent = ledger.get_intent(approval_id)
    assert intent.state == S.ACCEPTED
    assert "scheduled" not in intent.state.lower()
    # A provider read that fails leaves it exactly as it was.
    (item,) = run_reconcile(ledger, TransportReply(500, b""))
    assert (item.after, item.note) == (S.ACCEPTED, "PROVIDER_UNAVAILABLE")
    assert ledger.get_intent(approval_id).state == S.ACCEPTED


def test_a_verified_scheduled_post_is_confirmed_but_not_called_published(
    ledger, candidate, approval_id
):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, post_reply())
    assert (item.after, item.note) == (S.SCHEDULED, "PROVIDER_REPORTS_SCHEDULED")
    assert "NOT published" in describe(S.SCHEDULED)


@pytest.mark.parametrize("status", ["needs_approval", "draft"])
def test_needs_approval_and_draft_are_neither_scheduled_nor_published(
    ledger, candidate, approval_id, status
):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, post_reply(status=status))
    assert item.after == S.NOT_SCHEDULED
    assert ledger.get_intent(approval_id).state == S.NOT_SCHEDULED
    assert "published" not in describe(S.NOT_SCHEDULED).replace("NOT publish", "")


def test_a_sent_post_is_confirmed_published_from_provider_evidence(ledger, candidate, approval_id):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, post_reply(status="sent"))
    assert (item.after, item.note) == (S.SENT, "PROVIDER_REPORTS_SENT")
    assert run_reconcile(ledger) == []  # final: no longer examined


def test_an_error_post_is_recorded_without_resending(ledger, candidate, approval_id):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, post_reply(status="error"))
    assert item.after == S.PROVIDER_ERROR
    assert run_reconcile(ledger) == []


def test_scheduled_then_sent_progresses_on_a_later_read(ledger, candidate, approval_id):
    accepted(ledger, candidate, approval_id)
    run_reconcile(ledger, post_reply())
    (item,) = run_reconcile(ledger, post_reply(status="sent"))
    assert (item.before, item.after) == (S.SCHEDULED, S.SENT)


@pytest.mark.parametrize(
    "fields",
    [
        {"text": POST_TEXT + " "},  # the provider copy differs by one character
        {"text": POST_TEXT.replace("12/12", "13/13")},
        {"channel": OTHER_CHANNEL},
        {"due": "2026-11-02T09:31:00.000Z"},
        {"due": None},
        {"mode": "shareNow"},
        {"mode": "addToQueue"},
    ],
)
def test_a_provider_copy_that_differs_is_a_mismatch_never_a_confirmation(
    ledger, candidate, approval_id, fields
):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, post_reply(**fields))
    assert item.after == S.MISMATCH
    assert ledger.get_intent(approval_id).state == S.MISMATCH


def test_an_unrecognised_provider_status_confirms_nothing(ledger, candidate, approval_id):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, post_reply(status="sending"))
    assert (item.after, item.note) == (S.ACCEPTED, "UNRECOGNISED_PROVIDER_STATUS")


def test_a_missing_post_stays_accepted(ledger, candidate, approval_id):
    accepted(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, reply({"data": {"post": None}}))
    assert (item.after, item.note) == (S.ACCEPTED, "POST_NOT_FOUND")


def ambiguous(ledger, candidate, approval_id):
    buffer_, _ = provider(channel_reply(), timeout())
    submit(ledger, buffer_, candidate, approval_id, NOW)


def test_an_ambiguous_submission_is_found_by_exact_match_and_never_resent(
    ledger, candidate, approval_id
):
    ambiguous(ledger, candidate, approval_id)
    other = node(post_id="f0" * 12, text="someone else's post")
    (item,) = run_reconcile(ledger, listing_reply(other, node()), organization_id=ORG)
    intent = ledger.get_intent(approval_id)
    assert (item.after, intent.provider_post_id) == (S.SCHEDULED, POST_ID)


def test_an_ambiguous_submission_that_is_not_listed_stays_ambiguous(ledger, candidate, approval_id):
    ambiguous(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger, listing_reply(), organization_id=ORG)
    assert (item.after, item.note) == (S.AMBIGUOUS, "NOT_FOUND")
    pages = [listing_reply(node(post_id="f0" * 12, text="x"), more=True)] * 4
    (item,) = run_reconcile(ledger, *pages, organization_id=ORG)
    assert (item.after, item.note) == (S.AMBIGUOUS, "NOT_FOUND_LISTING_INCOMPLETE")


def test_two_matching_posts_leave_the_submission_ambiguous(ledger, candidate, approval_id):
    ambiguous(ledger, candidate, approval_id)
    twin = node(post_id="f0" * 12)
    (item,) = run_reconcile(ledger, listing_reply(node(), twin), organization_id=ORG)
    assert (item.after, item.note) == (S.AMBIGUOUS, "MULTIPLE_MATCHES")
    assert ledger.get_intent(approval_id).provider_post_id is None


def test_an_incomplete_paginated_listing_never_confirms_a_visible_match(
    ledger, candidate, approval_id
):
    ambiguous(ledger, candidate, approval_id)
    # One match is visible; an identical twin sits on a page the retrieval cap never reads.
    unread = [listing_reply(node(post_id="f0" * 12, text="x"), more=True)] * 3
    pages = [listing_reply(node(), more=True), *unread]
    (item,) = run_reconcile(ledger, *pages, organization_id=ORG)
    intent = ledger.get_intent(approval_id)
    assert (item.after, item.note) == (S.AMBIGUOUS, "LISTING_INCOMPLETE")
    assert (intent.state, intent.provider_post_id) == (S.AMBIGUOUS, None)


@pytest.mark.parametrize("visible", [1, 2])
def test_a_listing_with_a_discarded_entry_never_confirms_a_match(
    ledger, candidate, approval_id, visible
):
    ambiguous(ledger, candidate, approval_id)
    twins = [node(post_id=f"{i:02x}" * 12) for i in range(visible)]
    (item,) = run_reconcile(ledger, listing_reply({"id": "bad"}, *twins), organization_id=ORG)
    intent = ledger.get_intent(approval_id)
    assert item.after == S.AMBIGUOUS
    assert item.note == ("LISTING_INCOMPLETE" if visible == 1 else "MULTIPLE_MATCHES")
    assert (intent.state, intent.provider_post_id) == (S.AMBIGUOUS, None)


def test_a_complete_listing_with_one_valid_match_is_adopted_even_with_other_posts(
    ledger, candidate, approval_id
):
    ambiguous(ledger, candidate, approval_id)
    other = node(post_id="f0" * 12, text="someone else's post")
    (item,) = run_reconcile(ledger, listing_reply(other, node()), organization_id=ORG)
    intent = ledger.get_intent(approval_id)
    assert (item.after, item.note) == (S.SCHEDULED, "PROVIDER_REPORTS_SCHEDULED")
    assert intent.provider_post_id == POST_ID


def test_looking_for_an_ambiguous_post_needs_the_organization(ledger, candidate, approval_id):
    ambiguous(ledger, candidate, approval_id)
    (item,) = run_reconcile(ledger)
    assert (item.after, item.note) == (S.AMBIGUOUS, "ORGANIZATION_REQUIRED")
    assert refused(run_reconcile, ledger, organization_id="nope") == E.ORGANIZATION_INVALID


def test_rejected_intents_are_final_and_not_examined(ledger, candidate, approval_id):
    buffer_, _ = provider(channel_reply(), TransportReply(429, b"{}"))
    submit(ledger, buffer_, candidate, approval_id, NOW)
    assert run_reconcile(ledger) == []
