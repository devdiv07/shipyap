"""Later records beat a plan: which draft sentences repeat a plan that a later record of the same
revision has superseded. Deterministic and pure: no model, no I/O.

A segment is judged only when every reference it cites is a PLAN file (`stories.roles`): a
sentence with code, test or result lines behind it is the fact check's to judge, not this one's.
Such a segment is OUTDATED when

- it cites a dated status line of the plan ("Status: ... zero real API requests made") and any
  later record exists for the same revision: the status was true when written, and the record
  shows that work went on after it; or
- a change record (a deviation, correction, erratum or retraction) names in its title an item
  that it also writes as code (in backticks), and both the segment and its cited plan lines
  name that item: the plan's version of that item is the one the record changed. Words of the
  record's own path ("deviation", "phase4") are not items.

Each result names the records that decide it, so the owner sees them beside the sentence. A
heuristic over file roles and names, never proof: no OUTDATED does not mean the sentence is right,
and the owner still reads every claim.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from shipyap.editorial.models import Brief, DraftResponse
from shipyap.stories.roles import role

OUTDATED = "OUTDATED"
STATUS_LINE = "STATUS_LINE"
CHANGED_ITEM = "CHANGED_ITEM"
_STATUS = re.compile(r"^\W*status\W*:", re.IGNORECASE)
_CHANGE_RECORD = re.compile(r"deviation|correct|errat|retract")
_TOKEN = re.compile(r"[a-z0-9]+")
_CODE = re.compile(r"`([^`\n]+)`")
_MIN_ITEM_CHARS = 4


@dataclass(frozen=True)
class Record:
    """One later (RESULT-role) file of the draft's revision."""

    path: str
    lines: tuple[str, ...]

    @property
    def title_line(self) -> int:
        return next((n for n, line in enumerate(self.lines, 1) if line.startswith("#")), 1)

    @property
    def label(self) -> str:
        return f"{self.path}:{self.title_line}"

    @property
    def title(self) -> str:
        return self.lines[self.title_line - 1].lstrip("#").strip() if self.lines else ""


@dataclass(frozen=True)
class Superseded:
    segment_id: str
    rule: str  # STATUS_LINE or CHANGED_ITEM
    item: str | None  # the changed item, for CHANGED_ITEM
    records: tuple[str, ...]  # "path:line" of each deciding record's title
    reason: str

    verdict: str = OUTDATED


def later_records(files: Iterable[tuple[str, Sequence[str]]]) -> tuple[Record, ...]:
    """The RESULT-role files among `files` (path, lines), one per path, sorted by path."""
    seen: dict[str, Record] = {}
    for path, lines in files:
        if role(path) == "RESULT" and path not in seen:
            seen[path] = Record(path, tuple(lines))
    return tuple(seen[path] for path in sorted(seen))


def check_superseded(
    brief: Brief, response: DraftResponse, records: Sequence[Record]
) -> tuple[Superseded, ...]:
    """The OUTDATED segments of `response`, with the records that decide each one. `records` are
    the later files of the brief's revision (the brief's own RESULT files are added)."""
    records = later_records(
        [(r.path, r.lines) for r in records] + [(s.path, s.lines) for s in brief.sources]
    )
    if not records:
        return ()
    changed = {
        record.label: (record, items) for record in records if (items := _changed_items(record))
    }
    found: list[Superseded] = []
    for segment in response.segments:
        refs = segment.source_refs
        if not refs or any(role(ref.path) != "PLAN" for ref in refs):
            continue
        cited: list[str] = []
        for ref in refs:
            source = brief.source(ref.path)
            if source is not None:
                cited += source.lines[ref.line_start - 1 : ref.line_end]
        if any(_STATUS.match(line) for line in cited):
            labels = tuple(record.label for record in records)
            found.append(
                Superseded(
                    segment.id,
                    STATUS_LINE,
                    None,
                    labels,
                    "It cites the plan's status line, which describes the work when the plan was "
                    "written; later records of this revision exist: "
                    + "; ".join(f"{r.label} ({r.title})" for r in records),
                )
            )
            continue
        words = _tokens(segment.text)
        plan_words = _tokens("\n".join(cited))
        for record, items in changed.values():
            hit = next((item for item in items if item in words and item in plan_words), None)
            if hit is not None:
                found.append(
                    Superseded(
                        segment.id,
                        CHANGED_ITEM,
                        hit,
                        (record.label,),
                        f"It repeats the plan's '{hit}', which a later record changed: "
                        f"{record.label} ({record.title})",
                    )
                )
                break
    return tuple(found)


def _changed_items(record: Record) -> tuple[str, ...]:
    if not _CHANGE_RECORD.search(record.path.lower()):
        return ()
    own = _tokens(record.path)
    code = {
        token for line in record.lines for span in _CODE.findall(line) for token in _tokens(span)
    }
    items = [
        word
        for word in _TOKEN.findall(record.title.lower())
        if len(word) >= _MIN_ITEM_CHARS and not word.isdigit() and word not in own and word in code
    ]
    return tuple(dict.fromkeys(items))


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))
