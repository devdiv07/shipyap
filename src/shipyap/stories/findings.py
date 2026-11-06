"""Findings document, version 1, and the deterministic check of each finding against its snapshot.

A findings document proposes story angles for ONE pinned snapshot (bound by the snapshot's
manifest digest, as a drafter response is bound to its brief). Who wrote it, a person now or a
model later, is not recorded and does not matter: every finding must pass the same checks.

What a passing check establishes, and nothing more (the editorial grounding rules, reused):
- every cited path is a file of the verified snapshot, every line range exists and every quote
  is located in its lines after `editorial.grounding.normalize`;
- every digit-based number in the angle or the support text appears in a quote of the finding,
  or in the cited lines of a reference without a quote (`evidence_numbers`). A label's number
  ("Attempt 2") is a name: the label must appear in some snapshot file instead.
Semantic support stays UNKNOWN: a located citation does not mean the angle is right or worth a
post. A finding that fails any check is refused, never recorded and never repaired.

Limitation: comparisons, superlatives and first-person attribution are not judged here. Any
draft written from a candidate still goes through the unchanged `draft-build` checks.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from shipyap.editorial.grounding import (
    claim_numbers,
    label_index,
    labels,
    normalize,
    number_tokens,
)
from shipyap.editorial.inputs import sanitized_details
from shipyap.editorial.models import (
    SHA256_PATTERN,
    WORKSPACE_PATTERN,
    SourceRef,
    Text,
)
from shipyap.editorial.sources import VerifiedSnapshot
from shipyap.evidence.report import ErrorCode
from shipyap.evidence.strict_json import StrictJsonError, loads_strict
from shipyap.evidence.validation import UnreadableInput
from shipyap.stories.errors import StoryError, StoryErrorCode

FINDINGS_CAP_BYTES = 64 * 1024
MAX_FINDINGS = 20
MAX_FINDING_REFS = 4
MIN_TEXT_CHARS = 20  # the shortest angle or support a finding may have

# Lower case words joined by hyphens; the author's name for one angle within one repository.
StoryKey = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=3, max_length=64)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoryFinding(_Frozen):
    story_key: StoryKey
    # the proposed story, one sentence or two
    angle: Text = Field(min_length=MIN_TEXT_CHARS, max_length=300)
    # why the cited text supports it
    support: Text = Field(min_length=MIN_TEXT_CHARS, max_length=600)
    source_refs: tuple[SourceRef, ...] = Field(min_length=1, max_length=MAX_FINDING_REFS)


class FindingsDocument(_Frozen):
    findings_version: Literal["1"]
    workspace_id: str = Field(pattern=WORKSPACE_PATTERN)
    snapshot_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    # Empty is valid: nothing in this snapshot is worth a story.
    findings: tuple[StoryFinding, ...] = Field(default=(), max_length=MAX_FINDINGS)

    @model_validator(mode="after")
    def keys_are_unique(self) -> "FindingsDocument":
        keys = [finding.story_key for finding in self.findings]
        if len(set(keys)) != len(keys):
            raise ValueError("story_key values must be unique within a findings file")
        return self


def parse_findings(data: bytes | UnreadableInput) -> FindingsDocument:
    """Capped bytes, strict JSON, strict model; rejected values are never echoed."""
    if isinstance(data, UnreadableInput):
        too_large = data.code == ErrorCode.INPUT_TOO_LARGE
        raise StoryError(
            StoryErrorCode.FINDINGS_TOO_LARGE if too_large else StoryErrorCode.FINDINGS_UNREADABLE
        )
    try:
        value = loads_strict(data)
    except StrictJsonError:
        raise StoryError(StoryErrorCode.FINDINGS_INVALID_JSON) from None
    try:
        return FindingsDocument.model_validate(value)
    except ValidationError as exc:
        raise StoryError(StoryErrorCode.FINDINGS_INVALID, sanitized_details(exc)) from None


class Refusal(StrEnum):
    REF_PATH_NOT_IN_SNAPSHOT = "REF_PATH_NOT_IN_SNAPSHOT"
    REF_RANGE_INVALID = "REF_RANGE_INVALID"
    REF_QUOTE_NOT_FOUND = "REF_QUOTE_NOT_FOUND"
    NUMBER_NOT_IN_EVIDENCE = "NUMBER_NOT_IN_EVIDENCE"
    # Decided by the backlog, not here: the same angle is already recorded under another key.
    DUPLICATE_ANGLE = "DUPLICATE_ANGLE"


@dataclass(frozen=True)
class EvidenceRef:
    """A located citation, pinned by the cited file's SHA-256 in the snapshot."""

    path: str
    line_start: int
    line_end: int
    quote: str | None
    file_sha256: str

    def label(self) -> str:
        return f"{self.path}:{self.line_start}-{self.line_end}"


@dataclass(frozen=True)
class FindingCheck:
    finding: StoryFinding
    refs: tuple[EvidenceRef, ...]  # every reference, when refusal is None
    refusal: Refusal | None
    detail: str | None  # a reference location; never finding text


def check_finding(finding: StoryFinding, snapshot: VerifiedSnapshot) -> FindingCheck:
    sources = {source.path: source for source in snapshot.sources}
    refs: list[EvidenceRef] = []
    available: set[str] = set()
    for ref in finding.source_refs:
        label = f"{ref.path}:{ref.line_start}-{ref.line_end}"
        source = sources.get(ref.path)
        if source is None:
            return _refused(finding, Refusal.REF_PATH_NOT_IN_SNAPSHOT, label)
        if ref.line_end > len(source.lines):
            return _refused(finding, Refusal.REF_RANGE_INVALID, label)
        text = "\n".join(source.lines[ref.line_start - 1 : ref.line_end])
        if ref.quote is not None:
            quote = normalize(ref.quote)
            if not quote or quote not in normalize(text):
                return _refused(finding, Refusal.REF_QUOTE_NOT_FOUND, label)
        available |= evidence_numbers(text, ref.quote)
        refs.append(EvidenceRef(ref.path, ref.line_start, ref.line_end, ref.quote, source.sha256))
    text = f"{finding.angle}\n{finding.support}"
    known = snapshot_labels(snapshot)
    claimed = [*claim_numbers(text), *(t for w, t in labels(text) if (w, t) not in known)]
    if any(token not in available for token in claimed):
        return _refused(finding, Refusal.NUMBER_NOT_IN_EVIDENCE, None)
    return FindingCheck(finding=finding, refs=tuple(refs), refusal=None, detail=None)


def snapshot_labels(snapshot: VerifiedSnapshot) -> frozenset[tuple[str, str]]:
    """Every label ("Attempt 2") that a snapshot file's text or path shows."""
    return label_index(
        [source.path for source in snapshot.sources]
        + [line for source in snapshot.sources for line in source.lines]
    )


def evidence_numbers(cited: str, quote: str | None) -> set[str]:
    """The numbers a reference supports. A quote limits the evidence to the quoted words; without
    one, the whole cited range counts. Numbers are read with the source's own boundaries, so a
    quote cut out of "453" does not support 45."""
    in_lines = set(number_tokens(cited))
    return in_lines if quote is None else in_lines & set(number_tokens(quote))


def angle_fingerprint(angle: str) -> str:
    """Case- and whitespace-insensitive form of an angle, to catch one angle under two keys."""
    return normalize(angle).casefold()


def _refused(finding: StoryFinding, refusal: Refusal, detail: str | None) -> FindingCheck:
    return FindingCheck(finding=finding, refs=(), refusal=refusal, detail=detail)
