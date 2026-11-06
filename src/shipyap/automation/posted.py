"""After each run: read back every scheduled post that is not finished, so the owner hears
when one went live or failed. Read-only (the publisher's `reconcile`): it never sends, edits
or resends anything. Needs only PROS_BUFFER_API_KEY; without it, nothing is checked."""

from datetime import UTC, datetime
from pathlib import Path

from shipyap.publication.errors import PublicationError
from shipyap.publication.ledger import IntentState, Ledger

_TELL = {
    IntentState.SENT: "Posted",
    IntentState.PROVIDER_ERROR: "Buffer reports an error for",
    IntentState.NOT_SCHEDULED: "Not scheduled in Buffer (draft or awaiting approval):",
    IntentState.MISMATCH: "Buffer's copy differs from what you approved:",
}


def check_posted(ledger_path: Path, now: datetime | None = None) -> list[str]:
    """One message per post whose state changed to something the owner must hear about."""
    if not ledger_path.is_file():
        return []
    from shipyap.publication import buffer
    from shipyap.publication.service import reconcile

    try:
        key = buffer.load_api_key()
    except PublicationError:
        return []
    provider = buffer.BufferPublisher(buffer.HttpsBufferTransport(key))
    messages = []
    try:
        with Ledger.open(ledger_path, create=False) as ledger:
            for item in reconcile(ledger, provider, now or datetime.now(UTC)):
                if item.after == item.before or item.after not in _TELL:
                    continue
                approval = ledger.get_approval(item.approval_id)
                snippet = (approval.text if approval else "").replace("\n", " ")[:60]
                messages.append(f"{_TELL[item.after]} {snippet}")
    except PublicationError:
        return ["Could not read post status from Buffer this time; nothing was resent"]
    return messages
