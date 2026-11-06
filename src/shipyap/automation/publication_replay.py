"""Pinned, entirely offline replay of recorded publication history through the real send guard.

Historical approval rows are imported as evidence, never generated or approved by the replay.
The recorder implements no transport and has no provider/credential dependency.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from shipyap.editorial.grounding import check_draft, render_post
from shipyap.editorial.models import Brief, DraftResponse
from shipyap.publication.approval import Candidate, approval_id, text_sha256
from shipyap.publication.errors import PublicationError
from shipyap.publication.history import History
from shipyap.publication.ledger import _SCHEMA, IntentState, Ledger
from shipyap.publication.port import CreateKind, CreateOutcome
from shipyap.publication.service import submit


@dataclass(frozen=True)
class PublicationReplay:
    name: str
    recorded_sends: int
    create_calls: int
    outcomes: tuple[str, ...]


class Recorder:
    def __init__(self) -> None:
        self.creates = 0
        self.destinations = 0

    def check_destination(self, *_args) -> None:
        self.destinations += 1

    def create_scheduled_post(self, *_args) -> CreateOutcome:
        self.creates += 1
        return CreateOutcome(CreateKind.ACCEPTED, "ACCEPTED", "0" * 24)


def _file(root: Path, name: str) -> bytes:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("publication replay path escapes cases")
    return path.read_bytes()


def replay(root: Path, case: dict, name: str) -> PublicationReplay:
    if case.get("version") != 1:
        raise ValueError("unknown publication case version")
    original = _file(root, case["original_response"])
    brief_raw = _file(root, case["brief"])
    if hashlib.sha256(original).hexdigest() != case["original_response_sha256"]:
        raise ValueError("publication replay stored reply changed")
    brief = Brief.model_validate_json(brief_raw)
    response = DraftResponse.model_validate_json(original)
    if response.brief_sha256 != hashlib.sha256(brief_raw).hexdigest():
        raise ValueError("publication replay brief differs from stored reply")
    # Replay the real stored model reply through the same deterministic editorial checker too.
    check_draft(
        brief, response, hashlib.sha256(brief_raw).hexdigest(), hashlib.sha256(original).hexdigest()
    )
    if text_sha256(render_post(response.segments)) != case["original_text_sha256"]:
        raise ValueError("publication replay original text changed")
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        for statement in _SCHEMA:
            db.execute(statement)
        ledger = Ledger(db)
        provider = Recorder()
        outcomes = []
        rows = {a["approval_id"]: a for a in case["approvals"]}
        for event in case["events"]:
            now = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
            if event["kind"] == "review":
                row = event["row"]
                aid = row["approval_id"]
                if aid:
                    a = rows[aid]
                    if (
                        approval_id(a["text"], a["channel_id"], a["due_at"]) != aid
                        or text_sha256(a["text"]) != a["text_sha256"]
                    ):
                        raise ValueError("publication replay approval differs")
                    db.execute(
                        "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(
                            a[k]
                            for k in (
                                "approval_id",
                                "text",
                                "text_sha256",
                                "channel_id",
                                "due_at",
                                "workspace_id",
                                "bundle_record_sha256",
                                "approved_at",
                            )
                        ),
                    )
                    History(db).bind(aid, row["story_id"], aid)
                columns = (
                    "seq",
                    "story_id",
                    "decision",
                    "approval_id",
                    "text",
                    "text_sha256",
                    "draft_bundle",
                    "draft_record_sha256",
                    "origin_record_sha256",
                    "link_sha256",
                    "decided_at",
                )
                if text_sha256(row["text"]) != row["text_sha256"]:
                    raise ValueError("publication replay review text differs")
                db.execute(
                    "INSERT INTO review_decisions VALUES (" + ",".join("?" for _ in columns) + ")",
                    tuple(row[k] for k in columns),
                )
            elif event["kind"] == "submit":
                a = rows[event["approval_id"]]
                candidate = Candidate(
                    a["text"],
                    a["channel_id"],
                    a["due_at"],
                    a["workspace_id"],
                    a["bundle_record_sha256"],
                    case["story_id"],
                )
                before = provider.destinations
                try:
                    result = submit(ledger, provider, candidate, a["approval_id"], now)
                    outcomes.append(result.intent.state.value)
                except PublicationError as exc:
                    if before != provider.destinations:
                        raise ValueError(
                            "publication refusal occurred after provider access"
                        ) from None
                    outcomes.append(exc.code.value)
            elif event["kind"] == "state":
                ledger.transition(event["approval_id"], IntentState(event["state"]), now)
            else:
                raise ValueError("unknown publication replay event")
        return PublicationReplay(name, case["recorded_sends"], provider.creates, tuple(outcomes))
    finally:
        db.close()


def load_replays(root: Path) -> list[PublicationReplay]:
    return [
        replay(root, json.loads(path.read_text("utf-8")), path.stem)
        for path in sorted((root / "publications").glob("*.history.json"))
    ]


def failures(reports: list[PublicationReplay], pins: dict) -> list[str]:
    actual = {r.name: r for r in reports}
    errors = []
    for name in actual.keys() | pins.keys():
        if name not in actual or name not in pins:
            errors.append(f"PUBLICATION {name}: missing replay or pin")
            continue
        got = asdict(actual[name])
        for key in ("recorded_sends", "create_calls", "outcomes"):
            value = list(got[key]) if key == "outcomes" else got[key]
            if value != pins[name].get(key):
                errors.append(f"PUBLICATION {name}: {key} differs from pin")
    return errors
