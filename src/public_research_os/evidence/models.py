"""Strict evidence contracts. Structural validity is NOT scientific verification."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ClaimKind(StrEnum):
    NUMERIC = "numeric"
    OBSERVATION = "observation"
    INFERENCE = "inference"
    OPINION = "opinion"


class ClaimStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    WITHDRAWN = "withdrawn"


class EvidenceSource(StrictModel):
    url: HttpUrl
    revision_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("url")
    @classmethod
    def require_https(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("source URL must be HTTPS")
        return value

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if value.strip() in {"", "."} or path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("source path must be a safe repository-relative POSIX path")
        return value


class CountMetric(StrictModel):
    numerator: int = Field(ge=0, strict=True)
    denominator: int = Field(gt=0, strict=True)
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def numerator_cannot_exceed_denominator(self) -> "CountMetric":
        if self.numerator > self.denominator:
            raise ValueError("numerator exceeds denominator")
        return self


class Claim(StrictModel):
    workspace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    claim_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9_-]{1,63}$")
    kind: ClaimKind
    statement: str = Field(min_length=10)
    status: ClaimStatus
    sources: list[EvidenceSource] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    forbidden_interpretations: list[str] = Field(default_factory=list)
    metric: CountMetric | None = None

    @model_validator(mode="after")
    def numeric_claim_requires_metric(self) -> "Claim":
        if self.kind == ClaimKind.NUMERIC and self.metric is None:
            raise ValueError("numeric claim requires a count metric")
        if any(not value.strip() for value in self.limitations):
            raise ValueError("limitations must be meaningful")
        return self


def require_approved_claim(claim: Claim) -> None:
    """One mechanical gate. Does not judge factual or semantic support."""
    if claim.status != ClaimStatus.APPROVED:
        raise ValueError(f"claim {claim.claim_id} is not approved: {claim.status}")
