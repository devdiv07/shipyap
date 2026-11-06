"""The ONLY module that uses the Anthropic SDK or reads the environment (tests enforce both).

The client is configured explicitly (behaviour verified against anthropic 1.15.0):
- The API key is passed in, so the SDK consults no credential environment variable, profile,
  `ant auth` login or workload-identity setting. Claude Code logins and subscriptions are never
  used: `PROS_ANTHROPIC_API_KEY` is the only credential source.
- `base_url` is fixed, so `ANTHROPIC_BASE_URL` is ignored.
- `max_retries=0`: one call is one HTTP request. Nothing is retried, including a timed-out
  generation, which may already have been processed and billed.
- Explicit timeouts; no streaming.
- Redirects are not followed, so the key header is never re-sent to a redirect target.
- The SDK's OpenTelemetry tracing and trace propagation are off, whatever the environment says.
- No server-side refusal fallback (a beta parameter this module never sends).

Refused before any request, when the adapter is constructed:
- a non-blank `ANTHROPIC_CUSTOM_HEADERS`. The SDK always reads it, and its headers override the
  API key header, so it could silently bill a different key;
- any non-empty `ANTHROPIC_LOG`. The SDK reads it at import and then logs to stderr; at debug
  level each request's options, which contain the brief, are logged as the request is built.
  Importing the SDK logs nothing, so refusing here precedes every request and every log line.

Not controlled here (the operator's environment; see docs/security/threat-model.md):
- proxy variables (`HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`), which the SDK's default HTTP client
  honours, so an operator-configured proxy can route the request;
- certificate trust, which comes from the operating system's trust store.

Errors: every SDK exception becomes a fixed `DraftingError`. Exception text, response bodies and
headers are dropped; only a pattern-checked request id, a numeric retry-after and the
provider's error type, when it is one of the fixed `PROVIDER_ERROR_TYPES`, survive.
"""

import os
from typing import Any

import anthropic
from pydantic import SecretStr

from public_research_os.drafting.errors import DraftingError, DraftingErrorCode, Phase
from public_research_os.drafting.models import ModelReply, ModelRequest, Usage
from public_research_os.drafting.prompt import safe_request_id

API_KEY_ENV = "PROS_ANTHROPIC_API_KEY"
UNSAFE_SDK_ENV = "ANTHROPIC_CUSTOM_HEADERS"
SDK_LOG_ENV = "ANTHROPIC_LOG"
API_BASE_URL = "https://api.anthropic.com"
# Non-streaming: the reply arrives only when generation ends, so `read` bounds the generation.
TIMEOUT = anthropic.Timeout(timeout=300.0, connect=10.0, write=30.0, pool=10.0)
MAX_RETRY_AFTER_SECONDS = 86_400

_STATUS_CODES = {
    401: DraftingErrorCode.PROVIDER_AUTH_FAILED,
    403: DraftingErrorCode.PROVIDER_PERMISSION_DENIED,
    404: DraftingErrorCode.PROVIDER_NOT_FOUND,
    413: DraftingErrorCode.PROVIDER_REQUEST_TOO_LARGE,
    429: DraftingErrorCode.PROVIDER_RATE_LIMITED,
}
# The provider's documented error types. Only one of these exact values is kept from a body.
PROVIDER_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "billing_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
        "rate_limit_error",
        "api_error",
        "overloaded_error",
        "timeout_error",
    }
)
_RETRY_HINTED = frozenset(
    {DraftingErrorCode.PROVIDER_RATE_LIMITED, DraftingErrorCode.PROVIDER_UNAVAILABLE}
)


def load_api_key() -> SecretStr:
    """The key from `PROS_ANTHROPIC_API_KEY`, held so that it is not shown by repr or str."""
    value = _environment(API_KEY_ENV)
    if (
        value is None
        or not value
        or value != value.strip()
        or not value.isascii()
        or not value.isprintable()
    ):
        raise DraftingError(DraftingErrorCode.API_KEY_MISSING)
    return SecretStr(value)


class AnthropicDrafter:
    """`Drafter` over the Anthropic Messages API. `transport` exists for tests only: an httpx2
    mock transport, so the real SDK runs without a socket."""

    def __init__(self, api_key: SecretStr, *, transport: Any = None) -> None:
        custom_headers = _environment(UNSAFE_SDK_ENV)
        if custom_headers is not None and custom_headers.strip():
            raise DraftingError(DraftingErrorCode.SDK_ENVIRONMENT_UNSAFE)
        if _environment(SDK_LOG_ENV):  # the value is never echoed
            raise DraftingError(DraftingErrorCode.SDK_LOGGING_ENABLED)
        http_options: dict[str, Any] = {"follow_redirects": False}
        if transport is not None:
            http_options["transport"] = transport
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            base_url=API_BASE_URL,
            max_retries=0,
            timeout=TIMEOUT,
            http_client=anthropic.DefaultHttpxClient(**http_options),
            open_telemetry={"traces": False, "propagation": False},
        )

    def count_input_tokens(self, request: ModelRequest) -> int:
        try:
            counted = self._client.messages.count_tokens(**_params(request))
        except anthropic.AnthropicError as exc:
            raise _provider_error(exc, Phase.COUNT) from None
        tokens = getattr(counted, "input_tokens", None)
        if type(tokens) is not int or tokens < 1:
            raise DraftingError(DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE, phase=Phase.COUNT)
        return tokens

    def generate(self, request: ModelRequest) -> ModelReply:
        try:
            message = self._client.messages.create(
                max_tokens=request.max_tokens, **_params(request)
            )
        except anthropic.AnthropicError as exc:
            raise _provider_error(exc, Phase.GENERATE) from None
        return _reply(message)


def _environment(name: str) -> str | None:
    return os.environ.get(name)  # the only environment read in drafting/ (a test enforces it)


def _params(request: ModelRequest) -> dict[str, Any]:
    """The inputs shared by the token count and the generation, so the count measures exactly
    what generation will send (generation adds only `max_tokens`)."""
    return {
        "model": request.model,
        "system": request.system_prompt,
        "messages": [{"role": "user", "content": request.user_text}],
        "output_config": {
            "effort": request.effort,
            "format": {"type": "json_schema", "schema": request.output_schema},
        },
    }


def _reply(message: Any) -> ModelReply:
    """The SDK does not validate response bodies strictly, so every field is checked here."""
    request_id = safe_request_id(getattr(message, "_request_id", None))
    unexpected = DraftingError(
        DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE, phase=Phase.GENERATE, request_id=request_id
    )
    try:
        usage = Usage(
            input_tokens=_token_count(message.usage.input_tokens),
            output_tokens=_token_count(message.usage.output_tokens),
        )
        texts = tuple(block.text for block in message.content if block.type == "text")
        model, stop_reason = message.model, message.stop_reason
    except (AttributeError, TypeError, ValueError):
        raise unexpected from None
    if (
        not isinstance(model, str)
        or not (stop_reason is None or isinstance(stop_reason, str))
        or not all(isinstance(text, str) for text in texts)
    ):
        raise unexpected
    return ModelReply(
        model=model, request_id=request_id, stop_reason=stop_reason, texts=texts, usage=usage
    )


def _token_count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _provider_error(exc: anthropic.AnthropicError, phase: Phase) -> DraftingError:
    """A fixed code; the exception, its message and the response body are dropped."""
    if isinstance(exc, anthropic.APITimeoutError):  # before APIConnectionError: a subclass
        return DraftingError(DraftingErrorCode.PROVIDER_TIMEOUT, phase=phase)
    if isinstance(exc, anthropic.APIConnectionError):
        return DraftingError(DraftingErrorCode.PROVIDER_NETWORK_ERROR, phase=phase)
    if not isinstance(exc, anthropic.APIStatusError):
        return DraftingError(DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE, phase=phase)
    status = exc.status_code
    if status in _STATUS_CODES:
        code = _STATUS_CODES[status]
    elif status >= 500:
        code = DraftingErrorCode.PROVIDER_UNAVAILABLE
    elif status >= 400:
        code = DraftingErrorCode.PROVIDER_REQUEST_REJECTED
    else:  # an unfollowed redirect or other non-error status that is not a success
        code = DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE
    retry_after = _retry_after(exc.response.headers.get("retry-after"))
    return DraftingError(
        code,
        phase=phase,
        request_id=safe_request_id(exc.request_id),
        retry_after_seconds=retry_after if code in _RETRY_HINTED else None,
        error_type=_error_type(exc.body),
    )


def _error_type(body: object) -> str | None:
    """The body's `error.type` when it is a known type; nothing else of the body is read."""
    error = body.get("error") if isinstance(body, dict) else None
    value = error.get("type") if isinstance(error, dict) else None
    return value if isinstance(value, str) and value in PROVIDER_ERROR_TYPES else None


def _retry_after(value: object) -> int | None:
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()) or len(value) > 6:
        return None
    return min(int(value), MAX_RETRY_AFTER_SECONDS)
