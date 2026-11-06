"""Owner approval of one exact post: what is bound, how it is identified and what the owner sees.

An approval binds exactly three things: the UTF-8 text, the Buffer channel id and the UTC
scheduling time. Its id is a SHA-256 over those three, so changing any of them is a different
(unapproved) post, and approving the same post twice is the same approval.

What an approval is NOT: authentication. The ledger is a local file; whoever can write it can
forge a row. The only protection is that the CLI demands a typed phrase at an interactive
terminal. The editorial `READY_FOR_REVIEW` status, zero checker warnings and a content digest are
never approval (see `docs/quality/open-issues.md`, EDIT-01).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.linkedin import LINKEDIN_MAX_UNITS, linkedin_units, utf16_units

# Buffer documents ids as 24-character hex (developers.buffer.com, "efficient API usage").
_BUFFER_ID = re.compile(r"[0-9a-f]{24}")
_DUE_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_PROVIDER_TIME = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})"
)
# Chosen here, not documented by Buffer: a time too close to now can pass while a request is made.
MIN_LEAD = timedelta(minutes=5)
SHEET_RULE = "-" * 60


def parse_channel_id(value: str) -> str:
    if not isinstance(value, str) or _BUFFER_ID.fullmatch(value) is None:
        raise PublicationError(PublicationErrorCode.CHANNEL_INVALID)
    return value


def parse_organization_id(value: str) -> str:
    if not isinstance(value, str) or _BUFFER_ID.fullmatch(value) is None:
        raise PublicationError(PublicationErrorCode.ORGANIZATION_INVALID)
    return value


def parse_post_id(value: object) -> str | None:
    return value if isinstance(value, str) and _BUFFER_ID.fullmatch(value) else None


def parse_due_at(value: str, now: datetime) -> str:
    """The canonical `YYYY-MM-DDTHH:MM:SSZ` for an explicit UTC time at least MIN_LEAD ahead."""
    if not isinstance(value, str) or _DUE_AT.fullmatch(value) is None:
        raise PublicationError(PublicationErrorCode.SCHEDULE_INVALID)
    try:
        due = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise PublicationError(PublicationErrorCode.SCHEDULE_INVALID) from None
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")  # programming error
    if due < now + MIN_LEAD:
        raise PublicationError(PublicationErrorCode.SCHEDULE_NOT_FUTURE)
    return value


def provider_time(value: object) -> str | None:
    """A provider timestamp as canonical UTC seconds, or None when it is absent, not a string,
    malformed or carries sub-second precision (which can never equal an approved time)."""
    if not isinstance(value, str):
        return None
    match = _PROVIDER_TIME.fullmatch(value)
    if match is None or (match.group(2) is not None and match.group(2).strip("0")):
        return None
    try:
        moment = datetime.fromisoformat(f"{match.group(1)}{match.group(3).replace('Z', '+00:00')}")
    except ValueError:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def approval_id(text: str, channel_id: str, due_at: str) -> str:
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload = json.dumps(
        {"channel_id": channel_id, "due_at": due_at, "text_sha256": text_sha256, "version": 1},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def confirmation_phrase(approval: str) -> str:
    """What the owner must type. It contains part of the approval id, so a reflex "yes" or a
    phrase learnt for a different post does not approve this one."""
    return f"APPROVE {approval[:12]}"


@dataclass(frozen=True)
class Candidate:
    """One post as it would be sent, built from the reviewed bundle and the stated destination."""

    text: str
    channel_id: str
    due_at: str
    workspace_id: str
    bundle_record_sha256: str
    story_id: str | None = None

    @property
    def approval_id(self) -> str:
        return approval_id(self.text, self.channel_id, self.due_at)


@dataclass(frozen=True)
class Approval:
    approval_id: str
    text: str
    text_sha256: str
    channel_id: str
    due_at: str
    workspace_id: str
    bundle_record_sha256: str
    approved_at: str


def approval_sheet(candidate: Candidate) -> str:
    """What the owner is shown before approving: the exact text, where it goes and when."""
    text = candidate.text
    return (
        f"{SHEET_RULE}\n"
        "POST TEXT (exactly as it would be sent; line breaks are real)\n"
        f"{SHEET_RULE}\n"
        f"{text}\n"
        f"{SHEET_RULE}\n"
        f"destination   : Buffer LinkedIn channel {candidate.channel_id}\n"
        f"target time   : {candidate.due_at} (UTC), to request as customScheduled; approving\n"
        "                sends nothing, and Buffer has not scheduled it\n"
        f"utf-8 bytes   : {len(text.encode('utf-8'))}   sha256 {text_sha256(text)}\n"
        f"length        : {linkedin_units(text)} of {LINKEDIN_MAX_UNITS} "
        f"(UTF-16 units: {utf16_units(text)}; URLs count 24)\n"
        "The text is not changed, trimmed or reformatted after approval. A semantic check\n"
        "of the claims in it has NOT been done by any program.\n"
        f"{SHEET_RULE}\n"
    )
