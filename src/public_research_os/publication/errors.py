"""Fixed error codes for the Buffer publisher commands.

As elsewhere in this repository, a failure is a code plus a fixed message. Nothing taken from a
provider response, an environment variable, a path or an OS error is put into the message, so a
credential or post text cannot leak through an error.
"""

from enum import StrEnum


class PublicationErrorCode(StrEnum):
    # Gates before anything is sent.
    PUBLISHING_DISABLED = "PUBLISHING_DISABLED"
    API_KEY_MISSING = "API_KEY_MISSING"
    # The reviewed artifact.
    BUNDLE_INCOMPLETE = "BUNDLE_INCOMPLETE"
    BUNDLE_NOT_READY = "BUNDLE_NOT_READY"
    BUNDLE_INCONSISTENT = "BUNDLE_INCONSISTENT"
    TEXT_NOT_PUBLISHABLE = "TEXT_NOT_PUBLISHABLE"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    # Destination and time.
    CHANNEL_INVALID = "CHANNEL_INVALID"
    ORGANIZATION_INVALID = "ORGANIZATION_INVALID"
    SCHEDULE_INVALID = "SCHEDULE_INVALID"
    SCHEDULE_NOT_FUTURE = "SCHEDULE_NOT_FUTURE"
    DESTINATION_UNREADABLE = "DESTINATION_UNREADABLE"
    DESTINATION_MISMATCH = "DESTINATION_MISMATCH"
    DESTINATION_UNHEALTHY = "DESTINATION_UNHEALTHY"
    # Human approval.
    APPROVAL_NOT_INTERACTIVE = "APPROVAL_NOT_INTERACTIVE"
    APPROVAL_DECLINED = "APPROVAL_DECLINED"
    APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
    APPROVAL_MISMATCH = "APPROVAL_MISMATCH"
    APPROVAL_WITHDRAWN = "APPROVAL_WITHDRAWN"
    APPROVAL_NOT_REVIEWED_DRAFT = "APPROVAL_NOT_REVIEWED_DRAFT"
    APPROVAL_CONFLICT = "APPROVAL_CONFLICT"
    LEAD_NOT_ACCEPTED = "LEAD_NOT_ACCEPTED"
    # Review decisions (Assignment 06E).
    REVIEW_STALE = "REVIEW_STALE"
    REVIEW_CLOSED = "REVIEW_CLOSED"
    # Ledger.
    LEDGER_UNUSABLE = "LEDGER_UNUSABLE"
    LEDGER_WRITE_FAILED = "LEDGER_WRITE_FAILED"
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    # Provider reads.
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_UNEXPECTED_RESPONSE = "PROVIDER_UNEXPECTED_RESPONSE"
    ORGANIZATION_REQUIRED = "ORGANIZATION_REQUIRED"


MESSAGES: dict[PublicationErrorCode, str] = {
    PublicationErrorCode.PUBLISHING_DISABLED: (
        "publishing is disabled; it is enabled only by PUBLISHING_DISABLED=false in the "
        "environment of the trusted publisher shell"
    ),
    PublicationErrorCode.API_KEY_MISSING: (
        "PROS_BUFFER_API_KEY is not set to a single printable ASCII token"
    ),
    PublicationErrorCode.BUNDLE_INCOMPLETE: "--bundle is not a complete, unmodified draft bundle",
    PublicationErrorCode.BUNDLE_NOT_READY: (
        "the draft is not READY_FOR_REVIEW or has blocking editorial findings"
    ),
    PublicationErrorCode.BUNDLE_INCONSISTENT: (
        "draft.txt, the assertion map and the bundle record disagree"
    ),
    PublicationErrorCode.TEXT_NOT_PUBLISHABLE: (
        "the post text is empty, not UTF-8 encodable, or has characters a reviewer cannot see"
    ),
    PublicationErrorCode.TEXT_TOO_LONG: (
        "the post exceeds the Buffer length limit of its channel (LinkedIn 3,000; X 280)"
    ),
    PublicationErrorCode.CHANNEL_INVALID: "the channel id is not a 24-character lowercase hex id",
    PublicationErrorCode.ORGANIZATION_INVALID: (
        "the organization id is not a 24-character lowercase hex id"
    ),
    PublicationErrorCode.SCHEDULE_INVALID: (
        "the scheduling time is not an explicit UTC time like 2026-11-02T09:30:00Z"
    ),
    PublicationErrorCode.SCHEDULE_NOT_FUTURE: (
        "the scheduling time is not far enough in the future"
    ),
    PublicationErrorCode.DESTINATION_UNREADABLE: "the destination channel could not be read",
    PublicationErrorCode.DESTINATION_MISMATCH: (
        "the destination channel is not the approved channel of the expected network"
    ),
    PublicationErrorCode.DESTINATION_UNHEALTHY: (
        "the destination channel is disconnected, locked or has a paused queue"
    ),
    PublicationErrorCode.APPROVAL_NOT_INTERACTIVE: (
        "approval needs an interactive terminal; it cannot be piped or scripted"
    ),
    PublicationErrorCode.APPROVAL_DECLINED: "the owner did not give the exact confirmation phrase",
    PublicationErrorCode.APPROVAL_NOT_FOUND: "no owner approval is recorded for this approval id",
    PublicationErrorCode.APPROVAL_MISMATCH: (
        "the text, channel or time differs from what the owner approved"
    ),
    PublicationErrorCode.APPROVAL_WITHDRAWN: (
        "a later review decision (an edit, a rejection or another approval) withdrew this approval"
    ),
    PublicationErrorCode.APPROVAL_NOT_REVIEWED_DRAFT: (
        "this approval was given in review to a different draft bundle; schedule that bundle"
    ),
    PublicationErrorCode.APPROVAL_CONFLICT: (
        "the same text, channel and time is already approved from a different draft bundle; "
        "choose another time"
    ),
    PublicationErrorCode.LEAD_NOT_ACCEPTED: (
        "the X post follows its LinkedIn post: it is sent only after Buffer accepted the LinkedIn "
        "post of the same approval, and only while that approval stands"
    ),
    PublicationErrorCode.REVIEW_STALE: (
        "the review decisions for this draft changed since the page was shown; reload it"
    ),
    PublicationErrorCode.REVIEW_CLOSED: "this draft was rejected in review; a rejection is final",
    PublicationErrorCode.LEDGER_UNUSABLE: "the ledger database is missing, foreign or unusable",
    PublicationErrorCode.LEDGER_WRITE_FAILED: (
        "the ledger could not be updated; an intent already committed stays potentially sent"
    ),
    PublicationErrorCode.DUPLICATE_SUBMISSION: (
        "this approval already has a send intent; it is never submitted twice"
    ),
    PublicationErrorCode.PROVIDER_UNAVAILABLE: "the provider read failed or timed out",
    PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE: (
        "the provider reply did not have the expected shape"
    ),
    PublicationErrorCode.ORGANIZATION_REQUIRED: (
        "--organization-id is required to look for an ambiguous submission"
    ),
}


class PublicationError(Exception):
    def __init__(self, code: PublicationErrorCode) -> None:
        super().__init__(MESSAGES[code])
        self.code = code
