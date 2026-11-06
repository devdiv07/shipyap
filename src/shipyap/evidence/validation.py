"""Deterministic claim-vs-manifest checks. Pure: filesystem access is injected.

Fail closed: anything not positively matched is MISSING_SOURCE or SOURCE_MISMATCH, and
semantic support is always UNKNOWN. A matching SHA-256 is byte equality with a local
snapshot, never scientific truth or human approval.
"""

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from shipyap.evidence.manifest import ManifestFile, SourceManifest
from shipyap.evidence.models import (
    Claim,
    ClaimStatus,
    CountMetric,
    EvidenceSource,
)
from shipyap.evidence.policy import (
    PATH_MESSAGES,
    URL_MESSAGES,
    check_relative_path,
    check_source_url,
)
from shipyap.evidence.report import (
    DIAGNOSTICS,
    ClaimRecord,
    ErrorCode,
    EvidenceLabel,
    GateResult,
    InputError,
    Outcome,
    SourceCheck,
    SourceReason,
    ValidationReport,
    build_claim_record,
)
from shipyap.evidence.strict_json import JsonViolation, StrictJsonError, loads_strict

InputName = Literal["claim", "manifest"]


@dataclass(frozen=True)
class UnreadableInput:
    """Stands in for input bytes that could not be read."""

    code: Literal[ErrorCode.INPUT_UNREADABLE, ErrorCode.INPUT_TOO_LARGE]


@dataclass(frozen=True)
class DigestObservation:
    """Result of hashing one manifest-listed file; sha256 is set only on success."""

    sha256: str | None
    failure: SourceReason | None


DigestObserver = Callable[[str], DigestObservation]


@dataclass(frozen=True)
class EvidenceCheck:
    report: ValidationReport
    record: ClaimRecord | None


@dataclass(frozen=True)
class SourceDecision:
    """Pure pre-I/O decision for one claim source; `expected` set when a digest is needed."""

    source: EvidenceSource
    reason: SourceReason | None
    expected: ManifestFile | None


_KNOWN_FIELDS = frozenset().union(
    *(
        model.model_fields
        for model in (Claim, EvidenceSource, CountMetric, SourceManifest, ManifestFile)
    )
)

_READ_MESSAGES = {
    ErrorCode.INPUT_UNREADABLE: "input file could not be read as a regular file",
    ErrorCode.INPUT_TOO_LARGE: "input file exceeds the size limit",
}

_JSON_MESSAGES = {
    JsonViolation.MALFORMED: "input is not valid UTF-8 JSON",
    JsonViolation.NON_STANDARD_NUMBER: "NaN, Infinity and non-finite numbers are not allowed",
    JsonViolation.TOO_DEEP: "input JSON is nested too deeply",
}


def check_evidence(
    claim_input: bytes | UnreadableInput,
    manifest_input: bytes | UnreadableInput,
    observe: DigestObserver | None,
) -> EvidenceCheck:
    """Validate a claim against a v1 manifest. `observe=None` means no local snapshot."""
    errors: list[InputError] = []
    claim = _parse(Claim, "claim", claim_input, errors)
    manifest = _parse(SourceManifest, "manifest", manifest_input, errors)
    if claim is not None:
        errors += _claim_policy_errors(claim)
    if claim is not None and manifest is not None and claim.workspace_id != manifest.workspace_id:
        errors.append(
            InputError(
                input="manifest",
                code=ErrorCode.WORKSPACE_MISMATCH,
                location="workspace_id",
                message="claim and manifest belong to different workspaces",
            )
        )

    sources: list[SourceCheck] = []
    if not errors and claim is not None and manifest is not None:
        # Only reached for a single, consistent workspace: no file I/O happens otherwise.
        sources = _observe_sources(plan_source_checks(claim, manifest), observe)

    labels: set[EvidenceLabel] = {EvidenceLabel.UNKNOWN_SEMANTIC_SUPPORT}
    labels.update(source.label for source in sources)
    if not errors:
        labels.add(EvidenceLabel.VALID_STRUCTURE)
    if claim is not None and claim.status != ClaimStatus.APPROVED:
        labels.add(EvidenceLabel.NOT_APPROVED)

    gate_passes = (
        not errors
        and claim is not None
        and claim.status == ClaimStatus.APPROVED
        and bool(sources)
        and all(source.label == EvidenceLabel.SOURCE_MATCH for source in sources)
    )
    report = ValidationReport(
        outcome=Outcome.REJECTED if errors else Outcome.CHECKED,
        claim_id=claim.claim_id if claim is not None else None,
        workspace_id=claim.workspace_id if claim is not None else None,
        claim_status=claim.status if claim is not None else None,
        claim_sha256=_input_digest(claim_input),
        manifest_sha256=_input_digest(manifest_input),
        labels=tuple(label for label in EvidenceLabel if label in labels),
        mechanical_gate=GateResult.PASS if gate_passes else GateResult.FAIL,
        sources=tuple(sources),
        errors=tuple(errors),
    )
    record = None
    if report.outcome == Outcome.CHECKED and claim is not None:
        record = build_claim_record(claim, report)
    return EvidenceCheck(report=report, record=record)


def plan_source_checks(claim: Claim, manifest: SourceManifest) -> list[SourceDecision]:
    """Decide everything that does not need file bytes, in claim source order."""
    decisions = []
    for source in claim.sources:
        expected = None
        if str(source.url) != str(manifest.source_url):
            reason = SourceReason.UNSUPPORTED_REPOSITORY
        elif source.revision_sha.lower() != manifest.revision_sha:
            reason = SourceReason.REVISION_MISMATCH
        else:
            expected = manifest.file_for(source.path)
            reason = SourceReason.PATH_NOT_IN_MANIFEST if expected is None else None
        decisions.append(SourceDecision(source=source, reason=reason, expected=expected))
    return decisions


def _observe_sources(
    decisions: list[SourceDecision], observe: DigestObserver | None
) -> list[SourceCheck]:
    observations: dict[str, DigestObservation] = {}
    checks = []
    for decision in decisions:
        reason, observed = decision.reason, None
        if decision.expected is not None:
            if observe is None:
                reason = SourceReason.NO_SOURCE_ROOT
            else:
                path = decision.expected.path
                if path not in observations:
                    observations[path] = observe(path)
                observation = observations[path]
                if observation.sha256 is None:
                    reason = observation.failure or SourceReason.SOURCE_UNREADABLE
                else:
                    observed = observation.sha256
                    matches = hmac.compare_digest(observed, decision.expected.sha256)
                    reason = SourceReason.DIGEST_MATCH if matches else SourceReason.DIGEST_MISMATCH
        if reason is None:  # unreachable: decisions without an expected file carry a reason
            raise RuntimeError("source decision without a reason")
        checks.append(
            SourceCheck(
                url=str(decision.source.url),
                revision_sha=decision.source.revision_sha,
                path=decision.source.path,
                label=_label_for(reason),
                reason=reason,
                diagnostic=DIAGNOSTICS[reason],
                expected_sha256=decision.expected.sha256 if decision.expected else None,
                observed_sha256=observed,
            )
        )
    return checks


def _label_for(reason: SourceReason) -> EvidenceLabel:
    if reason == SourceReason.DIGEST_MATCH:
        return EvidenceLabel.SOURCE_MATCH
    if reason in {SourceReason.DIGEST_MISMATCH, SourceReason.REVISION_MISMATCH}:
        return EvidenceLabel.SOURCE_MISMATCH
    return EvidenceLabel.MISSING_SOURCE


def _claim_policy_errors(claim: Claim) -> list[InputError]:
    """Apply the manifest's URL/path policies to claim sources too; never echo values."""
    errors = []
    for index, source in enumerate(claim.sources):
        url_violation = check_source_url(source.url)
        if url_violation is not None:
            errors.append(
                InputError(
                    input="claim",
                    code=ErrorCode.URL_POLICY,
                    location=f"sources.{index}.url",
                    message=URL_MESSAGES[url_violation],
                )
            )
        path_violation = check_relative_path(source.path)
        if path_violation is not None:
            errors.append(
                InputError(
                    input="claim",
                    code=ErrorCode.PATH_POLICY,
                    location=f"sources.{index}.path",
                    message=PATH_MESSAGES[path_violation],
                )
            )
    return errors


def _parse[M: BaseModel](
    model: type[M],
    name: InputName,
    data: bytes | UnreadableInput,
    errors: list[InputError],
) -> M | None:
    if isinstance(data, UnreadableInput):
        errors.append(
            InputError(input=name, code=data.code, location="", message=_READ_MESSAGES[data.code])
        )
        return None
    try:
        # One parser only: strict stdlib decoding (which also rejects lone surrogates in every
        # key and string), then strict Pydantic validation of the result.
        return model.model_validate(loads_strict(data))
    except StrictJsonError as exc:
        errors.append(_json_policy_error(name, exc))
        return None
    except ValidationError as exc:
        errors += _sanitized_errors(name, exc)
        return None


def _json_policy_error(name: InputName, exc: StrictJsonError) -> InputError:
    if exc.violation == JsonViolation.DUPLICATE_KEY:
        key = exc.key if exc.key in _KNOWN_FIELDS else "<extra>"
        return InputError(
            input=name,
            code=ErrorCode.DUPLICATE_JSON_KEY,
            location=key,
            message="duplicate object key; repeated keys are ambiguous and rejected",
        )
    if exc.violation == JsonViolation.INVALID_UNICODE:
        return InputError(
            input=name,
            code=ErrorCode.INVALID_UNICODE,
            location="<document>",
            message="input contains a lone Unicode surrogate code point",
        )
    return InputError(
        input=name,
        code=ErrorCode.INVALID_JSON,
        location="<document>",
        message=_JSON_MESSAGES[exc.violation],
    )


def _sanitized_errors(name: InputName, exc: ValidationError) -> list[InputError]:
    """Pydantic errors without input values, URLs or context; unknown keys are masked."""
    sanitized = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(
            str(part) if isinstance(part, int) or part in _KNOWN_FIELDS else "<extra>"
            for part in error["loc"]
        )
        if error["type"] == "extra_forbidden":
            code, message = ErrorCode.INVALID_SHAPE, "unexpected field"
        else:
            code, message = ErrorCode.INVALID_SHAPE, error["msg"][:200]
        sanitized.append(
            InputError(input=name, code=code, location=location or "<root>", message=message)
        )
    return sanitized


def _input_digest(data: bytes | UnreadableInput) -> str | None:
    if isinstance(data, UnreadableInput):
        return None
    return hashlib.sha256(data).hexdigest()
