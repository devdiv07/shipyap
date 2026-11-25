"""What blocks Approve, and what is only shown beside a sentence. Pure: no I/O, no model.

Blocks (Approve is refused while any remains):
- a blocking editorial finding of the draft as saved now (a failed number check among them),
  re-run on every edit (`check_draft`);
- an OUTDATED sentence: the mechanical supersession check (`editorial.supersession`), also
  re-run on every edit, or the fact check's OUTDATED verdict;
- the fact check's CONTRADICTED verdict;
- CODE_MEANING_INCOMPLETE (C1): a sentence says what a value means, and the value is also set in
  lines it does not cite. It blocks until a fact check of these exact words was sent those lines
  (its verdict lists them under `supplied`) and judged the sentence SUPPORTED (CONTRADICTED or
  OUTDATED block through the verdict). An OVERSTATED or UNVERIFIABLE verdict, a warning for any
  other sentence, keeps this one blocked: the lines it was sent decide whether it is right. (CODE_OUTCOME_UNSUPPORTED, the
  other C1 code, is a blocking finding and blocks whatever the verdict.)

Warnings (shown beside the sentence, with the lines or records that matter; never a block):
editorial warnings, the fact check's OVERSTATED and UNVERIFIABLE verdicts, and "not re-checked"
for a sentence the owner edited after the fact check: the model judged other words, so its
verdict no longer applies, while the mechanical checks above judge the edited words.

Style notes (C2a, `editorial.voice`: VOICE_* and READ_* codes) are warnings too, judged on the
words as saved now. They never block and never count as "needs attention" (`is_style`), with
one exception (C2b): on a voice dry-nerdy draft (prompt p5, read from the generator label the
tool wrote) a labelled section or Markdown structure (`voice.STRUCTURE_CODES`) is Must fix, as
code LABELLED_SECTION or MARKDOWN_STRUCTURE.

A gate is a barrier against a known error, never certification: an approvable draft still
needs the owner's reading of every claim.
"""

from dataclasses import dataclass, field

from shipyap.editorial.closing import is_closing
from shipyap.editorial.code_claims import gap_labels
from shipyap.editorial.models import (
    AssertionMap,
    DraftResponse,
    DraftStatus,
    FindingCode,
    Severity,
)
from shipyap.editorial.supersession import Superseded
from shipyap.editorial.voice import STRUCTURE_CODES, check_voice, is_dry_nerdy

BLOCKING_VERDICTS = ("CONTRADICTED", "OUTDATED")
WARNING_VERDICTS = ("OVERSTATED", "UNVERIFIABLE")
NOT_RECHECKED = "NOT_RECHECKED"
STYLE_PREFIXES = ("VOICE_", "READ_")


def is_style(note: "Note") -> bool:
    """A voice or readability note: a warning that never blocks or asks for attention."""
    return note.code.startswith(STYLE_PREFIXES)


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
    # The tool's closing line (C2c) is a link, not prose: the voice checks read the post without it.
    voiced = response.segments
    if voiced and is_closing(voiced[-1]):
        voiced = voiced[:-1]
    style = check_voice(voiced)
    strict = is_dry_nerdy(response.generator.label)
    segments = []
    for record, segment in zip(amap.segments, response.segments, strict=True):
        blocks: list[Note] = []
        warnings: list[Note] = []
        model = verdicts.get(segment.id)
        current = model is not None and before.get(segment.id) == segment.text
        for finding in record.findings:
            if finding.code == FindingCode.CODE_MEANING_INCOMPLETE:
                needed = gap_labels(finding.detail)
                supplied = set((model or {}).get("supplied") or ()) if current else set()
                seen = bool(needed) and supplied >= set(needed)
                judged = str((model or {}).get("verdict")) if seen else None
                if not seen:
                    blocks.append(Note(finding.code.value, finding.message, needed))
                elif judged == "SUPPORTED" or judged in BLOCKING_VERDICTS:
                    # SUPPORTED lifts it; CONTRADICTED or OUTDATED blocks through the verdict.
                    warnings.append(
                        Note(finding.code.value, "the fact check was sent these lines", needed)
                    )
                else:
                    detail = f"the fact check was sent these lines and judged it {judged}"
                    blocks.append(Note(finding.code.value, detail, needed))
                continue
            note = Note(finding.code.value, finding.detail or finding.message)
            (blocks if finding.severity == Severity.BLOCKING else warnings).append(note)
        if (hit := outdated.get(segment.id)) is not None:
            blocks.append(Note("OUTDATED", hit.reason, hit.records))
        verdict = None
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
        for n in style:
            if n.segment_id != segment.id:
                continue
            if strict and n.code in STRUCTURE_CODES:
                blocks.append(Note(n.code.removeprefix("VOICE_"), n.detail))
            else:
                warnings.append(Note(n.code, n.detail))
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
    ) + tuple(Note(n.code, n.detail) for n in style if n.segment_id is None)
    if amap.draft_status != DraftStatus.READY_FOR_REVIEW and not (
        document_blocks or any(s.blocks for s in segments)
    ):
        document_blocks += (Note(amap.draft_status.value, "the draft is not ready for review"),)
    return Gate(tuple(segments), document_blocks, document_warnings, bool(verdicts))
