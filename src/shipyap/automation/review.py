"""`pros review`: the review page with every path taken from the config, the approval form
pre-filled (saved channel, next free posting slot). Ctrl+click the printed link to open it.

Scheduling is offered only when the publisher's two gates are open (PUBLISHING_DISABLED=false
and PROS_BUFFER_API_KEY), when pros review starts; otherwise the page
says it is off. Nothing here sends anything on its own: every approval and schedule is still
the owner's typed confirmation on the page."""

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from shipyap.automation import factcheck
from shipyap.automation.config import AutomationError, load_config
from shipyap.automation.engine import STOP_FILE
from shipyap.automation.slots import describe_local, next_free_slot
from shipyap.publication.errors import PublicationError
from shipyap.review.pages import STOP_MESSAGE, ApprovalDefaults
from shipyap.stories.backlog import StoryBacklog
from shipyap.stories.errors import StoryError

REVIEW_LEDGER = "review-ledger.sqlite"
EVAL_CASES = "eval-cases"


def taken_slots(ledger: Path) -> set[str]:
    """UTC times already bound by an approval in the review ledger (read-only)."""
    if not ledger.is_file():
        return set()
    db = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
    try:
        return {row[0] for row in db.execute("SELECT due_at FROM approvals")}
    except sqlite3.Error:
        return set()
    finally:
        db.close()


def _desk(config, snapshots: tuple[Path, ...]):
    from shipyap.review.service import ReviewDesk

    return ReviewDesk(
        config.drafts,
        config.backlog,
        config.state / REVIEW_LEDGER,
        snapshots,
        fact_checks=lambda story_id: factcheck.load(config.state, story_id),
        eval_cases=config.state / EVAL_CASES,
        x_channel_id=config.x_channel_id,
    )


def waiting(config) -> tuple[str, ...]:
    """Story ids the review page lists as waiting for the owner (UNAPPROVED), in page order.
    Read-only apart from the review ledger the page itself would create; () if anything fails."""
    from shipyap.review.service import ReviewError, ReviewState

    try:
        snapshots = tuple(sorted(p for p in config.snapshots.iterdir() if p.is_dir()))
        items, _ = _desk(config, snapshots).queue()
    except (ReviewError, StoryError, PublicationError, OSError, sqlite3.Error):
        return ()
    return tuple(i.story.story_id for i in items if i.state == ReviewState.UNAPPROVED)


def serve(config_path: Path, port: int) -> int:
    try:
        config = load_config(config_path)
    except AutomationError as exc:
        print(f"NEEDS_ATTENTION: {exc}")
        return 2
    ledger = config.state / REVIEW_LEDGER
    from shipyap.automation.initialise import DEMO_MARKER

    if (config.workspace_root / DEMO_MARKER).exists():
        print("DEMO_WORKSPACE: use shipyap demo; normal scheduling is unavailable for sample data.")
        return 2
    try:
        # A fresh workspace has none of these yet; the page then says there is nothing to review.
        for directory in (config.state, config.snapshots, config.drafts):
            directory.mkdir(parents=True, exist_ok=True)
        StoryBacklog.open(config.backlog, create=True).close()
        snapshots = tuple(sorted(p for p in config.snapshots.iterdir() if p.is_dir()))
    except StoryError as exc:
        print(f"NEEDS_ATTENTION: {exc.code.value}")
        return 2
    except OSError:
        print("NEEDS_ATTENTION: WORKSPACE_UNWRITABLE (check the paths in your config)")
        return 2

    def stopped() -> bool:
        return (config.workspace_root / STOP_FILE).exists()

    def defaults() -> ApprovalDefaults:
        due = next_free_slot(
            datetime.now(UTC), config.posting_slots, config.utc_offset, taken_slots(ledger)
        )
        local = describe_local(due, config.utc_offset) if due else ""
        return ApprovalDefaults(config.buffer_channel_id, due or "", local)

    from shipyap.publication import buffer
    from shipyap.review.server import ReviewServer
    from shipyap.review.service import ReviewError

    scheduler = None
    reason = "PUBLISHING_DISABLED is not 'false'"
    if buffer.publishing_enabled():
        try:
            key = buffer.load_api_key()
        except PublicationError:
            reason = "PROS_BUFFER_API_KEY is missing"
        else:

            def scheduler():
                return buffer.BufferPublisher(buffer.HttpsBufferTransport(key))

    try:
        desk = _desk(config, snapshots)
        server = ReviewServer(desk, port, scheduler=scheduler, defaults=defaults, stopped=stopped)
    except (ReviewError, StoryError, PublicationError) as exc:
        print(f"NEEDS_ATTENTION: {exc.code.value}")
        return 2
    except OSError:
        print("NEEDS_ATTENTION: PORT_UNAVAILABLE (is another review page already open?)")
        return 2
    with server:
        from shipyap.automation.launch import instruction

        instruction(server.start_url)
        print("Do not share this link. Ctrl+C stops the page.")
        if stopped():
            print(STOP_MESSAGE)
        if scheduler is None:
            print(f"Scheduling is OFF ({reason}); you can still read, edit and approve.")
        else:
            print("Scheduling is ON: one Buffer request per approved post, only when you confirm.")
        print(f"X: {'ON, after LinkedIn in the same slot' if config.x_channel_id else 'off'}.")
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
