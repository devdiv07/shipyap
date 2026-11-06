"""The provider adapter against the REAL anthropic SDK, with an httpx2 mock transport in place of
the network (no socket is opened; the conftest guard stays on).

Checks the exact request shape for the token count and the generation, that one call is one HTTP
request whatever the status, that the credential comes only from PROS_ANTHROPIC_API_KEY, and that
no exception text, response body or key survives into an error.
"""

import anthropic
import httpx2
import pytest
from drafting_fake import COUNTED, KEY, MODEL, REQUEST_ID, USAGE, FakeApi, message_body
from editorial_fake import SENTINEL
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from pydantic import SecretStr

from public_research_os.drafting import provider
from public_research_os.drafting.errors import DraftingError, DraftingErrorCode, Phase
from public_research_os.drafting.models import ModelRequest, Usage
from public_research_os.drafting.policy import EFFORT, MAX_OUTPUT_TOKENS
from public_research_os.drafting.prompt import MODEL_OUTPUT_SCHEMA, SYSTEM_PROMPT

REQUEST = ModelRequest(
    model=MODEL,
    system_prompt=SYSTEM_PROMPT,
    user_text=f"# Drafting brief\n\nsource text {SENTINEL}\n",
    output_schema=MODEL_OUTPUT_SCHEMA,
    max_tokens=MAX_OUTPUT_TOKENS,
    effort=EFFORT,
)
SDK_ENVIRONMENT = (
    "PROS_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_LOG",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in SDK_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def drafter(api: FakeApi) -> provider.AnthropicDrafter:
    return provider.AnthropicDrafter(SecretStr(KEY), transport=api.transport)


def test_count_and_generation_send_the_same_inputs_to_the_fixed_endpoint():
    api = FakeApi()
    client = drafter(api)
    assert client.count_input_tokens(REQUEST) == COUNTED
    answer = client.generate(REQUEST)

    assert api.paths() == ["/v1/messages/count_tokens", "/v1/messages"]
    for request in api.requests:
        assert (request.method, request.url.scheme, request.url.host) == (
            "POST",
            "https",
            "api.anthropic.com",
        )
        assert request.headers["x-api-key"] == KEY
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers and "anthropic-beta" not in request.headers
    count_body, generation_body = api.bodies()
    assert generation_body == {
        "model": MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": REQUEST.user_text}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": MODEL_OUTPUT_SCHEMA},
        },
    }  # exactly this: no fallbacks, tools, thinking override, streaming or metadata
    assert count_body == {k: v for k, v in generation_body.items() if k != "max_tokens"}

    # Only the text block is kept; the empty thinking block is dropped.
    assert answer.texts == (message_body()["content"][1]["text"],)
    assert (answer.model, answer.request_id, answer.stop_reason) == (MODEL, REQUEST_ID, "end_turn")
    assert answer.usage == Usage(*USAGE)


def test_credential_and_endpoint_environment_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"other-key-{SENTINEL}")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", f"other-token-{SENTINEL}")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example")
    monkeypatch.setenv("ANTHROPIC_PROFILE", "someone-else")
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    api = FakeApi()
    drafter(api).generate(REQUEST)
    (request,) = api.requests
    assert request.url.host == "api.anthropic.com"
    assert request.headers["x-api-key"] == KEY
    assert "authorization" not in request.headers
    assert SENTINEL not in str(request.headers)


def test_the_sdk_lets_custom_headers_replace_the_key_so_the_adapter_refuses_them(monkeypatch):
    """Why SDK_ENVIRONMENT_UNSAFE exists: anthropic 1.15.0 merges ANTHROPIC_CUSTOM_HEADERS over the
    API key header, even when the key is passed explicitly."""
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-api-key: a-different-key")
    raw = FakeApi()
    client = anthropic.Anthropic(
        api_key=KEY,
        base_url=provider.API_BASE_URL,
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=raw.transport),
    )
    client.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": "x"}])
    assert raw.requests[0].headers["x-api-key"] == "a-different-key"  # the SDK behaviour

    api = FakeApi()
    with pytest.raises(DraftingError) as caught:
        drafter(api)
    assert caught.value.code == DraftingErrorCode.SDK_ENVIRONMENT_UNSAFE
    assert caught.value.phase == Phase.PREFLIGHT and api.requests == []
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "  ")  # blank adds no header; allowed
    drafter(api).count_input_tokens(REQUEST)
    assert api.requests[0].headers["x-api-key"] == KEY


@pytest.mark.parametrize(
    "value", ["debug", "info", "off", " "], ids=["debug", "info", "off", "blank"]
)
def test_any_sdk_logging_setting_is_refused_before_any_request(monkeypatch, value):
    """At debug level anthropic 1.15.0 logs each request's options, the brief included, to stderr
    (test_drafting_cli proves it in a fresh process). Any non-empty ANTHROPIC_LOG is refused."""
    monkeypatch.setenv("ANTHROPIC_LOG", value)
    api = FakeApi()
    with pytest.raises(DraftingError) as caught:
        drafter(api)
    assert caught.value.code == DraftingErrorCode.SDK_LOGGING_ENABLED
    assert caught.value.phase == Phase.PREFLIGHT and api.requests == []
    assert value.strip() == "" or value not in str(caught.value)


def test_an_empty_sdk_logging_setting_is_not_refused(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_LOG", "")
    api = FakeApi()
    client = drafter(api)
    client.count_input_tokens(REQUEST)
    client.generate(REQUEST)
    assert api.paths() == ["/v1/messages/count_tokens", "/v1/messages"]


def test_sdk_tracing_and_trace_propagation_are_off_whatever_the_environment_says(monkeypatch):
    """By default anthropic 1.15.0 sends the caller's active trace context (`traceparent`) to the
    provider and lets the environment switch spans to recording request content."""
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY", "true")
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x0AF7651916CD43DD8448EB211C80319C,
            span_id=0xB7AD6B7169203331,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    raw = FakeApi()
    default = anthropic.Anthropic(
        api_key=KEY,
        base_url=provider.API_BASE_URL,
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=raw.transport),
    )
    with trace.use_span(span):
        default.messages.count_tokens(model=MODEL, messages=[{"role": "user", "content": "x"}])
    assert "traceparent" in raw.requests[0].headers  # the SDK default this test guards against

    api = FakeApi()
    client = drafter(api)
    with trace.use_span(span):
        client.count_input_tokens(REQUEST)
        client.generate(REQUEST)
    assert len(api.requests) == 2
    assert not any("traceparent" in request.headers for request in api.requests)
    resolved = client._client.open_telemetry
    assert resolved["traces"]["enabled"] is False and resolved["propagation"] is False


@pytest.mark.parametrize("value", [None, "", " ", f" {KEY}", f"{KEY}\n", "kéy", "a\tb"])
def test_a_missing_or_unusable_key_is_refused(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("PROS_ANTHROPIC_API_KEY", value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)  # never a fallback
    with pytest.raises(DraftingError) as caught:
        provider.load_api_key()
    assert caught.value.code == DraftingErrorCode.API_KEY_MISSING
    assert KEY not in str(caught.value)


def test_the_key_is_read_from_its_own_variable_and_never_shown(monkeypatch):
    monkeypatch.setenv("PROS_ANTHROPIC_API_KEY", KEY)
    secret = provider.load_api_key()
    assert secret.get_secret_value() == KEY
    assert KEY not in repr(secret) and KEY not in str(secret)


STATUS_CODES = [
    (301, DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE),
    (302, DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE),
    (400, DraftingErrorCode.PROVIDER_REQUEST_REJECTED),
    (401, DraftingErrorCode.PROVIDER_AUTH_FAILED),
    (403, DraftingErrorCode.PROVIDER_PERMISSION_DENIED),
    (404, DraftingErrorCode.PROVIDER_NOT_FOUND),
    (409, DraftingErrorCode.PROVIDER_REQUEST_REJECTED),
    (413, DraftingErrorCode.PROVIDER_REQUEST_TOO_LARGE),
    (422, DraftingErrorCode.PROVIDER_REQUEST_REJECTED),
    (429, DraftingErrorCode.PROVIDER_RATE_LIMITED),
    (500, DraftingErrorCode.PROVIDER_UNAVAILABLE),
    (503, DraftingErrorCode.PROVIDER_UNAVAILABLE),
    (529, DraftingErrorCode.PROVIDER_UNAVAILABLE),
]
HOSTILE_HEADERS = {
    "retry-after": "7",
    "x-should-retry": "true",  # the SDK would retry on this; max_retries=0 must still win
    "location": "https://elsewhere.example/steal",  # never followed
}


@pytest.mark.parametrize("status,expected", STATUS_CODES)
def test_generation_errors_are_fixed_codes_sent_once_never_retried_or_echoed(status, expected):
    api = FakeApi(status=status, headers=HOSTILE_HEADERS)
    with pytest.raises(DraftingError) as caught:
        drafter(api).generate(REQUEST)
    error = caught.value
    assert len(api.requests) == 1 and api.requests[0].url.host == "api.anthropic.com"
    assert (error.code, error.phase, error.request_id) == (expected, Phase.GENERATE, REQUEST_ID)
    retried = expected in {
        DraftingErrorCode.PROVIDER_RATE_LIMITED,
        DraftingErrorCode.PROVIDER_UNAVAILABLE,
    }
    assert error.retry_after_seconds == (7 if retried else None)
    assert SENTINEL not in str(error) and KEY not in str(error)
    assert error.__cause__ is None and error.__suppress_context__


@pytest.mark.parametrize("status,expected", STATUS_CODES)
def test_token_count_errors_are_fixed_codes_in_the_count_phase(status, expected):
    api = FakeApi(count_status=status, headers=HOSTILE_HEADERS)
    with pytest.raises(DraftingError) as caught:
        drafter(api).count_input_tokens(REQUEST)
    assert (caught.value.code, caught.value.phase) == (expected, Phase.COUNT)
    assert api.paths() == ["/v1/messages/count_tokens"]


@pytest.mark.parametrize(
    "failure,expected",
    [
        (httpx2.ReadTimeout, DraftingErrorCode.PROVIDER_TIMEOUT),
        (httpx2.ConnectTimeout, DraftingErrorCode.PROVIDER_TIMEOUT),
        (httpx2.ConnectError, DraftingErrorCode.PROVIDER_NETWORK_ERROR),
        (httpx2.RemoteProtocolError, DraftingErrorCode.PROVIDER_NETWORK_ERROR),
    ],
)
def test_timeouts_and_transport_failures_are_not_retried(failure, expected):
    for path, call, phase in (
        ("/v1/messages/count_tokens", "count_input_tokens", Phase.COUNT),
        ("/v1/messages", "generate", Phase.GENERATE),
    ):
        api = FakeApi(raise_on={path: failure(f"{SENTINEL}")})
        with pytest.raises(DraftingError) as caught:
            getattr(drafter(api), call)(REQUEST)
        assert (caught.value.code, caught.value.phase) == (expected, phase)
        assert len(api.requests) == 1 and SENTINEL not in str(caught.value)


@pytest.mark.parametrize(
    "message",
    [
        {"unexpected": True},
        {**message_body(), "content": "not a list"},
        {**message_body(), "content": [{"type": "text", "text": 7}]},
        {**message_body(), "usage": {"input_tokens": -1, "output_tokens": 3}},
        {**message_body(), "usage": {"input_tokens": "12", "output_tokens": 3}},
        {**message_body(), "model": 5},
    ],
)
def test_a_malformed_success_body_is_an_unexpected_response(message):
    api = FakeApi(message)
    with pytest.raises(DraftingError) as caught:
        drafter(api).generate(REQUEST)
    assert caught.value.code == DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE
    assert caught.value.phase == Phase.GENERATE


def test_a_non_json_success_body_is_an_unexpected_response():
    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=f"<html>{SENTINEL}</html>".encode())

    client = provider.AnthropicDrafter(SecretStr(KEY), transport=httpx2.MockTransport(handle))
    for call in (client.count_input_tokens, client.generate):
        with pytest.raises(DraftingError) as caught:
            call(REQUEST)
        assert caught.value.code == DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE
        assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize("counted", [0, -5, "12", 1.5, None, True])
def test_an_implausible_token_count_is_an_unexpected_response(counted):
    with pytest.raises(DraftingError) as caught:
        drafter(FakeApi(counted=counted)).count_input_tokens(REQUEST)
    assert caught.value.code == DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE


@pytest.mark.parametrize("request_id", ["not-a-request-id", f"req_{SENTINEL}!", "req_" + "a" * 65])
def test_only_a_well_formed_request_id_is_kept(request_id):
    answer = drafter(FakeApi(headers={"request-id": request_id})).generate(REQUEST)
    assert answer.request_id is None
