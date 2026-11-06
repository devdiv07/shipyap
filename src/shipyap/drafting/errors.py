"""Fixed error codes for `draft-model`.

As in ingestion and editorial, a failure is a code plus a fixed message. Nothing from the API key,
the brief, the provider's response body or error text, a local path or an OS error is put into an
error. What may accompany a code is provider metadata that cannot carry content: a request id that
matched a strict pattern, token counts, a retry-after number of seconds and cost estimates.

Every error also records how far the run got (`Phase`), because that decides what may have been
billed: nothing before the token count, nothing for a token count (it does not generate), and
possibly the whole generation request once it was sent.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from shipyap.drafting.models import Usage
from shipyap.drafting.policy import Estimate
from shipyap.editorial.errors import EditorialError


class Phase(StrEnum):
    PREFLIGHT = "PREFLIGHT"  # no request was sent
    COUNT = "COUNT"  # at most the token-counting request was sent; it generates nothing
    GENERATE = "GENERATE"  # the generation request was sent and may have been billed


class DraftingErrorCode(StrEnum):
    # Refused before any request.
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    BUDGET_INVALID = "BUDGET_INVALID"
    BRIEF_TOO_LARGE_FOR_MODEL = "BRIEF_TOO_LARGE_FOR_MODEL"
    API_KEY_MISSING = "API_KEY_MISSING"
    SDK_ENVIRONMENT_UNSAFE = "SDK_ENVIRONMENT_UNSAFE"
    SDK_LOGGING_ENABLED = "SDK_LOGGING_ENABLED"
    # Refused after the token count, before generation.
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    # The provider refused or failed (token count or generation).
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_PERMISSION_DENIED = "PROVIDER_PERMISSION_DENIED"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    PROVIDER_REQUEST_REJECTED = "PROVIDER_REQUEST_REJECTED"
    PROVIDER_REQUEST_TOO_LARGE = "PROVIDER_REQUEST_TOO_LARGE"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_NETWORK_ERROR = "PROVIDER_NETWORK_ERROR"
    PROVIDER_UNEXPECTED_RESPONSE = "PROVIDER_UNEXPECTED_RESPONSE"
    # The generation reply cannot be a draft response.
    MODEL_REFUSED = "MODEL_REFUSED"
    MODEL_TRUNCATED = "MODEL_TRUNCATED"
    MODEL_REPLY_MALFORMED = "MODEL_REPLY_MALFORMED"


MESSAGES: dict[DraftingErrorCode, str] = {
    DraftingErrorCode.MODEL_UNKNOWN: "--model is not an allowed drafting model",
    DraftingErrorCode.BUDGET_INVALID: (
        "--max-cost-usd must be a positive US dollar amount with at most two decimals"
    ),
    DraftingErrorCode.BRIEF_TOO_LARGE_FOR_MODEL: "brief.md exceeds the size limit for a model run",
    DraftingErrorCode.API_KEY_MISSING: (
        "PROS_ANTHROPIC_API_KEY is not set to a usable key in this environment"
    ),
    DraftingErrorCode.SDK_ENVIRONMENT_UNSAFE: (
        "ANTHROPIC_CUSTOM_HEADERS is set; it can replace the API key header, so unset it"
    ),
    DraftingErrorCode.SDK_LOGGING_ENABLED: (
        "ANTHROPIC_LOG is set; SDK logging can print the brief to stderr, so unset it"
    ),
    DraftingErrorCode.BUDGET_EXCEEDED: (
        "the estimated maximum cost exceeds --max-cost-usd; generation was not requested"
    ),
    DraftingErrorCode.PROVIDER_AUTH_FAILED: "the provider rejected the API key",
    DraftingErrorCode.PROVIDER_PERMISSION_DENIED: (
        "the API key is not permitted to use this model or endpoint"
    ),
    DraftingErrorCode.PROVIDER_NOT_FOUND: "the provider does not know this model or endpoint",
    DraftingErrorCode.PROVIDER_REQUEST_REJECTED: (
        "the provider rejected the request (for example its format, credit or spend limits)"
    ),
    DraftingErrorCode.PROVIDER_REQUEST_TOO_LARGE: "the provider rejected the request as too large",
    DraftingErrorCode.PROVIDER_RATE_LIMITED: "the provider rate-limited the request",
    DraftingErrorCode.PROVIDER_UNAVAILABLE: "the provider reported a server error or overload",
    DraftingErrorCode.PROVIDER_TIMEOUT: "the provider did not answer in time",
    DraftingErrorCode.PROVIDER_NETWORK_ERROR: "the provider could not be reached",
    DraftingErrorCode.PROVIDER_UNEXPECTED_RESPONSE: (
        "the provider's answer was not in the expected form"
    ),
    DraftingErrorCode.MODEL_REFUSED: "the model declined the request",
    DraftingErrorCode.MODEL_TRUNCATED: (
        "the model reply was cut off at the output token limit and is incomplete"
    ),
    DraftingErrorCode.MODEL_REPLY_MALFORMED: (
        "the model reply did not consist of exactly one complete text answer"
    ),
}


@dataclass(frozen=True)
class CallReport:
    """What the provider reported about the generation request. Metadata only, never content."""

    model: str
    request_id: str | None
    usage: Usage
    estimated_charge_usd: Decimal


class DraftingError(Exception):
    """Code, fixed message, the phase reached and safe provider metadata. Never content."""

    def __init__(
        self,
        code: DraftingErrorCode,
        *,
        phase: Phase = Phase.PREFLIGHT,
        request_id: str | None = None,
        retry_after_seconds: int | None = None,
        estimate: Estimate | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {MESSAGES[code]}")
        self.code = code
        self.phase = phase
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        self.estimate = estimate
        self.error_type = error_type  # one of provider.PROVIDER_ERROR_TYPES, or None

    def detail(self) -> str:
        """Phase, provider error type and request id, for a run note; empty when unknown."""
        parts = [f"phase {self.phase}"]
        if self.error_type:
            parts.append(f"provider type {self.error_type}")
        if self.request_id:
            parts.append(f"request id {self.request_id}")
        return f" ({', '.join(parts)})" if self.error_type or self.request_id else ""


class GenerationRejected(Exception):
    """A generation request was made, but its reply did not become a published bundle.

    `cause` is the fixed-code error that stopped it (a refusal, truncation, malformed or invalid
    reply, or a failed publication); `call` is what was reported about the billed request.
    """

    def __init__(self, cause: DraftingError | EditorialError, call: CallReport) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.call = call
