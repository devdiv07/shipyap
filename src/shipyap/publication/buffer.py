"""The Buffer GraphQL adapter. The ONLY module that reads `PROS_BUFFER_API_KEY` or
`PUBLISHING_DISABLED`, and the only one in `publication/` that opens a network connection (tests
enforce both).

Containment:
- One fixed host (`api.buffer.com`, port 443) and path; neither is a parameter. Certificates are
  verified (TLS 1.2 or newer). Redirects are never followed: a 3xx is just an unexpected status,
  and the key header is never re-sent anywhere. The standard-library connection reads no proxy
  variable.
- ONE request per call and no automatic retry, anywhere. A timed-out `createPost` may already have
  been processed, so it is reported AMBIGUOUS and never resent.
- Scheduling is `customScheduled` with an explicit UTC `dueAt`, and nothing else: the mode and the
  scheduling type are constants, not parameters, so no other mode can be requested by any caller.
- The post text travels as a GraphQL variable, unchanged. Nothing trims, rewrites or truncates it.
- Bounded: per-operation socket timeout, a body cap, `Accept-Encoding: identity`, and an overall
  deadline checked between body chunks. NOT bounded by the deadline: name resolution, the TLS
  handshake and the status line/headers (only the per-operation timeout applies there).
- Provider text never leaves this module: a provider error `message` is not even requested; only
  allow-listed type names and fixed codes are returned, so neither the key nor post text can reach
  an error, a log line or the ledger.

Source of the API facts (Buffer's developer docs, read 2026-10-21; no Buffer API request was made
to learn them): endpoint `https://api.buffer.com`, `Authorization: Bearer <key>`, `createPost`
with `mode: customScheduled`, `dueAt` as UTC ISO 8601 and `schedulingType: automatic`, a payload of
`PostActionSuccess { post }` or typed errors implementing `MutationError`, HTTP 200 for GraphQL
errors, and 24-character hex ids.

UNVERIFIED (not exercised against the live server by this repository):
- the variable type names `CreatePostInput`, `ChannelInput`, `PostInput`, `PostsInput`, and the
  types of `$first`/`$after` (taken from the reference page and Gate 1 notes, not from a run);
- the exact spelling of the `PostStatus` values; the five named in the assignment are matched
  exactly and any other value is "unrecognised", never a confirmation;
- that `assets: []` and `needsApproval: false` (required by the input type; absent from Buffer's
  examples) are accepted as written;
- whether `posts` without a `dueAt` filter lists everything needed to find an ambiguous post.
- that an X text post needs no `metadata` (Buffer's guide asks for network metadata only for
  threads and similar options; the text post example carries none).
A wrong name makes Buffer answer with a GraphQL error, which this adapter treats as a failure or an
ambiguous outcome, never as success.
"""

import http.client
import json
import os
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

from shipyap.evidence.strict_json import StrictJsonError, loads_strict
from shipyap.publication.approval import (
    parse_channel_id,
    parse_post_id,
    provider_time,
)
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.port import (
    CreateKind,
    CreateOutcome,
    PostListing,
    ProviderPost,
)

API_KEY_ENV = "PROS_BUFFER_API_KEY"
PUBLISHING_DISABLED_ENV = "PUBLISHING_DISABLED"
API_HOST = "api.buffer.com"
API_PORT = 443
API_PATH = "/"
USER_AGENT = "public-research-os-publisher/0.1.2"
IDLE_TIMEOUT_SECONDS = 15.0
DEADLINE_SECONDS = 45.0
MAX_BODY_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
CHUNK_BYTES = 16 * 1024
PAGE_SIZE = 50
MAX_PAGES = 4

SCHEDULING_MODE = "customScheduled"
SCHEDULING_TYPE = "automatic"
LINKEDIN_SERVICE = "linkedin"
X_SERVICE = "twitter"  # Buffer's `service` value for an X channel ("Data Model" guide)
SERVICES = frozenset({LINKEDIN_SERVICE, X_SERVICE})
STATUS_VALUES = frozenset({"scheduled", "needs_approval", "draft", "sent", "error"})

# Typed errors that mean "the provider processed the request and created nothing". Anything
# else (including UnexpectedError and a type name this code has never seen) stays AMBIGUOUS.
_DEFINITIVE_ERROR_TYPES = frozenset(
    {"InvalidInputError", "LimitReachedError", "UnauthorizedError", "NotFoundError"}
)
_DEFINITIVE_ERROR_CODES = frozenset(
    {"UNAUTHORIZED", "FORBIDDEN", "NOT_FOUND", "RATE_LIMIT_EXCEEDED"}
)
_DEFINITIVE_HTTP = {401: "HTTP_401", 403: "HTTP_403", 429: "HTTP_429_RATE_LIMITED"}

CREATE_MUTATION = (
    "mutation CreatePost($input: CreatePostInput!) {"
    " createPost(input: $input) { __typename ... on PostActionSuccess { post { id } } } }"
)
CHANNEL_QUERY = (
    "query Channel($input: ChannelInput!) {"
    " channel(input: $input) { id service isDisconnected isLocked isQueuePaused } }"
)
_POST_FIELDS = "id status dueAt channelId text shareMode"
POST_QUERY = f"query Post($input: PostInput!) {{ post(input: $input) {{ {_POST_FIELDS} }} }}"
POSTS_QUERY = (
    "query Posts($first: Int!, $after: String, $input: PostsInput!) {"
    " posts(first: $first, after: $after, input: $input) {"
    f" edges {{ node {{ {_POST_FIELDS} }} }} pageInfo {{ hasNextPage endCursor }} }} }}"
)


def _environment(name: str) -> str | None:
    return os.environ.get(name)  # the only environment read in publication/ (a test enforces it)


def publishing_enabled() -> bool:
    """Publishing is off unless the variable is exactly `false`. Unset, empty, `true`, `0`,
    `False ` and every other value keep it off."""
    return _environment(PUBLISHING_DISABLED_ENV) == "false"


def load_api_key() -> SecretStr:
    value = _environment(API_KEY_ENV)
    if (
        not value
        or value != value.strip()
        or not value.isascii()
        or not value.isprintable()
        or " " in value
    ):
        raise PublicationError(PublicationErrorCode.API_KEY_MISSING)
    return SecretStr(value)


# --- transport ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportReply:
    status: int
    body: bytes


class TransportFailure(Exception):
    """No usable reply. Carries no text: the cause and any response bytes are dropped."""

    def __init__(self, *, timed_out: bool) -> None:
        super().__init__("transport failure")
        self.timed_out = timed_out


class BufferTransport(Protocol):
    def post(self, body: bytes) -> TransportReply: ...


class HttpsBufferTransport:
    """One HTTPS POST to the fixed endpoint. `connection_factory` exists for tests only."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._connection_factory = connection_factory
        self._clock = clock

    def __repr__(self) -> str:
        return "HttpsBufferTransport(api_key=<hidden>)"

    def post(self, body: bytes) -> TransportReply:
        if len(body) > MAX_REQUEST_BYTES:
            raise TransportFailure(timed_out=False)
        deadline = self._clock() + DEADLINE_SECONDS
        connection = None
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            connection = self._connection_factory(
                API_HOST, API_PORT, timeout=IDLE_TIMEOUT_SECONDS, context=context
            )
            connection.request("POST", API_PATH, body=body, headers=self._headers())
            response = connection.getresponse()
            return TransportReply(int(response.status), self._read(response, deadline))
        except TransportFailure:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            timed_out = isinstance(exc, TimeoutError | ssl.SSLWantReadError | ssl.SSLWantWriteError)
            raise TransportFailure(timed_out=timed_out) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }

    def _read(self, response: Any, deadline: float) -> bytes:
        encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
        if encoding not in {"", "identity"}:
            raise TransportFailure(timed_out=False)
        declared = response.getheader("Content-Length")
        if declared is not None and (
            not (declared.isascii() and declared.isdigit()) or int(declared) > MAX_BODY_BYTES
        ):
            raise TransportFailure(timed_out=False)
        chunks: list[bytes] = []
        total = 0
        while True:
            if self._clock() >= deadline:
                raise TransportFailure(timed_out=True)
            chunk = response.read1(min(CHUNK_BYTES, MAX_BODY_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise TransportFailure(timed_out=False)
            chunks.append(chunk)
        if declared is not None and total != int(declared):
            raise TransportFailure(timed_out=False)
        return b"".join(chunks)


# --- the adapter -------------------------------------------------------------------------------


class BufferPublisher:
    """`SchedulingProvider` for a LinkedIn or an X channel. Every method is a single provider
    request; none retries."""

    def __init__(self, transport: BufferTransport) -> None:
        self._transport = transport

    # Destination ----------------------------------------------------------------------------

    def check_destination(self, channel_id: str, service: str = LINKEDIN_SERVICE) -> None:
        parse_channel_id(channel_id)
        if service not in SERVICES:
            raise PublicationError(PublicationErrorCode.DESTINATION_MISMATCH)
        node = self._read("channel", CHANNEL_QUERY, {"input": {"id": channel_id}})
        if node is None:
            raise PublicationError(PublicationErrorCode.DESTINATION_UNREADABLE)
        if (
            not isinstance(node, dict)
            or node.get("id") != channel_id
            or node.get("service") != service
        ):
            raise PublicationError(PublicationErrorCode.DESTINATION_MISMATCH)
        flags = [node.get(key) for key in ("isDisconnected", "isLocked", "isQueuePaused")]
        if any(type(flag) is not bool for flag in flags):
            raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
        if any(flags):
            raise PublicationError(PublicationErrorCode.DESTINATION_UNHEALTHY)

    # The one mutation -----------------------------------------------------------------------

    def create_scheduled_post(self, text: str, channel_id: str, due_at: str) -> CreateOutcome:
        body = create_request_body(text, channel_id, due_at)  # invalid input: nothing is sent
        try:
            reply = self._transport.post(body)
        except TransportFailure as failure:
            return CreateOutcome(
                CreateKind.AMBIGUOUS, "TIMEOUT" if failure.timed_out else "NETWORK"
            )
        except Exception:  # noqa: BLE001 - after the attempt, nothing may escape as "not sent"
            return CreateOutcome(CreateKind.AMBIGUOUS, "TRANSPORT_ERROR")
        try:
            return _classify_create(reply)
        except Exception:  # noqa: BLE001 - same reason: the request was sent
            return CreateOutcome(CreateKind.AMBIGUOUS, "UNPARSEABLE_RESPONSE")

    # Read-only ------------------------------------------------------------------------------

    def read_post(self, post_id: str) -> ProviderPost | None:
        if parse_post_id(post_id) is None:
            raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
        node = self._read("post", POST_QUERY, {"input": {"id": post_id}})
        if node is None:
            return None
        post = _provider_post(node)
        if post is None or post.post_id != post_id:
            raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
        return post

    def list_posts(self, organization_id: str, channel_id: str) -> PostListing:
        posts: list[ProviderPost] = []
        complete = True
        cursor: str | None = None
        for page in range(MAX_PAGES):
            variables = {
                "first": PAGE_SIZE,
                "after": cursor,
                "input": {
                    "organizationId": organization_id,
                    "filter": {"channelIds": [channel_id], "status": sorted(STATUS_VALUES)},
                },
            }
            data = self._read("posts", POSTS_QUERY, variables)
            if not isinstance(data, dict) or not isinstance(data.get("edges"), list):
                raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
            for edge in data["edges"]:
                post = _provider_post(edge.get("node")) if isinstance(edge, dict) else None
                if post is None:
                    complete = False  # an unreadable entry could have been the one sought
                else:
                    posts.append(post)
            info = data.get("pageInfo")
            if not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool:
                raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
            if not info["hasNextPage"]:
                return PostListing(tuple(posts), complete)
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
            if page == MAX_PAGES - 1:
                complete = False
        return PostListing(tuple(posts), complete)

    def _read(self, field: str, query: str, variables: dict[str, Any]) -> Any:
        """One read request; returns `data[field]` (None for a null field)."""
        body = _json_bytes({"query": query, "variables": variables})
        try:
            reply = self._transport.post(body)
        except Exception:  # noqa: BLE001 - a read has no side effect; the cause is dropped
            raise PublicationError(PublicationErrorCode.PROVIDER_UNAVAILABLE) from None
        if reply.status != 200:
            raise PublicationError(PublicationErrorCode.PROVIDER_UNAVAILABLE)
        document = _decode(reply.body)
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, dict) or document.get("errors") or field not in data:
            raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE)
        return data[field]


# --- request and response shapes ---------------------------------------------------------------


def create_request_body(text: str, channel_id: str, due_at: str) -> bytes:
    """The one `createPost` request. Raises (so nothing is sent) on an invalid argument."""
    parse_channel_id(channel_id)
    if len(due_at) != 20 or provider_time(due_at) != due_at:
        raise PublicationError(PublicationErrorCode.SCHEDULE_INVALID)
    return _json_bytes(
        {
            "query": CREATE_MUTATION,
            "variables": {
                "input": {
                    "text": text,
                    "channelId": channel_id,
                    "schedulingType": SCHEDULING_TYPE,
                    "mode": SCHEDULING_MODE,
                    "dueAt": f"{due_at[:-1]}.000Z",
                    "needsApproval": False,
                    "assets": [],
                }
            },
        }
    )


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode(body: bytes) -> Any:
    try:
        return loads_strict(body)
    except (StrictJsonError, ValueError):
        raise PublicationError(PublicationErrorCode.PROVIDER_UNEXPECTED_RESPONSE) from None


def _classify_create(reply: TransportReply) -> CreateOutcome:
    if reply.status != 200:
        if reply.status in _DEFINITIVE_HTTP:
            return CreateOutcome(CreateKind.REJECTED, _DEFINITIVE_HTTP[reply.status])
        return CreateOutcome(CreateKind.AMBIGUOUS, "HTTP_STATUS")
    document = _decode(reply.body)
    if not isinstance(document, dict):
        return CreateOutcome(CreateKind.AMBIGUOUS, "UNEXPECTED_RESPONSE")
    data, errors = document.get("data"), document.get("errors")
    result = data.get("createPost") if isinstance(data, dict) else None
    if result is None:
        codes = _error_codes(errors)
        if codes and codes <= _DEFINITIVE_ERROR_CODES:
            return CreateOutcome(CreateKind.REJECTED, sorted(codes)[0])
        return CreateOutcome(CreateKind.AMBIGUOUS, "UNEXPECTED_RESPONSE")
    if errors or not isinstance(result, dict):
        return CreateOutcome(CreateKind.AMBIGUOUS, "UNEXPECTED_RESPONSE")
    typename = result.get("__typename")
    if typename == "PostActionSuccess":
        post = result.get("post")
        post_id = parse_post_id(post.get("id")) if isinstance(post, dict) else None
        if post_id is None:
            return CreateOutcome(CreateKind.AMBIGUOUS, "ACCEPTED_WITHOUT_VALID_ID")
        return CreateOutcome(CreateKind.ACCEPTED, "ACCEPTED", post_id)
    if typename in _DEFINITIVE_ERROR_TYPES:
        return CreateOutcome(CreateKind.REJECTED, str(typename))
    return CreateOutcome(CreateKind.AMBIGUOUS, "UNRECOGNISED_RESULT")


def _error_codes(errors: object) -> set[str]:
    """The `extensions.code` of every error, or an empty set if any error lacks a string code."""
    if not isinstance(errors, list) or not errors:
        return set()
    codes = set()
    for error in errors:
        extensions = error.get("extensions") if isinstance(error, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        if not isinstance(code, str):
            return set()
        codes.add(code)
    return codes


def _provider_post(node: object) -> ProviderPost | None:
    if not isinstance(node, dict):
        return None
    post_id, channel = parse_post_id(node.get("id")), node.get("channelId")
    status, text = node.get("status"), node.get("text")
    share_mode = node.get("shareMode")
    try:
        parse_channel_id(channel)  # type: ignore[arg-type]
    except PublicationError:
        return None
    if post_id is None or not isinstance(status, str) or not isinstance(text, str):
        return None
    return ProviderPost(
        post_id=post_id,
        status=status,
        due_at=provider_time(node.get("dueAt")),
        channel_id=str(channel),
        text=text,
        share_mode=share_mode if isinstance(share_mode, str) else None,
    )
