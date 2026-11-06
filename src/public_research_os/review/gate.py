"""What blocks Approve, and what is only shown beside a sentence. Pure: no I/O, no model.

Blocks (Approve is refused while any remains):
- a blocking editorial finding of the draft as saved now (a failed number check among them),
  re-run on every edit (`check_draft`);
- an OUTDATED sentence: the mechanical supersession check (`editorial.supersession`), also
  re-run on every edit, or the fact check's OUTDATED verdict;
- the fact check's CONTRADICTED verdict.

Warnings (shown beside the sentence, with the lines or records that matter; never a block):
editorial warnings, the fact check's OVERSTATED and UNVERIFIABLE verdicts, and "not re-checked"
for a sentence the owner edited after the fact check: the model judged other words, so its
verdict no longer applies, while the mechanical checks above judge the edited words.

A gate is a barrier against a known error, never certification: an approvable draft still
needs the owner's reading of every claim.
"""

from dataclasses import dataclass, field

from public_research_os.editorial.models import AssertionMap, DraftResponse, DraftStatus, Severity
from public_research_os.editorial.supersession import Superseded

BLOCKING_VERDICTS = ("CONTRADICTED", "OUTDATED")
WARNING_VERDICTS = ("OVERSTATED", "UNVERIFIABLE")
NOT_RECHECKED = "NOT_RECHECKED"


@dataclass(frozen=True)
class Note:
    """One reason on one sentence: a code, a plain detail and the lines or records behind it."""

    code: str
    detail: str
    refs: tuple[str, ...] = ()

    def line(self) -> str:
        where = f" [{', '.join(self.refs)}]" if self.refs else ""
        return f"{self.code} {self.detail}{where}".strip()


@dataclass(frozen=True)
class SegmentGate:
    segment_id: str
    blocks: tuple[Note, ...] = ()
    warnings: tuple[Note, ...] = ()
    verdict: str | None = None  # the fact check's verdict, when it applies to these words


@dataclass(frozen=True)
class Gate:
    segments: tuple[SegmentGate, ...]
    document_blocks: tuple[Note, ...] = ()
    document_warnings: tuple[Note, ...] = field(default=())
    fact_checked: bool = False

    @property
    def approvable(self) -> bool:
        return not self.document_blocks and not any(s.blocks for s in self.segments)

    def blocked(self) -> tuple[SegmentGate, ...]:
        return tuple(s for s in self.segments if s.blocks)

    def reasons(self) -> tuple[str, ...]:
        """Every block, one line each, for a refusal message."""
        return (
            *(note.line() for note in self.document_blocks),
            *(f"{s.segment_id}: {note.line()}" for s in self.segments for note in s.blocks),
        )


def judge(
    amap: AssertionMap,
    response: DraftResponse,
    superseded: tuple[Superseded, ...] = (),
    fact_check: dict | None = None,
    checked: DraftResponse | None = None,
) -> Gate:
    """The gate of a saved draft. `fact_check` is the stored verdict document; `checked` is the
    response the fact check read (None: the same as `response`). A segment whose text differs
    from the checked one keeps no model verdict."""
    verdicts: dict[str, dict] = {}
    if fact_check:
        for item in fact_check.get("verdicts") or []:
            if isinstance(item, dict) and isinstance(item.get("segment_id"), str):
                verdicts.setdefault(item["segment_id"], item)
    before = {s.id: s.text for s in (checked or response).segments}
    outdated = {s.segment_id: s for s in superseded}
    segments = []
    for record, segment in zip(amap.segments, response.segments, strict=True):
        blocks: list[Note] = []
        warnings: list[Note] = []
        for finding in record.findings:
            note = Note(finding.code.value, finding.detail or finding.message)
            (blocks if finding.severity == Severity.BLOCKING else warnings).append(note)
        if (hit := outdated.get(segment.id)) is not None:
            blocks.append(Note("OUTDATED", hit.reason, hit.records))
        verdict = None
        model = verdicts.get(segment.id)
        if model is not None and before.get(segment.id) != segment.text:
            warnings.append(
                Note(NOT_RECHECKED, "edited after the fact check; its verdict no longer applies")
            )
        elif model is not None:
            verdict = str(model.get("verdict"))
            refs = tuple(str(r) for r in model.get("refs") or [])
            note = Note(f"FACT_CHECK_{verdict}", str(model.get("reason") or ""), refs)
            if verdict in BLOCKING_VERDICTS:
                blocks.append(note)
            elif verdict in WARNING_VERDICTS:
                warnings.append(note)
        segments.append(SegmentGate(segment.id, tuple(blocks), tuple(warnings), verdict))
    document_blocks = tuple(
        Note(f.code.value, f.detail or f.message)
        for f in amap.document_findings
        if f.severity == Severity.BLOCKING
    )
    document_warnings = tuple(
        Note(f.code.value, f.detail or f.message)
        for f in amap.document_findings
        if f.severity != Severity.BLOCKING
    )
    if amap.draft_status != DraftStatus.READY_FOR_REVIEW and not (
        document_blocks or any(s.blocks for s in segments)
    ):
        document_blocks += (Note(amap.draft_status.value, "the draft is not ready for review"),)
    return Gate(tuple(segments), document_blocks, document_warnings, bool(verdicts))
