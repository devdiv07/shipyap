"""Offline builders for the drafting tests.

Two stand-ins for the provider, both socket-free (the conftest network guard stays on):
- `FakeDrafter` replaces the whole `Drafter`, for the service tests; it counts calls.
- `FakeApi` is an httpx2 mock transport answering as api.anthropic.com, so the provider and CLI
  tests run the real anthropic SDK end to end; it records every HTTP request.
All content is synthetic. KEY is a sentinel that must never appear in any output.
"""

import json
from pathlib import Path

import httpx2
from editorial_fake import CLEAN_SEGMENTS, SENTINEL, contribution, write_json

from public_research_os.drafting.errors import DraftingError
from public_research_os.drafting.models import ModelReply, ModelRequest, Usage
from public_research_os.editorial.service import run_brief

KEY = "sk-ant-api03-TESTKEY-never-print-0123456789"
REQUEST_ID = "req_011TestRequestId0001"
MODEL = "claude-opus-5-5"
COUNTED = 12_000
USAGE = (12_100, 3_000)


def model_output(segments=CLEAN_SEGMENTS, **overrides) -> dict:
    """What a model returns under MODEL_OUTPUT_SCHEMA: the editorial keys only."""
    data = {
        "outcome": "DRAFT",
        "no_post_reason": None,
        "segments": list(segments),
        "verification_needed": [],
    }
    data.update(overrides)
    return data


def model_text(segments=CLEAN_SEGMENTS, **overrides) -> str:
    return json.dumps(model_output(segments, **overrides))


def reply(
    text: str | None = None,
    *,
    texts: tuple[str, ...] | None = None,
    stop_reason: str | None = "end_turn",
    model: str = MODEL,
    request_id: str | None = REQUEST_ID,
    usage: tuple[int, int] = USAGE,
) -> ModelReply:
    if texts is None:
        texts = (model_text() if text is None else text,)
    return ModelReply(
        model=model,
        request_id=request_id,
        stop_reason=stop_reason,
        texts=texts,
        usage=Usage(*usage),
    )


class FakeDrafter:
    def __init__(
        self,
        answer: ModelReply | None = None,
        *,
        counted: int = COUNTED,
        count_error: DraftingError | None = None,
        generate_error: DraftingError | None = None,
    ) -> None:
        self.answer = reply() if answer is None else answer
        self.counted = counted
        self.count_error = count_error
        self.generate_error = generate_error
        self.count_calls: list[ModelRequest] = []
        self.generate_calls: list[ModelRequest] = []

    def count_input_tokens(self, request: ModelRequest) -> int:
        self.count_calls.append(request)
        if self.count_error is not None:
            raise self.count_error
        return self.counted

    def generate(self, request: ModelRequest) -> ModelReply:
        self.generate_calls.append(request)
        if self.generate_error is not None:
            raise self.generate_error
        return self.answer


def message_body(
    text: str | None = None,
    *,
    stop_reason: str = "end_turn",
    model: str = MODEL,
    usage: tuple[int, int] = USAGE,
) -> dict:
    """A Messages API success body: an (empty, default-display) thinking block, then the text."""
    return {
        "id": "msg_01Test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": model_text() if text is None else text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


def error_body() -> dict:
    return {"type": "error", "error": {"type": "api_error", "message": f"echo {SENTINEL} {KEY}"}}


class FakeApi:
    """Answers `/v1/messages/count_tokens` and `/v1/messages` like the Messages API."""

    def __init__(
        self,
        message: dict | None = None,
        *,
        counted: object = COUNTED,
        status: int = 200,
        count_status: int = 200,
        headers: dict[str, str] | None = None,
        raise_on: dict[str, Exception] | None = None,
    ) -> None:
        self.message = message_body() if message is None else message
        self.counted = counted
        self.status = status
        self.count_status = count_status
        self.headers = {"request-id": REQUEST_ID, **(headers or {})}
        self.raise_on = raise_on or {}
        self.requests: list[httpx2.Request] = []

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.raise_on:
            raise self.raise_on[path]
        if path == "/v1/messages/count_tokens":
            if self.count_status != 200:
                return httpx2.Response(self.count_status, json=error_body(), headers=self.headers)
            return httpx2.Response(200, json={"input_tokens": self.counted}, headers=self.headers)
        if path == "/v1/messages":
            if self.status != 200:
                return httpx2.Response(self.status, json=error_body(), headers=self.headers)
            return httpx2.Response(200, json=self.message, headers=self.headers)
        return httpx2.Response(404, json=error_body())


def make_brief(work: Path, snapshot: Path, name: str = "brief", **overrides) -> Path:
    contrib = write_json(work / f"contribution-{name}.json", contribution(**overrides))
    out = work / name
    run_brief(snapshot, contrib, out)
    return out
