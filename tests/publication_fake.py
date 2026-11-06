"""Offline builders for the Buffer publisher tests.

Draft bundles are real `draft-build` outputs (editorial code, synthetic content). Buffer is a
scripted fake transport: nothing here opens a socket, and all content is synthetic.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from drafting_fake import make_brief
from editorial_fake import CLEAN_POST, CLEAN_SEGMENTS, make_snapshot, response, seg, write_json

from public_research_os.editorial.service import run_build
from public_research_os.publication.buffer import TransportFailure, TransportReply

KEY = "bufkey-SENTINEL-0123456789abcdef"
CHANNEL = "a1" * 12
OTHER_CHANNEL = "d4" * 12
ORG = "b2" * 12
POST_ID = "c3" * 12
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
DUE = "2026-11-02T09:30:00Z"
POST_TEXT = CLEAN_POST


def make_bundle(work: Path, name: str = "bundle", segments=CLEAN_SEGMENTS) -> Path:
    """A real READY_FOR_REVIEW draft bundle (or whatever `segments` check out as)."""
    snapshot = make_snapshot(work, name=f"snap-{name}")
    brief = make_brief(work, snapshot, name=f"brief-{name}")
    brief_sha = hashlib.sha256((brief / "brief.json").read_bytes()).hexdigest()
    path = write_json(work / f"response-{name}.json", response(brief_sha, segments))
    out = work / name
    run_build(snapshot, brief, path, out)
    return out


def blocking_segments():
    """A draft whose number is not in its cited evidence: NUMBER_NOT_IN_EVIDENCE (blocking)."""
    return (seg("S01", "The producing step was linked for 99/99 synthetic artifacts.", "FRAMING"),)


# --- Buffer replies ----------------------------------------------------------------------------


def reply(document: dict, status: int = 200) -> TransportReply:
    return TransportReply(status, json.dumps(document).encode("utf-8"))


def create_ok(post_id: str = POST_ID) -> TransportReply:
    result = {"__typename": "PostActionSuccess", "post": {"id": post_id}}
    return reply({"data": {"createPost": result}})


def create_error(typename: str) -> TransportReply:
    return reply({"data": {"createPost": {"__typename": typename}}})


def node(
    post_id: str = POST_ID,
    status: str = "scheduled",
    due: str | None = "2026-11-02T09:30:00.000Z",
    channel: str = CHANNEL,
    text: str = POST_TEXT,
    mode: str = "customScheduled",
) -> dict:
    return {
        "id": post_id,
        "status": status,
        "dueAt": due,
        "channelId": channel,
        "text": text,
        "shareMode": mode,
    }


def post_reply(**fields) -> TransportReply:
    return reply({"data": {"post": node(**fields)}})


def listing_reply(*nodes: dict, more: bool = False, cursor: str = "c") -> TransportReply:
    edges = [{"node": n} for n in nodes]
    info = {"hasNextPage": more, "endCursor": cursor if more else None}
    return reply({"data": {"posts": {"edges": edges, "pageInfo": info}}})


def channel_reply(
    channel: str = CHANNEL,
    service: str = "linkedin",
    disconnected: bool = False,
    locked: bool = False,
    paused: bool = False,
) -> TransportReply:
    chan = {
        "id": channel,
        "service": service,
        "isDisconnected": disconnected,
        "isLocked": locked,
        "isQueuePaused": paused,
    }
    return reply({"data": {"channel": chan}})


class FakeTransport:
    """Scripted replies, in order. A script entry may be a reply, an exception to raise or a
    callable taking the request body. Every request body is recorded; running out of script is a
    test failure, so an unexpected extra request cannot pass silently."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.bodies: list[bytes] = []

    def post(self, body: bytes) -> TransportReply:
        self.bodies.append(body)
        if not self.script:
            raise AssertionError("unexpected extra Buffer request")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(body)
        return item

    @property
    def requests(self) -> list[dict]:
        return [json.loads(body) for body in self.bodies]

    def operations(self) -> list[str]:
        """'mutation' or 'query' for each recorded request."""
        return [r["query"].split(None, 1)[0] for r in self.requests]


def timeout() -> TransportFailure:
    return TransportFailure(timed_out=True)


def always(item) -> Callable:
    return lambda _body: item
