"""`pros eval --workspace`: every draft in a live workspace, judged by the review page's own gate,
as "approvable: yes/no + reason". With `--check`, each answer must match the pin in
`expected.json` ("workspace_drafts", by story id), so a gate change that blocks a good draft, or
opens a bad one, fails the replay.

Read-only on the workspace: the story backlog and review ledger are copied into a temporary
folder through SQLite's backup API from read-only connections (opening the ledger in place takes
a write lock and may upgrade it); drafts, snapshots and fact checks are only read. No eval case is
saved, no provider exists, nothing is approved or scheduled. No processes, no network.
"""

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from shipyap.automation import factcheck
from shipyap.automation.config import load_config
from shipyap.automation.review import REVIEW_LEDGER
from shipyap.review import required

CONFIG_FILE = "pros.toml"


@dataclass(frozen=True)
class LiveDraft:
    story_id: str
    status: str  # what the review page says (e.g. "Ready to approve", "Scheduled ...")
    approvable: bool
    reason: str
    fact_check: str  # COMPLETE, INHERITED or REQUIRED
    refusal: str | None = None  # why the page does not list it, when it does not


def _copy_db(source: Path, target: Path) -> Path:
    if not source.is_file():
        return target  # the desk starts an empty ledger there, in the temporary folder
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _codes(reasons) -> str:
    return ", ".join(dict.fromkeys(line.split(" ", 1)[0].rstrip(":") for line in reasons))


def replay_live(workspace: Path) -> list[LiveDraft]:
    """Raises AutomationError (config), ReviewError or sqlite3.Error when unreadable."""
    from shipyap.review.service import ReviewDesk, ReviewError
    from shipyap.review.ux import status

    config = load_config(workspace / CONFIG_FILE)
    snapshots = (
        tuple(sorted(p for p in config.snapshots.iterdir() if p.is_dir()))
        if config.snapshots.is_dir()
        else ()
    )
    found: list[LiveDraft] = []
    with tempfile.TemporaryDirectory(prefix="pros-live-") as tmp:
        backlog = _copy_db(config.backlog, Path(tmp) / "story-backlog.sqlite")
        ledger = _copy_db(config.state / REVIEW_LEDGER, Path(tmp) / REVIEW_LEDGER)
        desk = ReviewDesk(
            config.drafts,
            backlog,
            ledger,
            snapshots,
            fact_checks=lambda story_id: factcheck.load(config.state, story_id),
            eval_cases=None,  # never write a decision case
            x_channel_id=config.x_channel_id,
        )
        items, refused = desk.queue()
        judged = [(item, None) for item in items]
        for entry in refused:
            try:
                judged.append((desk.item(entry.story_id), entry.reason.value))
            except ReviewError:
                found.append(
                    LiveDraft(
                        entry.story_id,
                        "Not reviewable",
                        False,
                        entry.reason.value,
                        required.REQUIRED,
                        entry.reason.value,
                    )
                )
        for item, refusal in judged:
            gate = desk.gate(item)
            check = desk.fact_check(item)
            if check is None:
                basis = required.REQUIRED
            elif isinstance(check.get("inherited_from"), str):
                basis = required.INHERITED
            else:
                basis = required.COMPLETE
            if gate.approvable:
                reason = (
                    f"fact-checked (inherited from {check['inherited_from']})"
                    if basis == required.INHERITED
                    else "fact-checked; no block"
                )
            else:
                reason = _codes(gate.reasons()) or "blocked"
            found.append(
                LiveDraft(
                    item.story.story_id,
                    status(item, gate),
                    gate.approvable,
                    reason,
                    basis,
                    refusal,
                )
            )
    return sorted(found, key=lambda d: d.story_id)


def render(drafts: list[LiveDraft]) -> list[str]:
    lines = []
    for draft in drafts:
        listed = f"; not listed: {draft.refusal}" if draft.refusal else ""
        lines.append(
            f"LIVE {draft.story_id[:16]}: approvable: {'yes' if draft.approvable else 'no'} "
            f"({draft.reason}); fact check {draft.fact_check}; page: {draft.status}{listed}"
        )
    lines.append(f"LIVE TOTAL drafts={len(drafts)} approvable={sum(d.approvable for d in drafts)}")
    return lines


def pin_failures(drafts: list[LiveDraft], pins: dict) -> list[str]:
    """Every live draft must be pinned, and every pin must name a live draft and match it."""
    failures = []
    live = {d.story_id: d for d in drafts}
    for story_id, want in pins.items():
        got = live.get(story_id)
        if got is None:
            failures.append(f"LIVE {story_id[:16]}: pinned draft missing from the workspace")
            continue
        if want.get("approvable") is not None and got.approvable != want["approvable"]:
            failures.append(
                f"LIVE {story_id[:16]}: approvable {got.approvable}, pinned "
                f"{want['approvable']} ({got.reason})"
            )
        if want.get("fact_check") is not None and got.fact_check != want["fact_check"]:
            failures.append(
                f"LIVE {story_id[:16]}: fact check {got.fact_check}, pinned {want['fact_check']}"
            )
    for story_id in live:
        if story_id not in pins:
            failures.append(f"LIVE {story_id[:16]}: not pinned in expected.json workspace_drafts")
    return failures
