"""Strict parsing of the three editorial JSON documents: contribution, response and brief.

One parser path only, as in the evidence checker: capped bytes, then `loads_strict` (duplicate
keys, NaN/Infinity, non-UTF-8, deep nesting and lone surrogates rejected), then the strict
frozen model. Schema problems are reported as `location: constraint` details built from known
field names and indices only; rejected values and unknown keys are never echoed.
"""

from typing import Literal

from pydantic import BaseModel, ValidationError

from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.models import (
    AuthorContribution,
    Brief,
    BriefLimits,
    BriefSource,
    DraftResponse,
    Generator,
    OpenQuestion,
    Segment,
    SnapshotIdentity,
    SourceRef,
)
from public_research_os.evidence.report import ErrorCode
from public_research_os.evidence.strict_json import StrictJsonError, loads_strict
from public_research_os.evidence.validation import UnreadableInput

CONTRIBUTION_CAP_BYTES = 64 * 1024
RESPONSE_CAP_BYTES = 512 * 1024
MAX_DETAILS = 10

_KNOWN_FIELDS = frozenset().union(
    *(
        model.model_fields
        for model in (
            AuthorContribution,
            DraftResponse,
            Segment,
            SourceRef,
            OpenQuestion,
            Generator,
            Brief,
            SnapshotIdentity,
            BriefSource,
            BriefLimits,
        )
    )
)

_CODES = {
    "contribution": (
        EditorialErrorCode.CONTRIBUTION_UNREADABLE,
        EditorialErrorCode.CONTRIBUTION_TOO_LARGE,
        EditorialErrorCode.CONTRIBUTION_INVALID_JSON,
        EditorialErrorCode.CONTRIBUTION_INVALID,
    ),
    "response": (
        EditorialErrorCode.RESPONSE_UNREADABLE,
        EditorialErrorCode.RESPONSE_TOO_LARGE,
        EditorialErrorCode.RESPONSE_INVALID_JSON,
        EditorialErrorCode.RESPONSE_INVALID,
    ),
}


def parse_contribution(data: bytes | UnreadableInput) -> AuthorContribution:
    return _parse(AuthorContribution, data, "contribution")


def parse_response(data: bytes | UnreadableInput) -> DraftResponse:
    return _parse(DraftResponse, data, "response")


def parse_brief(data: bytes) -> Brief:
    """brief.json bytes from a verified brief bundle. Any problem means the bundle is unusable."""
    try:
        return Brief.model_validate(loads_strict(data))
    except (StrictJsonError, ValidationError):
        raise EditorialError(EditorialErrorCode.BRIEF_INCOMPLETE) from None


def _parse[M: BaseModel](
    model: type[M], data: bytes | UnreadableInput, kind: Literal["contribution", "response"]
) -> M:
    unreadable, too_large, invalid_json, invalid = _CODES[kind]
    if isinstance(data, UnreadableInput):
        raise EditorialError(too_large if data.code == ErrorCode.INPUT_TOO_LARGE else unreadable)
    try:
        value = loads_strict(data)
    except StrictJsonError:
        raise EditorialError(invalid_json) from None
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise EditorialError(invalid, sanitized_details(exc)) from None


def sanitized_details(exc: ValidationError) -> tuple[str, ...]:
    details = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        parts = list(error["loc"])
        if error["type"] == "extra_forbidden" and parts:
            parts[-1] = "<extra>"  # an unknown key is input; never echo it
        location = ".".join(
            str(part) for part in parts if isinstance(part, int) or part in _KNOWN_FIELDS
        )
        if error["type"] == "extra_forbidden":
            location = f"{location}.<extra>" if location else "<extra>"
        message = "unexpected field" if error["type"] == "extra_forbidden" else error["msg"]
        details.append(f"{location or '<root>'}: {message[:200]}")
    if len(details) > MAX_DETAILS:
        details = [*details[:MAX_DETAILS], f"... and {len(details) - MAX_DETAILS} more"]
    return tuple(details)
