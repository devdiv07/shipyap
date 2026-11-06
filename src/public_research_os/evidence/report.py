"""Deterministic evidence-check report and reviewable claim record (schema version 1).

Pure: no filesystem or network access. Reports carry no timestamps, local paths,
input file names or environment data, so identical inputs give byte-identical output.
"""

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from public_research_os.evidence.models import Claim, ClaimStatus


class EvidenceLabel(StrEnum):
    """Deliberately separate labels; there is no VERIFIED label."""

    VALID_STRUCTURE = "VALID_STRUCTURE"
    SOURCE_MATCH = "SOURCE_MATCH"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    MISSING_SOURCE = "MISSING_SOURCE"
    NOT_APPROVED = "NOT_APPROVED"
    UNKNOWN_SEMANTIC_SUPPORT = "UNKNOWN_SEMANTIC_SUPPORT"


class SourceReason(StrEnum):
    DIGEST_MATCH = "DIGEST_MATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    UNSUPPORTED_REPOSITORY = "UNSUPPORTED_REPOSITORY"
    PATH_NOT_IN_MANIFEST = "PATH_NOT_IN_MANIFEST"
    NO_SOURCE_ROOT = "NO_SOURCE_ROOT"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PATH_ESCAPE = "PATH_ESCAPE"
    NOT_A_REGULAR_FILE = "NOT_A_REGULAR_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    SOURCE_CLOSE_FAILED = "SOURCE_CLOSE_FAILED"


DIAGNOSTICS: dict[SourceReason, str] = {
    SourceReason.DIGEST_MATCH: "local file bytes match the manifest digest (byte equality only)",
    SourceReason.DIGEST_MISMATCH: "local file bytes differ from the manifest digest",
    SourceReason.REVISION_MISMATCH: "claim revision differs from the manifest's pinned revision",
    SourceReason.UNSUPPORTED_REPOSITORY: (
        "manifest v1 covers a single repository at one pinned revision; "
        "this source's repository URL is not covered"
    ),
    SourceReason.PATH_NOT_IN_MANIFEST: "claim path is not listed in the manifest (exact match)",
    SourceReason.NO_SOURCE_ROOT: "no local snapshot root was provided; digest not checked",
    SourceReason.FILE_NOT_FOUND: "manifest-listed file is absent from the local snapshot",
    SourceReason.PATH_ESCAPE: "resolved path is outside the authorized snapshot root",
    SourceReason.NOT_A_REGULAR_FILE: "snapshot entry is not a regular file",
    SourceReason.FILE_TOO_LARGE: "snapshot file exceeds the digest size limit",
    SourceReason.SOURCE_CHANGED: "snapshot file changed while it was being checked",
    SourceReason.SOURCE_UNREADABLE: "snapshot file could not be read",
    SourceReason.SOURCE_CLOSE_FAILED: (
        "snapshot file could not be closed cleanly after reading; digest not trusted"
    ),
}


class ErrorCode(StrEnum):
    INPUT_UNREADABLE = "INPUT_UNREADABLE"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    INVALID_JSON = "INVALID_JSON"
    DUPLICATE_JSON_KEY = "DUPLICATE_JSON_KEY"
    INVALID_UNICODE = "INVALID_UNICODE"
    INVALID_SHAPE = "INVALID_SHAPE"
    URL_POLICY = "URL_POLICY"
    PATH_POLICY = "PATH_POLICY"
    WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"


class Outcome(StrEnum):
    CHECKED = "CHECKED"
    REJECTED = "REJECTED"


class GateResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


NOTICES: tuple[str, ...] = (
    "A mechanical gate PASS means structure, provenance assertions and local file digests "
    "agree. It is not scientific verification, human approval or publication readiness.",
    "Semantic support is UNKNOWN pending human review.",
    "Source URLs were not fetched; the manifest is an assertion, not third-party verification.",
)


class FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceCheck(FrozenStrictModel):
    url: str
    revision_sha: str
    path: str
    label: Literal[
        EvidenceLabel.SOURCE_MATCH, EvidenceLabel.SOURCE_MISMATCH, EvidenceLabel.MISSING_SOURCE
    ]
    reason: SourceReason
    diagnostic: str
    expected_sha256: str | None
    observed_sha256: str | None


class InputError(FrozenStrictModel):
    input: Literal["claim", "manifest"]
    code: ErrorCode
    location: str
    message: str


class ValidationReport(FrozenStrictModel):
    report_schema_version: Literal["1"] = "1"
    outcome: Outcome
    claim_id: str | None
    workspace_id: str | None
    claim_status: ClaimStatus | None
    claim_sha256: str | None
    manifest_sha256: str | None
    labels: tuple[EvidenceLabel, ...]
    mechanical_gate: GateResult
    sources: tuple[SourceCheck, ...]
    errors: tuple[InputError, ...]
    notices: tuple[str, ...] = NOTICES

    @model_validator(mode="after")
    def labels_cannot_overstate(self) -> "ValidationReport":
        if EvidenceLabel.UNKNOWN_SEMANTIC_SUPPORT not in self.labels:
            raise ValueError("semantic support must remain UNKNOWN")
        if self.notices != NOTICES:
            raise ValueError("report notices are fixed")
        if (self.outcome == Outcome.REJECTED) != bool(self.errors):
            raise ValueError("REJECTED outcome must coincide with input errors")
        if self.mechanical_gate == GateResult.PASS and not (
            self.outcome == Outcome.CHECKED
            and self.claim_status == ClaimStatus.APPROVED
            and EvidenceLabel.NOT_APPROVED not in self.labels
            and self.sources
            and all(source.label == EvidenceLabel.SOURCE_MATCH for source in self.sources)
        ):
            raise ValueError("mechanical gate PASS requires approved claim and all SOURCE_MATCH")
        return self


class ClaimRecord(FrozenStrictModel):
    """A claim prepared for human review. Not an approval and not publishable."""

    record_schema_version: Literal["1"] = "1"
    review_state: Literal["PENDING_HUMAN_REVIEW"] = "PENDING_HUMAN_REVIEW"
    semantic_support: Literal["UNKNOWN"] = "UNKNOWN"
    claim: Claim
    labels: tuple[EvidenceLabel, ...]
    mechanical_gate: GateResult
    report_sha256: str


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def render_json(report: ValidationReport) -> str:
    return canonical_json(report.model_dump(mode="json"))


def render_record(record: ClaimRecord) -> str:
    return canonical_json(record.model_dump(mode="json"))


def build_claim_record(claim: Claim, report: ValidationReport) -> ClaimRecord:
    if report.outcome != Outcome.CHECKED:
        raise ValueError("claim records are only prepared for CHECKED reports")
    return ClaimRecord(
        claim=claim,
        labels=report.labels,
        mechanical_gate=report.mechanical_gate,
        report_sha256=hashlib.sha256(render_json(report).encode("utf-8")).hexdigest(),
    )


def render_text(report: ValidationReport) -> str:
    lines = [
        f"Evidence check report (schema {report.report_schema_version})",
        f"Outcome: {report.outcome}",
        f"Claim: {report.claim_id or '-'} [{report.claim_status or '-'}] "
        f"workspace={report.workspace_id or '-'}",
        f"Mechanical gate: {report.mechanical_gate}",
        f"Labels: {', '.join(report.labels)}",
        f"Claim input sha256: {report.claim_sha256 or '-'}",
        f"Manifest input sha256: {report.manifest_sha256 or '-'}",
        "Sources:" if report.sources else "Sources: (none checked)",
    ]
    for index, source in enumerate(report.sources, start=1):
        lines += [
            f"  [{index}] {source.label} ({source.reason}) {source.path} @ {source.revision_sha}",
            f"      url: {source.url}",
            f"      expected sha256: {source.expected_sha256 or '-'}",
            f"      observed sha256: {source.observed_sha256 or '-'}",
            f"      note: {source.diagnostic}",
        ]
    lines.append("Errors:" if report.errors else "Errors: (none)")
    for error in report.errors:
        lines.append(f"  - {error.input} {error.code} at {error.location}: {error.message}")
    lines.append("Notices:")
    lines += [f"  - {notice}" for notice in report.notices]
    text = "\n".join(lines) + "\n"
    # ASCII-only output: stable across console encodings and free of terminal control bytes.
    return text.encode("ascii", "backslashreplace").decode("ascii")
