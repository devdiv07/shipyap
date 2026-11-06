"""The X text of a reviewed LinkedIn draft: deterministic, words only from the draft, no model.

The rule (owner's decisions, 2026-11-03):
1. Opening: whole sentences from the start of the post, in order, never cut inside a sentence,
   never rewritten, reordered or joined to anything new. Sentences are found inside each segment
   (a segment boundary is always a sentence boundary); a decimal, a version or an abbreviation
   such as "e.g." is not one.
2. Scope line: if the draft has a segment that counts as its limitation reference (the one
   auto-repair adds from the record's own scope statement, or any segment the checks count for
   NO_LIMITATION_REFERENCE), it is appended verbatim after the opening, as its own paragraph,
   unless the opening already holds it. The opening is as long as fits with it.
3. The whole text must fit X's 280 weighted characters (`publication.x`); the first sentence that
   would pass it ends the opening. Opening plus scope line not fitting: nothing.
4. It must stand alone: at least MIN_CHARS characters, no list line, the opening not ending with
   a colon and not pointing elsewhere ("below", "the following", ...).
5. The unchanged free mechanical checks run on the X text as its own draft, with each sentence
   keeping its segment's references: any blocking finding (numbers, labels, first person, ...)
   or a superseded-plan sentence means nothing.

Anything that fails gives no X text and one fixed reason. X then gets nothing for that post.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from public_research_os.editorial.grounding import check_draft, render_post, scope_lines
from public_research_os.editorial.models import (
    Brief,
    DraftResponse,
    Segment,
    Severity,
    is_tool_item,
)
from public_research_os.editorial.supersession import Record, check_superseded
from public_research_os.publication.errors import PublicationError
from public_research_os.publication.x import X_MAX_WEIGHT, check_x_text, x_weight

MIN_CHARS = 100

NO_SEGMENTS = "the draft has no post"
FIRST_TOO_LONG = f"the first sentence is over {X_MAX_WEIGHT} weighted characters"
SCOPE_DOES_NOT_FIT = "scope line doesn't fit"
TOO_SHORT = f"too short to stand alone (under {MIN_CHARS} characters)"
ENDS_WITH_COLON = "the opening ends with a colon, so it leads into something X would not get"
POINTS_ELSEWHERE = "the opening points to another part of the post"
HAS_LIST = "the text contains a list line"
NOT_PUBLISHABLE = "the text is not publishable on X"
CHECK_BLOCKS = "a mechanical check blocks the X text"
SUPERSEDED = "a sentence of the X text repeats a plan a later record superseded"

_END = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_ABBREVIATIONS = frozenset(
    {"e.g", "i.e", "etc", "vs", "cf", "mr", "mrs", "ms", "dr", "no", "fig", "approx", "inc"}
)
_INITIALS = re.compile(r"[a-z](?:\.[a-z])*")
_POINTER = re.compile(
    r"\b(?:below|above|the following|here'?s how|here is how|this list|"
    r"in the comments?|link in (?:the )?comments?)\b",
    re.IGNORECASE,
)
_LIST_LINE = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s", re.MULTILINE)


@dataclass(frozen=True)
class XPost:
    """The X text, or None with the reason X gets nothing."""

    text: str | None
    reason: str = ""
    segment_ids: tuple[str, ...] = ()  # the draft segments its sentences come from


def sentence_ends(text: str) -> list[int]:
    """The end offset of every sentence of `text` (the last is always len(text))."""
    ends = []
    for match in _END.finditer(text):
        word = text[: match.start() + 1].split()[-1]
        stem = word.strip("\"'“‘(").rstrip(".!?").lower()
        if text[match.start()] == "." and (stem in _ABBREVIATIONS or _INITIALS.fullmatch(stem)):
            continue
        ends.append(match.start() + len(match.group().rstrip()))
    return [*ends, len(text)]


def scope_segment(brief: Brief, response: DraftResponse) -> Segment | None:
    """The first segment that counts as the draft's limitation reference, as the checks count it:
    a `limitations.*` item the contributor wrote (never one of the tool's reviewer-only items), or
    a whole own scope statement of a cited file."""
    scopes = {source.path: scope_lines(source.lines) for source in brief.sources}
    items = brief.contribution.referenceable_items()
    for segment in response.segments:
        if any(
            ref.startswith("limitations.") and ref in items and not is_tool_item(items[ref])
            for ref in segment.author_refs
        ):
            return segment
        for ref in segment.source_refs:
            scope = scopes.get(ref.path)
            if (
                scope is not None
                and ref.line_start <= scope[0]
                and scope[1] <= ref.line_end
                and ref.quote is None
            ):
                return segment
    return None


def x_post(
    brief: Brief,
    response: DraftResponse,
    records: Sequence[Record] = (),
    brief_sha256: str = "0" * 64,
) -> XPost:
    segments = response.segments
    if not segments:
        return XPost(None, NO_SEGMENTS)
    scope = scope_segment(brief, response)
    # Units in post order: (segment index, end offset in its text). The scope segment is one
    # unit: it is only ever taken whole.
    units = [
        (index, end)
        for index, segment in enumerate(segments)
        for end in ([len(segment.text)] if segment is scope else sentence_ends(segment.text))
    ]
    chosen: list[Segment] = []
    taken: dict[int, int] = {}
    for index, end in units:
        trial = {**taken, index: end}
        candidate = _segments(segments, trial, scope)
        if x_weight(render_post(candidate)) > X_MAX_WEIGHT:
            break
        taken, chosen = trial, candidate
    if not taken:
        first_alone = segments[0].text[: sentence_ends(segments[0].text)[0]]
        if scope is not None and scope is not segments[0] and x_weight(first_alone) <= X_MAX_WEIGHT:
            return XPost(None, SCOPE_DOES_NOT_FIT)
        return XPost(None, FIRST_TOO_LONG)
    text = render_post(chosen)
    opening = render_post(s for s in chosen if scope is None or s.id != scope.id)
    if len(text) < MIN_CHARS:
        return XPost(None, TOO_SHORT)
    if opening.rstrip().endswith(":"):
        return XPost(None, ENDS_WITH_COLON)
    if _POINTER.search(opening):
        return XPost(None, POINTS_ELSEWHERE)
    if _LIST_LINE.search(text):
        return XPost(None, HAS_LIST)
    try:
        check_x_text(text)
    except PublicationError:
        return XPost(None, NOT_PUBLISHABLE)
    data = response.model_dump(mode="json")
    data["segments"] = [segment.model_dump(mode="json") for segment in chosen]
    data["verification_needed"] = []
    drafted = DraftResponse.model_validate(data)
    amap = check_draft(brief, drafted, brief_sha256, "0" * 64)
    blocking = [
        finding
        for finding in (*amap.document_findings, *(f for s in amap.segments for f in s.findings))
        if finding.severity == Severity.BLOCKING
    ]
    if blocking:
        where = ", ".join(
            sorted({f"{f.code.value} {f.segment_id or ''}".strip() for f in blocking})
        )
        return XPost(None, f"{CHECK_BLOCKS}: {where}")
    superseded = check_superseded(brief, drafted, tuple(records))
    if superseded:
        return XPost(None, f"{SUPERSEDED}: {', '.join(s.segment_id for s in superseded)}")
    return XPost(text, "", tuple(segment.id for segment in chosen))


def _segments(
    segments: Sequence[Segment], taken: dict[int, int], scope: Segment | None
) -> list[Segment]:
    """The X draft's segments: each taken segment cut after its taken sentences, in post order,
    then the scope segment as its own paragraph when the opening did not reach it."""
    out = []
    for index in sorted(taken):
        segment = segments[index]
        text = segment.text[: taken[index]].rstrip()
        out.append(
            segment.model_copy(
                update={
                    "text": text,
                    "paragraph_break_before": bool(out) and segment.paragraph_break_before,
                }
            )
        )
    if scope is not None and not any(s.id == scope.id for s in out):
        out.append(scope.model_copy(update={"paragraph_break_before": True}))
    return out
