"""The deterministic normaliser between a model's proposed story finding and its validation.

A model does not reliably obey the citation rules (10 lines per reference, numbers inside the
quote), and one broken reference used to sink a whole finding. The normaliser repairs what can be
repaired by removing material, never by adding it:

- an over-long reference with a quote is narrowed to the shortest run of its own lines (at most
  MAX_REF_LINES) whose normalized text holds the quote;
- an over-long reference without a quote is split into consecutive references of at most
  MAX_REF_LINES lines that together cover exactly the cited range;
- a bad reference (unknown path, range outside the file or the lines sent, quote not in its range
  or longer than MAX_REF_LINES lines, more than MAX_FINDING_REFS references) is dropped, but only
  while at least one valid reference remains;
- a sentence of the angle or the support whose number is not in the evidence (a quote, or the
  cited lines of a reference without one) is dropped and becomes a review question, but only
  while both texts keep at least MIN_TEXT_CHARS characters. A label's number ("Attempt 2") is
  not a claim when the label appears in some snapshot file (`grounding.labels`);
- the angle's first sentence is the finding's main claim: when it would be dropped, the finding
  is marked `core_claim_dropped` and screening refuses it (CORE_CLAIM_DROPPED) instead of
  passing what is left.

When a rule cannot apply (no valid reference would remain, a text would become too short) the
candidate is left as the model sent it, so validation refuses it with its real reason. A path,
line, quote or number is never invented, and the result goes through the unchanged schema and
06B checks. The normaliser reads no file and has no side effects.

`normalise_response` does the same for a model-written draft response, before the strict parser:
only the parts the schema refuses are touched, by the same narrow, split or drop rules, and a
part the post depends on is never cut (see its docstring for each limit).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ValidationError

from shipyap.editorial.grounding import (
    claim_numbers,
    label_index,
    labels,
    normalize,
)
from shipyap.editorial.models import (
    AUTHOR_REF_PATTERN,
    MAX_AUTHOR_REFS,
    MAX_QUESTIONS,
    MAX_REF_LINES,
    MAX_SEGMENT_REFS,
    GeneratorKind,
    OpenQuestion,
    Outcome,
    SourceRef,
)
from shipyap.evidence.report import canonical_json
from shipyap.evidence.strict_json import StrictJsonError, loads_strict
from shipyap.stories.findings import (
    MAX_FINDING_REFS,
    MIN_TEXT_CHARS,
    evidence_numbers,
)

_TEXT_FIELDS = ("angle", "support")
# A sentence ends at . ! or ? followed by whitespace. Over-splitting ("e.g. 5") only makes a
# dropped piece smaller; it can never add words.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])(\s+)")


class ChangeKind(StrEnum):
    NARROWED = "NARROWED"
    SPLIT = "SPLIT"
    REF_DROPPED = "REF_DROPPED"
    SENTENCE_DROPPED = "SENTENCE_DROPPED"


class DropReason(StrEnum):
    MALFORMED = "MALFORMED"
    PATH_NOT_IN_SNAPSHOT = "PATH_NOT_IN_SNAPSHOT"
    RANGE_INVALID = "RANGE_INVALID"
    NOT_SUPPLIED = "NOT_SUPPLIED"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    QUOTE_TOO_LONG = "QUOTE_TOO_LONG"
    OVER_REF_LIMIT = "OVER_REF_LIMIT"


@dataclass(frozen=True)
class Change:
    """One edit. For a reference: `before` is its label, `after` the new labels or the drop
    reason. For a sentence: `before` is the field, `sentence` the dropped model text (display
    only) and `numbers` the tokens no evidence holds."""

    kind: ChangeKind
    before: str
    after: str
    sentence: str | None = None
    numbers: tuple[str, ...] = ()

    def review_question(self) -> str | None:
        if self.kind != ChangeKind.SENTENCE_DROPPED:
            return None
        return (
            f"The model's {self.before} also claimed {', '.join(self.numbers)}, which no quote "
            "of this finding holds. Is it true? Check the repository before using it."
        )


@dataclass(frozen=True)
class Normalised:
    candidate: object  # what validation judges: the input itself when nothing changed
    changes: tuple[Change, ...]
    core_claim_dropped: bool = False  # the angle's first sentence held an unsupported number

    @property
    def review_questions(self) -> tuple[str, ...]:
        return tuple(q for c in self.changes if (q := c.review_question()) is not None)


def normalise_finding(
    candidate: object,
    files: Mapping[str, Sequence[str]],
    supplied: Mapping[str, int],
) -> Normalised:
    """`files` holds each snapshot file's lines; `supplied` how many of its first lines were sent
    to the model (a path missing from it counts as fully sent)."""
    unchanged = Normalised(candidate, ())
    if not isinstance(candidate, dict) or not isinstance(candidate.get("source_refs"), list):
        return unchanged
    refs: list[dict] = []
    changes: list[Change] = []
    for ref in candidate["source_refs"]:
        kept, change = _reference(ref, files, supplied)
        refs += kept
        if change is not None:
            changes.append(change)
    for extra in refs[MAX_FINDING_REFS:]:
        changes.append(Change(ChangeKind.REF_DROPPED, _label(extra), DropReason.OVER_REF_LIMIT))
    refs = refs[:MAX_FINDING_REFS]
    if not refs:
        return unchanged  # validation names the real problem
    result = {**candidate, "source_refs": refs}
    available: set[str] = set()
    for ref in refs:
        cited = "\n".join(files[ref["path"]][ref["line_start"] - 1 : ref["line_end"]])
        available |= evidence_numbers(cited, ref.get("quote"))
    known = label_index([*files, *(line for lines in files.values() for line in lines)])
    texts, dropped, core = {}, [], False
    for field in _TEXT_FIELDS:
        value = candidate.get(field)
        if not isinstance(value, str):
            continue
        texts[field], removed = _without_unsupported(value, available, known)
        dropped += [
            Change(ChangeKind.SENTENCE_DROPPED, field, "review question", sentence, numbers)
            for _, sentence, numbers in removed
        ]
        core = core or (field == "angle" and any(index == 0 for index, _, _ in removed))
    if core:
        return Normalised(result, (*changes, *dropped), core_claim_dropped=True)
    if dropped and all(len(text.strip()) >= MIN_TEXT_CHARS for text in texts.values()):
        result.update(texts)
        changes += dropped
    return Normalised(result, tuple(changes)) if changes else unchanged


def _reference(
    ref: object, files: Mapping[str, Sequence[str]], supplied: Mapping[str, int]
) -> tuple[list[dict], Change | None]:
    """The valid references `ref` becomes (itself, narrowed or split) and what changed."""
    if not isinstance(ref, dict):
        return [], Change(ChangeKind.REF_DROPPED, "<reference>", DropReason.MALFORMED)
    path, start, end, quote = (ref.get(key) for key in ("path", "line_start", "line_end", "quote"))
    if not (isinstance(path, str) and type(start) is int and type(end) is int):
        return [], Change(ChangeKind.REF_DROPPED, "<reference>", DropReason.MALFORMED)
    label = f"{path}:{start}-{end}"

    def drop(reason: DropReason) -> tuple[list[dict], Change]:
        return [], Change(ChangeKind.REF_DROPPED, label, reason)

    lines = files.get(path)
    if lines is None:
        return drop(DropReason.PATH_NOT_IN_SNAPSHOT)
    if not 1 <= start <= end <= len(lines):
        return drop(DropReason.RANGE_INVALID)
    if end > supplied.get(path, len(lines)):
        return drop(DropReason.NOT_SUPPLIED)
    if quote is not None and not isinstance(quote, str):
        return drop(DropReason.MALFORMED)
    if quote is not None:
        wanted = normalize(quote)
        if not wanted or wanted not in normalize("\n".join(lines[start - 1 : end])):
            return drop(DropReason.QUOTE_NOT_FOUND)
        if end - start < MAX_REF_LINES:
            return [ref], None
        for size in range(1, MAX_REF_LINES + 1):
            for first in range(start, end - size + 2):
                if wanted in normalize("\n".join(lines[first - 1 : first - 1 + size])):
                    narrowed = {**ref, "line_start": first, "line_end": first + size - 1}
                    return [narrowed], Change(ChangeKind.NARROWED, label, _label(narrowed))
        return drop(DropReason.QUOTE_TOO_LONG)
    if end - start < MAX_REF_LINES:
        return [ref], None
    parts = [
        {**ref, "line_start": first, "line_end": min(first + MAX_REF_LINES - 1, end)}
        for first in range(start, end + 1, MAX_REF_LINES)
    ]
    return parts, Change(ChangeKind.SPLIT, label, ", ".join(_label(part) for part in parts))


def unsupported_numbers(
    text: str, available: set[str], known_labels: frozenset[tuple[str, str]]
) -> tuple[str, ...]:
    """The numbers of `text` that need evidence and lack it: claims outside `available`, and
    labels' numbers whose label no file shows."""
    missing = [token for token in claim_numbers(text) if token not in available]
    missing += [
        token
        for word, token in labels(text)
        if (word, token) not in known_labels and token not in available and token not in missing
    ]
    return tuple(missing)


def _without_unsupported(
    text: str, available: set[str], known_labels: frozenset[tuple[str, str]]
) -> tuple[str, list[tuple[int, str, tuple[str, ...]]]]:
    """`text` without its sentences that hold an unsupported number, and those sentences (with
    their position and unsupported numbers). Kept sentences keep their own spacing."""
    pieces = _SENTENCE_BREAK.split(text)  # sentence, spacing, sentence, ...
    kept: list[str] = []
    removed: list[tuple[int, str, tuple[str, ...]]] = []
    for index in range(0, len(pieces), 2):
        sentence = pieces[index]
        spacing = pieces[index + 1] if index + 1 < len(pieces) else ""
        missing = unsupported_numbers(sentence, available, known_labels)
        if missing:
            removed.append((index // 2, sentence, missing))
        else:
            kept.append(sentence + spacing)
    return "".join(kept).rstrip(), removed


def _label(ref: dict) -> str:
    return f"{ref['path']}:{ref['line_start']}-{ref['line_end']}"


# --- draft responses -------------------------------------------------------------------------

_AUTHOR_REF = re.compile(AUTHOR_REF_PATTERN)


@dataclass(frozen=True)
class ResponseRepair:
    data: bytes  # what the strict parser judges: the input itself when nothing changed
    changes: tuple[str, ...]  # one line per edit, for the owner and `pros eval`


def normalise_response(data: bytes, files: Mapping[str, Sequence[str]]) -> ResponseRepair:
    """A model-written draft response with the parts the schema would refuse repaired by removal,
    so one broken part no longer sinks a paid reply. `files` holds the lines sent per path.

    Repaired (the model's own material, narrowed, split or dropped; never added to):
    - a segment's source reference over MAX_REF_LINES lines: narrowed to its quote, or split
      when it has none (`_reference`, as for story findings); any other reference the schema
      refuses is dropped, while at least one valid reference remains;
    - more than MAX_SEGMENT_REFS source references on a segment: the first ones are kept;
    - a segment's author references: duplicates, ids of no possible item and those over
      MAX_AUTHOR_REFS are dropped, while at least one remains;
    - outer whitespace of a segment's text is trimmed;
    - a review question (never post text): invalid references, references over
      MAX_SEGMENT_REFS and questions over MAX_QUESTIONS are dropped, as is a question the
      schema still refuses;
    - a DRAFT's no_post_reason is dropped.

    Refused as before, because the post itself would change: a segment's text (too long,
    control characters), id or basis; a basis its references do not fit; duplicate segment ids;
    too many segments; an outcome without what it needs; a NO_POST reason over its limit.
    A response that is not model-written, or not a JSON object, is returned unchanged."""
    unchanged = ResponseRepair(data, ())
    try:
        value = loads_strict(data)
    except (StrictJsonError, ValueError):
        return unchanged
    generator = value.get("generator") if isinstance(value, dict) else None
    if not isinstance(generator, dict) or generator.get("kind") != GeneratorKind.EXTERNAL_MODEL:
        return unchanged
    changes: list[str] = []
    segments = value.get("segments")
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict):
                changes += _segment(segment, files)
    if value.get("outcome") == Outcome.DRAFT and segments and value.get("no_post_reason"):
        value["no_post_reason"] = None
        changes.append("no_post_reason: dropped (a DRAFT carries none)")
    questions = value.get("verification_needed")
    if isinstance(questions, list):
        value["verification_needed"], dropped = _questions(questions)
        changes += dropped
    if not changes:
        return unchanged
    return ResponseRepair(canonical_json(value).encode("utf-8"), tuple(changes))


def _valid(model: type[BaseModel], value: object) -> bool:
    try:
        model.model_validate(value)
    except ValidationError:
        return False
    return True


def _segment(segment: dict, files: Mapping[str, Sequence[str]]) -> list[str]:
    sid = segment["id"] if isinstance(segment.get("id"), str) else "<segment>"
    changes: list[str] = []
    text = segment.get("text")
    if isinstance(text, str) and text.strip() and text != text.strip():
        segment["text"] = text.strip()
        changes.append(f"{sid}: outer whitespace trimmed")
    refs = segment.get("source_refs")
    if isinstance(refs, list) and refs:
        kept: list = []
        notes: list[str] = []
        for ref in refs:
            if _valid(SourceRef, ref):
                kept.append(ref)
                continue
            repaired, change = _reference(ref, files, {})
            repaired = [r for r in repaired if _valid(SourceRef, r)]
            if repaired and change is not None:
                notes.append(f"{sid}: {change.before} {change.kind.lower()} to {change.after}")
            else:
                reason = change.after if change and not repaired else DropReason.MALFORMED
                label = change.before if change else "<reference>"
                notes.append(f"{sid}: {label} dropped ({reason})")
            kept += repaired
        notes += [
            f"{sid}: {_label(extra)} dropped ({DropReason.OVER_REF_LIMIT})"
            for extra in kept[MAX_SEGMENT_REFS:]
        ]
        if kept and notes:  # with no valid reference left, the parser names the real problem
            segment["source_refs"] = kept[:MAX_SEGMENT_REFS]
            changes += notes
    authors = segment.get("author_refs")
    if isinstance(authors, list) and authors:
        valid = [
            a
            for a in dict.fromkeys(a for a in authors if isinstance(a, str))
            if _AUTHOR_REF.match(a)
        ][:MAX_AUTHOR_REFS]
        if valid and valid != authors:
            segment["author_refs"] = valid
            changes.append(f"{sid}: author_refs {len(authors)} kept {len(valid)}")
    return changes


def _questions(questions: list) -> tuple[list, list[str]]:
    kept, changes = [], []
    for number, question in enumerate(questions, start=1):
        refs = question.get("source_refs") if isinstance(question, dict) else None
        if isinstance(refs, list):
            valid = [ref for ref in refs if _valid(SourceRef, ref)][:MAX_SEGMENT_REFS]
            if len(valid) != len(refs):
                question = {**question, "source_refs": valid}
                changes.append(f"question {number}: references {len(refs)} kept {len(valid)}")
        if not _valid(OpenQuestion, question):
            changes.append(f"question {number}: dropped (does not fit the schema)")
            continue
        kept.append(question)
    if len(kept) > MAX_QUESTIONS:
        changes.append(f"questions: {len(kept)} kept the first {MAX_QUESTIONS}")
        kept = kept[:MAX_QUESTIONS]
    return kept, changes
