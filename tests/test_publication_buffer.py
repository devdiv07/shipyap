"""The Buffer adapter over a scripted transport, and the real HTTPS transport over a fake
connection: request shape, outcome classification, destination checks, bounds. No socket."""

import json

import pytest
from publication_fake import (
    CHANNEL,
    DUE,
    KEY,
    OTHER_CHANNEL,
    POST_ID,
    POST_TEXT,
    FakeTransport,
    channel_reply,
    create_error,
    create_ok,
    listing_reply,
    node,
    post_reply,
    reply,
    timeout,
)
from pydantic import SecretStr

from shipyap.publication import buffer
from shipyap.publication.buffer import (
    BufferPublisher,
    HttpsBufferTransport,
    TransportFailure,
    TransportReply,
)
from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.port import CreateKind

E = PublicationErrorCode
K = CreateKind


def refused(call, *args, **kwargs) -> PublicationErrorCode:
    with pytest.raises(PublicationError) as raised:
        call(*args, **kwargs)
    return raised.value.code


def create(*script, text=POST_TEXT, channel=CHANNEL, due=DUE):
    transport = FakeTransport(*script)
    outcome = BufferPublisher(transport).create_scheduled_post(text, channel, due)
    return outcome, transport


# --- the one mutation --------------------------------------------------------------------------


def test_the_request_is_a_custom_scheduled_create_post_with_the_exact_text():
    text = 'Line one\n\nLine two é \U0001f600 一 "quoted" {braces} $var'
    outcome, transport = create(create_ok(), text=text)
    assert outcome == buffer.CreateOutcome(K.ACCEPTED, "ACCEPTED", POST_ID)
    (request,) = transport.requests
    assert request["query"].startswith("mutation CreatePost")
    assert request["variables"]["input"] == {
        "text": text,  # a variable: never spliced into the query, never altered
        "channelId": CHANNEL,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": "2026-11-02T09:30:00.000Z",
        "needsApproval": False,
        "assets": [],
    }
    assert text not in request["query"]
    assert transport.bodies[0].decode("utf-8").count(text.split("\n")[0]) == 1


@pytest.mark.parametrize("word", ["shareNow", "shareNext", "addToQueue", "recommendedTime"])
def test_no_other_scheduling_mode_exists_in_any_request_or_in_the_module(word):
    _, transport = create(create_ok())
    assert word not in transport.bodies[0].decode("utf-8")
    source = buffer.__file__
    assert word not in open(source, encoding="utf-8").read()


@pytest.mark.parametrize("channel", ["", "xyz", "A1" * 12, "a1" * 13])
def test_an_invalid_channel_sends_nothing(channel):
    transport = FakeTransport()
    publisher = BufferPublisher(transport)
    assert refused(publisher.create_scheduled_post, "text", channel, DUE) == E.CHANNEL_INVALID
    assert transport.bodies == []


@pytest.mark.parametrize(
    "due", ["", "2026-11-02T09:30:00.000Z", "2026-11-02T10:30:00+01:00", "2026-11-02", "soon"]
)
def test_an_invalid_time_sends_nothing(due):
    transport = FakeTransport()
    publisher = BufferPublisher(transport)
    assert refused(publisher.create_scheduled_post, "text", CHANNEL, due) == E.SCHEDULE_INVALID
    assert transport.bodies == []


@pytest.mark.parametrize(
    ("script", "kind", "code", "post_id"),
    [
        (create_ok(), K.ACCEPTED, "ACCEPTED", POST_ID),
        (create_error("InvalidInputError"), K.REJECTED, "InvalidInputError", None),
        (create_error("LimitReachedError"), K.REJECTED, "LimitReachedError", None),
        (create_error("UnauthorizedError"), K.REJECTED, "UnauthorizedError", None),
        (create_error("NotFoundError"), K.REJECTED, "NotFoundError", None),
        (create_error("UnexpectedError"), K.AMBIGUOUS, "UNRECOGNISED_RESULT", None),
        (create_error("SomethingNew"), K.AMBIGUOUS, "UNRECOGNISED_RESULT", None),
        (
            reply(
                {"data": {"createPost": None}, "errors": [{"extensions": {"code": "UNAUTHORIZED"}}]}
            ),
            K.REJECTED,
            "UNAUTHORIZED",
            None,
        ),
        (
            reply({"errors": [{"extensions": {"code": "RATE_LIMIT_EXCEEDED"}}]}),
            K.REJECTED,
            "RATE_LIMIT_EXCEEDED",
            None,
        ),
        (
            reply({"errors": [{"extensions": {"code": "UNEXPECTED"}}]}),
            K.AMBIGUOUS,
            "UNEXPECTED_RESPONSE",
            None,
        ),
        (
            reply({"errors": [{"extensions": {"code": "NOT_FOUND"}}, {"message": "no code"}]}),
            K.AMBIGUOUS,
            "UNEXPECTED_RESPONSE",
            None,
        ),
        (
            reply(
                {
                    "data": {
                        "createPost": {"__typename": "PostActionSuccess", "post": {"id": "nope"}}
                    }
                }
            ),
            K.AMBIGUOUS,
            "ACCEPTED_WITHOUT_VALID_ID",
            None,
        ),
        (
            reply({"data": {"createPost": {"__typename": "PostActionSuccess"}}}),
            K.AMBIGUOUS,
            "ACCEPTED_WITHOUT_VALID_ID",
            None,
        ),
        (
            reply(
                {
                    "data": {
                        "createPost": {"__typename": "PostActionSuccess", "post": {"id": POST_ID}}
                    },
                    "errors": [{"extensions": {"code": "UNEXPECTED"}}],
                }
            ),
            K.AMBIGUOUS,
            "UNEXPECTED_RESPONSE",
            None,
        ),
        (TransportReply(401, b"{}"), K.REJECTED, "HTTP_401", None),
        (TransportReply(403, b"{}"), K.REJECTED, "HTTP_403", None),
        (TransportReply(429, b"{}"), K.REJECTED, "HTTP_429_RATE_LIMITED", None),
        (TransportReply(500, b"{}"), K.AMBIGUOUS, "HTTP_STATUS", None),
        (TransportReply(502, b"<html>"), K.AMBIGUOUS, "HTTP_STATUS", None),
        (TransportReply(302, b""), K.AMBIGUOUS, "HTTP_STATUS", None),
        (TransportReply(400, b"{}"), K.AMBIGUOUS, "HTTP_STATUS", None),
        (TransportReply(200, b"not json"), K.AMBIGUOUS, "UNPARSEABLE_RESPONSE", None),
        (TransportReply(200, b""), K.AMBIGUOUS, "UNPARSEABLE_RESPONSE", None),
        (
            TransportReply(200, b'{"data": {"createPost": null}, "data": 1}'),
            K.AMBIGUOUS,
            "UNPARSEABLE_RESPONSE",
            None,
        ),
        (TransportReply(200, b"[]"), K.AMBIGUOUS, "UNEXPECTED_RESPONSE", None),
        (reply({"data": None}), K.AMBIGUOUS, "UNEXPECTED_RESPONSE", None),
    ],
)
def test_outcomes_are_classified_conservatively(script, kind, code, post_id):
    outcome, transport = create(script)
    assert (outcome.kind, outcome.code, outcome.post_id) == (kind, code, post_id)
    assert len(transport.bodies) == 1  # one attempt, whatever the result


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (timeout(), "TIMEOUT"),
        (TransportFailure(timed_out=False), "NETWORK"),
        (RuntimeError(f"boom {KEY}"), "TRANSPORT_ERROR"),
        (OSError(f"reset {POST_TEXT}"), "TRANSPORT_ERROR"),
    ],
)
def test_a_failed_attempt_is_ambiguous_after_exactly_one_request_and_leaks_nothing(failure, code):
    outcome, transport = create(failure)
    assert (outcome.kind, outcome.code, outcome.post_id) == (K.AMBIGUOUS, code, None)
    assert len(transport.bodies) == 1
    assert KEY not in repr(outcome) and POST_TEXT not in repr(outcome)


def test_a_provider_message_is_never_requested_or_returned():
    outcome, transport = create(
        reply(
            {"data": {"createPost": {"__typename": "InvalidInputError", "message": f"bad {KEY}"}}}
        )
    )
    assert "message" not in transport.requests[0]["query"]
    assert KEY not in repr(outcome)


# --- destination -------------------------------------------------------------------------------


def check(*script, channel=CHANNEL):
    transport = FakeTransport(*script)
    BufferPublisher(transport).check_destination(channel)
    return transport


def test_a_healthy_linkedin_channel_passes_with_one_read():
    transport = check(channel_reply())
    assert transport.operations() == ["query"]
    assert transport.requests[0]["variables"] == {"input": {"id": CHANNEL}}


@pytest.mark.parametrize(
    ("script", "code"),
    [
        (channel_reply(channel=OTHER_CHANNEL), E.DESTINATION_MISMATCH),
        (channel_reply(service="twitter"), E.DESTINATION_MISMATCH),
        (channel_reply(service="LINKEDIN"), E.DESTINATION_MISMATCH),
        (channel_reply(disconnected=True), E.DESTINATION_UNHEALTHY),
        (channel_reply(locked=True), E.DESTINATION_UNHEALTHY),
        (channel_reply(paused=True), E.DESTINATION_UNHEALTHY),
        (reply({"data": {"channel": None}}), E.DESTINATION_UNREADABLE),
        (
            reply({"data": {"channel": {"id": CHANNEL, "service": "linkedin"}}}),
            E.PROVIDER_UNEXPECTED_RESPONSE,
        ),
        (reply({"errors": [{"message": "x"}]}), E.PROVIDER_UNEXPECTED_RESPONSE),
        (TransportReply(500, b""), E.PROVIDER_UNAVAILABLE),
        (timeout(), E.PROVIDER_UNAVAILABLE),
        (RuntimeError("x"), E.PROVIDER_UNAVAILABLE),
    ],
)
def test_an_unhealthy_or_mismatched_destination_is_refused(script, code):
    assert refused(check, script) == code


def test_an_x_channel_is_checked_as_twitter_and_a_linkedin_reply_is_a_mismatch():
    transport = FakeTransport(channel_reply(service="twitter"))
    BufferPublisher(transport).check_destination(CHANNEL, "twitter")
    assert transport.operations() == ["query"]
    publisher = BufferPublisher(FakeTransport(channel_reply()))
    assert refused(publisher.check_destination, CHANNEL, "twitter") == E.DESTINATION_MISMATCH


def test_an_unknown_network_is_refused_before_any_request():
    transport = FakeTransport()
    publisher = BufferPublisher(transport)
    assert refused(publisher.check_destination, CHANNEL, "x") == E.DESTINATION_MISMATCH
    assert transport.bodies == []


def test_a_malformed_channel_id_is_refused_before_any_request():
    transport = FakeTransport()
    assert refused(BufferPublisher(transport).check_destination, "nope") == E.CHANNEL_INVALID
    assert transport.bodies == []


# --- reads -------------------------------------------------------------------------------------


def test_read_post_returns_the_provider_copy_with_canonical_time():
    post = BufferPublisher(FakeTransport(post_reply())).read_post(POST_ID)
    assert (post.status, post.due_at, post.channel_id, post.text) == (
        "scheduled",
        DUE,
        CHANNEL,
        POST_TEXT,
    )
    assert post.share_mode == "customScheduled"


def test_read_post_for_an_unknown_post_is_none_and_a_wrong_id_is_unexpected():
    assert (
        BufferPublisher(FakeTransport(reply({"data": {"post": None}}))).read_post(POST_ID) is None
    )
    other = FakeTransport(post_reply(post_id="f0" * 12))
    assert refused(BufferPublisher(other).read_post, POST_ID) == E.PROVIDER_UNEXPECTED_RESPONSE


def test_listing_marks_a_capped_or_unreadable_page_incomplete():
    one = BufferPublisher(FakeTransport(listing_reply(node()))).list_posts("b2" * 12, CHANNEL)
    assert one.complete and len(one.posts) == 1
    pages = [listing_reply(node(), more=True)] * buffer.MAX_PAGES
    capped = BufferPublisher(FakeTransport(*pages)).list_posts("b2" * 12, CHANNEL)
    assert not capped.complete and len(capped.posts) == buffer.MAX_PAGES
    broken = BufferPublisher(FakeTransport(listing_reply({"id": "bad"}, node()))).list_posts(
        "b2" * 12, CHANNEL
    )
    assert not broken.complete and len(broken.posts) == 1


# --- the real transport over a fake connection -------------------------------------------------


class FakeConnection:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.seen = None
        self.requests = 0
        self.closed = False

    def __call__(self, host, port, *, timeout, context):
        self.seen = {"host": host, "port": port, "timeout": timeout, "context": context}
        return self

    def request(self, method, path, body=None, headers=None):
        self.requests += 1
        self.seen.update(method=method, path=path, body=body, headers=headers)
        if self.error:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status=200, body=b'{"data":{}}', headers=None, chunk=7):
        self.status, self._body, self._headers, self._chunk = status, body, headers or {}, chunk

    def getheader(self, name):
        return self._headers.get(name)

    def read1(self, size):
        out, self._body = self._body[: min(size, self._chunk)], self._body[self._chunk :]
        return out


def send(connection, body=b"{}", clock=None):
    transport = HttpsBufferTransport(
        SecretStr(KEY), connection_factory=connection, **({"clock": clock} if clock else {})
    )
    return transport.post(body)


def test_the_transport_talks_only_to_the_fixed_endpoint_with_the_key_in_one_header():
    connection = FakeConnection(FakeResponse(body=b'{"data":{"a":1}}'))
    result = send(connection, b'{"query":"x"}')
    assert result == TransportReply(200, b'{"data":{"a":1}}')
    seen = connection.seen
    assert (seen["host"], seen["port"], seen["method"], seen["path"]) == (
        "api.buffer.com",
        443,
        "POST",
        "/",
    )
    assert seen["headers"]["Authorization"] == f"Bearer {KEY}"
    assert seen["headers"]["Accept-Encoding"] == "identity"
    assert seen["timeout"] == buffer.IDLE_TIMEOUT_SECONDS
    assert seen["context"].minimum_version.name == "TLSv1_2"
    assert connection.closed
    assert KEY not in repr(HttpsBufferTransport(SecretStr(KEY)))


def test_a_redirect_is_returned_as_a_status_and_never_followed():
    connection = FakeConnection(FakeResponse(302, b"", {"Location": "https://evil.example/"}))
    result = send(connection)
    assert result.status == 302 and result.body == b""
    assert connection.seen["host"] == "api.buffer.com"  # one connection, one host


@pytest.mark.parametrize(
    ("response", "timed_out"),
    [
        (FakeResponse(headers={"Content-Encoding": "gzip"}), False),
        (FakeResponse(headers={"Content-Length": "9999999"}), False),
        (FakeResponse(headers={"Content-Length": "abc"}), False),
        (FakeResponse(body=b"x" * (buffer.MAX_BODY_BYTES + 1), chunk=buffer.CHUNK_BYTES), False),
        (FakeResponse(body=b"short", headers={"Content-Length": "50"}), False),
    ],
)
def test_unbounded_or_inconsistent_bodies_are_failures(response, timed_out):
    with pytest.raises(TransportFailure) as raised:
        send(FakeConnection(response))
    assert raised.value.timed_out is timed_out


def test_the_overall_deadline_is_enforced_between_chunks():
    ticks = iter([0.0, 1.0, 100.0, 200.0])
    with pytest.raises(TransportFailure) as raised:
        send(FakeConnection(FakeResponse(body=b"x" * 100)), clock=lambda: next(ticks))
    assert raised.value.timed_out is True


@pytest.mark.parametrize(
    ("error", "timed_out"),
    [
        (TimeoutError(f"timed out {KEY}"), True),
        (ConnectionResetError(f"reset {KEY}"), False),
        (OSError(f"gaierror {KEY}"), False),
        (json.JSONDecodeError("x", "y", 0), False),
    ],
)
def test_transport_errors_are_reduced_to_a_flag_with_no_text(error, timed_out):
    with pytest.raises(TransportFailure) as raised:
        send(FakeConnection(error=error))
    assert raised.value.timed_out is timed_out
    assert KEY not in str(raised.value) and raised.value.__cause__ is None


def test_a_request_over_the_cap_is_not_sent():
    connection = FakeConnection(FakeResponse())
    with pytest.raises(TransportFailure):
        send(connection, b"x" * (buffer.MAX_REQUEST_BYTES + 1))
    assert connection.seen is None


def test_the_transport_never_retries():
    connection = FakeConnection(error=OSError("x"))
    with pytest.raises(TransportFailure):
        send(connection)
    assert connection.requests == 1


# --- the two start gates `pros review` checks (formerly covered through review-serve) --------


@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, False), ("", False), ("true", False), ("0", False), ("False", False), ("false", True)],
)
def test_publishing_is_enabled_only_by_the_exact_word_false(monkeypatch, value, enabled):
    if value is None:
        monkeypatch.delenv("PUBLISHING_DISABLED", raising=False)
    else:
        monkeypatch.setenv("PUBLISHING_DISABLED", value)
    assert buffer.publishing_enabled() is enabled


@pytest.mark.parametrize("value", [None, "", " key", "key ", "a key", "kéy"])
def test_a_missing_or_malformed_key_is_refused_without_echoing_it(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PROS_BUFFER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("PROS_BUFFER_API_KEY", value)
    with pytest.raises(PublicationError) as failed:
        buffer.load_api_key()
    assert failed.value.code == E.API_KEY_MISSING
    assert not value or value.strip() not in str(failed.value)


def test_a_well_formed_key_is_returned_as_a_secret(monkeypatch):
    monkeypatch.setenv("PROS_BUFFER_API_KEY", "test-key-123")
    key = buffer.load_api_key()
    assert isinstance(key, SecretStr) and key.get_secret_value() == "test-key-123"
    assert "test-key-123" not in repr(key)
