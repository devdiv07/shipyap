"""Validate the stored fact-check record before granting approval.

An auto-repaired draft can rely on its origin's completed fact check ("inherited") when every
difference between the two is explained, and re-derived here rather than taken from the repair
record's log lines:

- a sentence with the same text keeps the origin's verdict;
- a hash or hex ID dropped as an aside (`repair._drop_aside` on exactly the IDs no cited file
  holds) keeps the origin's verdict: only words were removed;
- an added sentence that is exactly the cited record's own scope line (`repair._scope_segment`)
  carries no model verdict: it is the record's words, verbatim, and the mechanical checks judge it;
- a dropped sentence is either a tool-text sentence the free repair may drop (no source lines,
  blocked only for TOOL_ITEM_CITED / TOOL_PHRASE) or one a recorded model round removed;
- a sentence a recorded model round rewrote must hold exactly the last applied rewrite and its
  own re-check verdict in the repair record (the repair loop drops a rewritten sentence's old
  verdict, so a verdict there is the re-check's).

Anything else (a changed reference, a sentence nobody recorded, an origin without a completed
check) inherits nothing, so the draft stays "Not fact-checked". Pure: no I/O.
"""

from dataclasses import dataclass

from shipyap.automation.factcheck import VERDICTS
from shipyap.automation.repair import DROPPABLE, _drop_aside, _scope_segment
from shipyap.editorial.grounding import hex_ids
from shipyap.editorial.models import AssertionMap, Brief, DraftResponse, Severity

COMPLETE = "COMPLETE"
INHERITED = "INHERITED"
REQUIRED = "REQUIRED"


def _valid_verdict(verdict, segments: set[str]) -> bool:
    return (
        isinstance(verdict, dict)
        and isinstance(verdict.get("segment_id"), str)
        and verdict["segment_id"] in segments
        and isinstance(verdict.get("verdict"), str)
        and verdict["verdict"] in VERDICTS
        and isinstance(verdict.get("reason"), str)
        and isinstance(verdict.get("refs"), (list, tuple))
        and all(isinstance(ref, str) for ref in verdict["refs"])
    )


def completed_fact_check(check, response) -> bool:
    if not isinstance(check, dict) or not isinstance(check.get("verdicts"), list):
        return False
    segments = {segment.id for segment in response.segments}
    if not segments:
        return False
    found = set()
    for verdict in check["verdicts"]:
        if not _valid_verdict(verdict, segments) or verdict["segment_id"] in found:
            return False
        found.add(verdict["segment_id"])
    return found == segments


@dataclass(frozen=True)
class Inherited:
    """The verdicts a repaired draft relies on, and why each sentence is covered."""

    document: dict  # {"draft", "verdicts", "inherited_from", "basis"}; no verdict for a scope line
    basis: dict[str, str]  # segment id -> how it is covered, in plain words


def _final_rewrites(record: dict) -> dict[str, str] | None:
    """The last applied rewrite of each sentence over the rounds that did not fail."""
    rounds = record.get("rounds")
    if not isinstance(rounds, list):
        return None
    final: dict[str, str] = {}
    for item in rounds:
        if not isinstance(item, dict):
            return None
        rewrites = item.get("rewrites")
        if not isinstance(rewrites, dict):
            return None
        if item.get("failed") is not None:
            continue
        for sid, text in rewrites.items():
            if not isinstance(sid, str) or not isinstance(text, str):
                return None
            final[sid] = text
    return final


def _round_of(record: dict, sid: str) -> int:
    number = 0
    for item in record.get("rounds") or []:
        if item.get("failed") is None and sid in (item.get("rewrites") or {}):
            number = item.get("round") if isinstance(item.get("round"), int) else number
    return number


def _id_dropped(brief: Brief, segment, origin_text: str) -> bool:
    """`segment.text` is `origin_text` with only unheld hex IDs removed as asides."""
    files = {source.path: "\n".join(source.lines) for source in brief.sources}
    held = "\n".join(files.get(ref.path, "") for ref in segment.source_refs).casefold()
    text = origin_text
    for token in hex_ids(origin_text):
        if token not in held:
            text, _ = _drop_aside(text, token)
    return text != origin_text and text == segment.text


def _same_claim_frame(a, b) -> bool:
    """Same basis and references; only text and paragraph breaks may differ."""
    return a.basis == b.basis and a.source_refs == b.source_refs and a.author_refs == b.author_refs


def inherited_fact_check(
    brief: Brief,
    origin: DraftResponse,
    origin_amap: AssertionMap,
    origin_check,
    repaired: DraftResponse,
    record,
    origin_name: str,
    repaired_name: str,
) -> Inherited | None:
    """The origin's completed fact check carried to the repaired draft, or None (blocked)."""
    if not completed_fact_check(origin_check, origin) or not isinstance(record, dict):
        return None
    if not repaired.segments:
        return None
    origin_ids = {s.id for s in origin.segments}
    if sum(s.id not in origin_ids for s in repaired.segments) > 1:
        return None  # the free repair adds one scope sentence at most
    final = _final_rewrites(record)
    if final is None:
        return None
    rechecked = {}
    record_check = record.get("fact_check")
    if isinstance(record_check, dict) and isinstance(record_check.get("verdicts"), list):
        ids = {s.id for s in repaired.segments}
        for verdict in record_check["verdicts"]:
            if _valid_verdict(verdict, ids):
                rechecked.setdefault(verdict["segment_id"], verdict)
    inherited = {v["segment_id"]: v for v in origin_check["verdicts"]}
    before = {s.id: s for s in origin.segments}
    findings = {s.id: s for s in origin_amap.segments}
    verdicts: list[dict] = []
    basis: dict[str, str] = {}

    for segment in repaired.segments:
        sid = segment.id
        old = before.get(sid)
        if sid in final and final[sid] and final[sid] == segment.text:
            if sid not in rechecked:
                return None  # rewritten by a model with no re-check of its own
            if old is not None and not _same_claim_frame(old, segment):
                return None
            if old is None and not _is_scope(brief, repaired, segment, frame_only=True):
                return None
            verdicts.append(rechecked[sid])
            basis[sid] = f"re-checked after repair round {_round_of(record, sid)}"
            continue
        if old is None:
            if not _is_scope(brief, repaired, segment):
                return None
            ref = segment.source_refs[0]
            basis[sid] = (
                f"the record's own scope line, verbatim ({ref.path}:{ref.line_start}-"
                f"{ref.line_end}); no model verdict, the mechanical checks apply"
            )
            continue
        if not _same_claim_frame(old, segment):
            return None
        if segment.text == old.text:
            verdicts.append(inherited[sid])
            basis[sid] = "inherited"
        elif _id_dropped(brief, segment, old.text):
            verdicts.append(inherited[sid])
            basis[sid] = "inherited (an unneeded ID was dropped)"
        else:
            return None  # changed words nobody recorded: a human or unknown edit

    kept = {s.id for s in repaired.segments}
    for old in origin.segments:
        if old.id in kept:
            continue
        if final.get(old.id) == "":
            continue  # a recorded model round removed it
        record_findings = findings.get(old.id)
        codes = {
            f.code
            for f in (record_findings.findings if record_findings else ())
            if f.severity == Severity.BLOCKING
        }
        if old.source_refs or not codes or not codes <= DROPPABLE:
            return None
    document = {
        "draft": repaired_name,
        "verdicts": verdicts,
        "inherited_from": origin_name,
        "basis": basis,
    }
    return Inherited(document, basis)


def _is_scope(brief: Brief, repaired: DraftResponse, segment, frame_only: bool = False) -> bool:
    """`segment` is the scope sentence the free repair builds for this draft."""
    data = repaired.model_dump(mode="json")
    data["segments"] = [s for s in data["segments"] if s["id"] != segment.id]
    expected = _scope_segment(brief, data)
    if expected is None:
        return False
    got = segment.model_dump(mode="json")
    keys = ("basis", "source_refs", "author_refs") if frame_only else tuple(expected)
    keys = tuple(k for k in keys if k != "id")
    return all(got.get(k) == expected[k] for k in keys)
