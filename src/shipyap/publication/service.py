"""Approve, schedule and reconcile one LinkedIn post. No environment access and no network code
here: the provider and the ledger are passed in.

Flow, and the order that matters:
1. `load_candidate` reads the reviewed draft bundle and turns it into the exact text, channel and
   UTC time. The bundle must verify as COMPLETE and be READY_FOR_REVIEW with no blocking finding.
   That is eligibility to be shown to the owner, never approval.
2. `approve` records an approval only if the owner typed the exact phrase for THIS post.
3. `submit` refuses an approval a later review decision withdrew (`review-serve`, 06E) or one that
   review gave to a different draft bundle, rebuilds the candidate from the files as they are NOW
   and compares it with the stored approval; then checks the destination; then commits the
   SENDING intent, re-running the review checks in that transaction; then makes at most one
   provider request; then records the outcome.
   An X post follows its LinkedIn post: `submit(..., lead_approval_id=...)` sends it only after
   Buffer accepted the LinkedIn post of the same reviewed draft and time, and only while that
   approval stands (re-checked inside the transaction that commits the X intent).
4. `reconcile` only reads from the provider. It never sends, and it never resends an AMBIGUOUS or
   interrupted submission.
"""

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from shipyap.editorial.bundle import BundleIncomplete, verify_bundle
from shipyap.editorial.models import AssertionMap, BundleKind, DraftStatus, Severity
from shipyap.evidence.strict_json import StrictJsonError, loads_strict
from shipyap.publication.approval import (
    Approval,
    Candidate,
    confirmation_phrase,
    parse_channel_id,
    parse_due_at,
    parse_organization_id,
)
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.ledger import (
    FINAL_STATES,
    LEAD_ACCEPTED,
    Intent,
    IntentState,
    Ledger,
)
from shipyap.publication.linkedin import check_publishable_text
from shipyap.publication.port import CreateKind, ProviderPost, SchedulingProvider
from shipyap.publication.x import check_x_text

LINKEDIN = "linkedin"
X = "twitter"  # Buffer's service name for X
_TEXT_CHECKS = {LINKEDIN: check_publishable_text, X: check_x_text}

# Longer than the transport's overall deadline, so a SENDING intent this old belongs to a process
# that is gone, not one still waiting for a reply.
INTERRUPTED_AFTER = timedelta(minutes=3)
_E = PublicationErrorCode


# --- the reviewed artifact ---------------------------------------------------------------------


def load_candidate(
    bundle_dir: Path, channel_id: str, due_at: str, now: datetime, *, story_id: str | None = None
) -> Candidate:
    """The exact post that `bundle_dir`, `channel_id` and `due_at` describe, or a refusal.

    The text is `draft.txt` without its single terminating newline (the editorial writer's file
    format: post + "\\n"). Nothing else is stripped, normalised or rewritten."""
    channel = parse_channel_id(channel_id)
    due = parse_due_at(due_at, now)
    try:
        bundle = verify_bundle(bundle_dir, BundleKind.DRAFT)
    except BundleIncomplete:
        raise PublicationError(_E.BUNDLE_INCOMPLETE) from None
    try:
        amap = AssertionMap.model_validate(loads_strict(bundle.files["assertion-map.json"]))
        raw = bundle.files["draft.txt"].decode("utf-8")
    except (ValidationError, StrictJsonError, ValueError):
        raise PublicationError(_E.BUNDLE_INCONSISTENT) from None
    blocking = any(
        finding.severity == Severity.BLOCKING
        for finding in (*amap.document_findings, *(f for s in amap.segments for f in s.findings))
    )
    if (
        blocking
        or amap.draft_status != DraftStatus.READY_FOR_REVIEW
        or bundle.record.draft_status != DraftStatus.READY_FOR_REVIEW
    ):
        raise PublicationError(_E.BUNDLE_NOT_READY)
    if not raw.endswith("\n") or raw.endswith("\n\n"):
        raise PublicationError(_E.BUNDLE_INCONSISTENT)
    text = raw[:-1]
    if (
        len(text) != amap.post_chars
        or amap.brief_sha256 != bundle.record.brief_sha256
        or amap.response_sha256 != bundle.record.response_sha256
    ):
        raise PublicationError(_E.BUNDLE_INCONSISTENT)
    check_publishable_text(text)
    return Candidate(
        text=text,
        channel_id=channel,
        due_at=due,
        workspace_id=bundle.record.workspace_id,
        bundle_record_sha256=bundle.record_sha256,
        story_id=story_id,
    )


# --- owner approval ----------------------------------------------------------------------------


def approve(ledger: Ledger, candidate: Candidate, typed: str, now: datetime) -> Approval:
    """Record the owner's approval of exactly `candidate`.

    `typed` must equal the confirmation phrase for this candidate (nothing else counts, and
    surrounding whitespace is not forgiven). The caller is responsible for obtaining `typed` from
    an interactive terminal; this function cannot tell who typed it."""
    confirm(candidate, typed, now)
    return ledger.add_approval(candidate, now)


def confirm(candidate: Candidate, typed: str, now: datetime) -> None:
    """The checks every approval passes, whichever interface obtained `typed`."""
    expected = confirmation_phrase(candidate.approval_id)
    if not hmac.compare_digest(typed.encode("utf-8"), expected.encode("utf-8")):
        raise PublicationError(_E.APPROVAL_DECLINED)
    # The time must still be in the future now, not only when the files were loaded.
    parse_due_at(candidate.due_at, now)


# --- scheduling --------------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitResult:
    intent: Intent
    code: str  # the provider outcome code, e.g. ACCEPTED, TIMEOUT, InvalidInputError


def submit(
    ledger: Ledger,
    provider: SchedulingProvider,
    candidate: Candidate,
    approval_id: str,
    now: datetime,
    *,
    service: str = LINKEDIN,
    lead_approval_id: str | None = None,
    origin: str = "service",
) -> SubmitResult:
    """Make at most one provider request for an approved post.

    `candidate` is rebuilt by the caller from the files as they are now; it must hash to the
    approval the owner gave. Every refusal below happens before the intent is committed, so a
    refused call leaves no intent and sends nothing.

    `service` is the network the channel must be (`linkedin` or `twitter`), and picks the text
    limit. An X post needs `lead_approval_id`: the LinkedIn approval of the same draft and time,
    whose intent Buffer must have accepted."""
    if service not in _TEXT_CHECKS or (service == X) != (lead_approval_id is not None):
        raise ValueError("an X post follows a LinkedIn approval; a LinkedIn post follows none")
    approval = ledger.get_approval(approval_id)
    if approval is None:
        raise PublicationError(_E.APPROVAL_NOT_FOUND)
    bound = [approval.approval_id]
    if lead_approval_id is not None:
        lead = ledger.get_approval(lead_approval_id)
        lead_intent = ledger.get_intent(lead_approval_id)
        if (
            lead is None
            or lead.approval_id == approval.approval_id
            or lead.bundle_record_sha256 != approval.bundle_record_sha256
            or lead.due_at != approval.due_at
            or lead.channel_id == approval.channel_id
        ):
            raise PublicationError(_E.APPROVAL_MISMATCH)
        if lead_intent is None or lead_intent.state not in LEAD_ACCEPTED:
            raise PublicationError(_E.LEAD_NOT_ACCEPTED)
        bound.append(lead.approval_id)
    for one in bound:
        if ledger.approval_withdrawn(one):  # edited, rejected or re-decided in review
            raise PublicationError(_E.APPROVAL_WITHDRAWN)
        reviewed = ledger.reviewed_drafts(one)  # empty for publish-approve approvals
        if reviewed and reviewed != {candidate.bundle_record_sha256}:
            raise PublicationError(_E.APPROVAL_NOT_REVIEWED_DRAFT)
    if (
        candidate.approval_id != approval.approval_id
        or candidate.text != approval.text
        or candidate.channel_id != approval.channel_id
        or candidate.due_at != approval.due_at
    ):
        raise PublicationError(_E.APPROVAL_MISMATCH)
    parse_due_at(approval.due_at, now)  # still in the future, with the minimum lead
    parse_channel_id(approval.channel_id)  # so the request below cannot be refused after commit
    _TEXT_CHECKS[service](approval.text)
    if ledger.get_intent(approval.approval_id) is not None:  # no network call for a duplicate
        raise PublicationError(_E.DUPLICATE_SUBMISSION)
    binding = ledger.publication_binding(approval_id)
    if binding is None or (candidate.story_id is not None and candidate.story_id != binding[1]):
        raise PublicationError(_E.PUBLICATION_IDENTITY_REQUIRED)
    ledger.check_publication(approval_id, lead_approval_id)
    provider.check_destination(approval.channel_id, service)

    # Committed; from here the post may be sent. The review checks above run again inside this
    # transaction, so a rejection committed during the destination check still stops the send.
    ledger.begin_send(
        approval.approval_id, now, candidate.bundle_record_sha256, lead_approval_id, origin=origin
    )
    outcome = provider.create_scheduled_post(approval.text, approval.channel_id, approval.due_at)
    new_state = {
        CreateKind.ACCEPTED: IntentState.ACCEPTED,
        CreateKind.REJECTED: IntentState.REJECTED,
        CreateKind.AMBIGUOUS: IntentState.AMBIGUOUS,
    }[outcome.kind]
    try:
        ledger.transition(
            approval.approval_id, new_state, now, post_id=outcome.post_id, code=outcome.code
        )
    except PublicationError:
        # The intent stays SENDING, which reads as potentially sent. Say so; never retry here.
        raise PublicationError(_E.LEDGER_WRITE_FAILED) from None
    intent = ledger.get_intent(approval.approval_id)
    if intent is None:
        raise PublicationError(_E.LEDGER_WRITE_FAILED)
    return SubmitResult(intent, outcome.code)


# --- read-only reconciliation ------------------------------------------------------------------


@dataclass(frozen=True)
class Reconciled:
    approval_id: str
    before: IntentState
    after: IntentState
    note: str  # a fixed code; see `reconcile`


def reconcile(
    ledger: Ledger,
    provider: SchedulingProvider,
    now: datetime,
    *,
    organization_id: str | None = None,
    approval_id: str | None = None,
) -> list[Reconciled]:
    """Read each unfinished intent back from the provider and record what the evidence shows.

    Never sends. A post is only confirmed as SCHEDULED when the provider reports `scheduled`, the
    exact approved text, the approved channel, the approved time and the `customScheduled` mode.
    Not finding an AMBIGUOUS post leaves it AMBIGUOUS: absence from a listing is not proof."""
    if organization_id is not None:
        parse_organization_id(organization_id)
    results = []
    for intent in ledger.list_intents():
        if intent.state in FINAL_STATES or (approval_id and intent.approval_id != approval_id):
            continue
        approval = ledger.get_approval(intent.approval_id)
        if approval is None:
            raise PublicationError(_E.LEDGER_UNUSABLE)
        results.append(_reconcile_one(ledger, provider, intent, approval, now, organization_id))
    return results


def _reconcile_one(
    ledger: Ledger,
    provider: SchedulingProvider,
    intent: Intent,
    approval: Approval,
    now: datetime,
    organization_id: str | None,
) -> Reconciled:
    state = intent.state
    if state == IntentState.SENDING:
        updated = datetime.strptime(intent.updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if now - updated < INTERRUPTED_AFTER:
            return Reconciled(approval.approval_id, state, state, "IN_FLIGHT")
        ledger.transition(approval.approval_id, IntentState.AMBIGUOUS, now, code="INTERRUPTED")
        state = IntentState.AMBIGUOUS
    post_id = intent.provider_post_id
    try:
        if post_id is not None:
            post = provider.read_post(post_id)
            if post is None:
                return Reconciled(approval.approval_id, state, state, "POST_NOT_FOUND")
        else:
            if organization_id is None:
                raise PublicationError(_E.ORGANIZATION_REQUIRED)
            listing = provider.list_posts(organization_id, approval.channel_id)
            matches = [p for p in listing.posts if _same_post(p, approval)]
            if len(matches) > 1:
                return Reconciled(approval.approval_id, state, state, "MULTIPLE_MATCHES")
            if not matches:
                note = "NOT_FOUND" if listing.complete else "NOT_FOUND_LISTING_INCOMPLETE"
                return Reconciled(approval.approval_id, state, state, note)
            if not listing.complete:
                # An unread or discarded entry could be a second identical post: one visible
                # match is not proof it is the only one, so nothing is adopted or recorded.
                return Reconciled(approval.approval_id, state, state, "LISTING_INCOMPLETE")
            post = matches[0]
    except PublicationError as exc:
        return Reconciled(approval.approval_id, state, state, exc.code.value)
    after, note = _judge(post, approval)
    if after is not None:
        ledger.transition(approval.approval_id, after, now, post_id=post.post_id, code=note)
        state = after
    return Reconciled(approval.approval_id, intent.state, state, note)


def _same_post(post: ProviderPost, approval: Approval) -> bool:
    return (
        post.channel_id == approval.channel_id
        and post.text == approval.text
        and post.due_at == approval.due_at
    )


def _judge(post: ProviderPost, approval: Approval) -> tuple[IntentState | None, str]:
    """(new state, note) for what the provider reports. None leaves the state unchanged."""
    if post.channel_id != approval.channel_id or post.text != approval.text:
        return IntentState.MISMATCH, "TEXT_OR_CHANNEL_DIFFERS"
    status = post.status
    if status == "sent":
        return IntentState.SENT, "PROVIDER_REPORTS_SENT"
    if status == "error":
        return IntentState.PROVIDER_ERROR, "PROVIDER_REPORTS_ERROR"
    if status in {"needs_approval", "draft"}:
        return IntentState.NOT_SCHEDULED, f"PROVIDER_STATUS_{status.upper()}"
    if status == "scheduled":
        if post.due_at != approval.due_at or post.share_mode != "customScheduled":
            return IntentState.MISMATCH, "TIME_OR_MODE_DIFFERS"
        return IntentState.SCHEDULED, "PROVIDER_REPORTS_SCHEDULED"
    return None, "UNRECOGNISED_PROVIDER_STATUS"


def describe(state: IntentState) -> str:
    """What a state lets anyone say. Deliberately never 'published' except for SENT."""
    return {
        IntentState.SENDING: "request may have been sent; potentially sent, not retryable",
        IntentState.ACCEPTED: "provider accepted the request; NOT confirmed as scheduled",
        IntentState.AMBIGUOUS: "outcome unknown; may exist at the provider; never resent",
        IntentState.REJECTED: "provider refused the request; nothing was created",
        IntentState.SCHEDULED: "provider read-back confirms it is scheduled; NOT published yet",
        IntentState.NOT_SCHEDULED: "post exists at the provider but will NOT publish as it stands",
        IntentState.SENT: "provider reports it was sent (published)",
        IntentState.PROVIDER_ERROR: "provider reports an error; nothing is resent automatically",
        IntentState.MISMATCH: "provider copy differs from what was approved; needs the owner",
    }[state]
