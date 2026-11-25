"""Rotation memory (C2a): which post format and repository the last posts used, so the same
format never runs twice in a row and no two consecutive posts come from the same repository.

C2b: the drafting loop reads it for voice dry-nerdy (`engine._rotation_arguments`): the next
draft prefers another repository and the other format, and each new dry-nerdy draft is recorded
with the format it actually has. The file is
`<state>/rotation.json`, small JSON, written whole through a temporary file and a rename.
The formats per voice come from `editorial/voice_lists.toml` ([formats]).
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from shipyap.editorial.voice import lists

ROTATION_FILE = "rotation.json"
ROTATION_VERSION = "1"
KEEP = 50  # entries kept; only the last one decides, the rest is history for the owner
_REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
_STORY = re.compile(r"[0-9a-f]{16,64}")


class RotationError(Exception):
    """Fixed codes only: ROTATION_UNREADABLE, ENTRY_INVALID, FORMAT_UNKNOWN, FORMAT_REPEATED,
    REPO_REPEATED."""


@dataclass(frozen=True)
class Entry:
    story_id: str
    repo: str
    format: str
    voice: str


def formats(voice: str) -> tuple[str, ...]:
    found = lists().formats.get(voice)
    if not found:
        raise RotationError("FORMAT_UNKNOWN")
    return found


@dataclass(frozen=True)
class Rotation:
    entries: tuple[Entry, ...] = ()

    @property
    def last(self) -> Entry | None:
        return self.entries[-1] if self.entries else None

    def next_format(self, voice: str) -> str:
        """The format after the last one in this voice's list, wrapping round; never the last
        post's format (a voice with one format cannot rotate: FORMAT_REPEATED)."""
        choices = formats(voice)
        last = self.last
        if last is None:
            return choices[0]
        start = choices.index(last.format) + 1 if last.format in choices else 0
        for offset in range(len(choices)):
            pick = choices[(start + offset) % len(choices)]
            if pick != last.format:
                return pick
        raise RotationError("FORMAT_REPEATED")

    def repo_allowed(self, repo: str) -> bool:
        """False when the last post came from `repo`."""
        return self.last is None or self.last.repo != repo

    def refusal(self, entry: Entry) -> str | None:
        """Why `entry` may not follow the last post, or None."""
        if entry.format not in formats(entry.voice):
            return "FORMAT_UNKNOWN"
        last = self.last
        if last is not None and last.format == entry.format:
            return "FORMAT_REPEATED"
        if not self.repo_allowed(entry.repo):
            return "REPO_REPEATED"
        return None


def load(state: Path) -> Rotation:
    """The rotation memory under `state`; a missing file is an empty memory."""
    path = state / ROTATION_FILE
    if not path.exists():
        return Rotation()
    try:
        value = json.loads(path.read_text("utf-8"))
        if value.get("rotation_version") != ROTATION_VERSION:
            raise ValueError
        entries = tuple(_entry(item) for item in value["entries"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RotationError):
        raise RotationError("ROTATION_UNREADABLE") from None
    return Rotation(entries)


def record(state: Path, entry: Entry, allow_repeat: bool = False) -> Rotation:
    """Append `entry` after checking it against the last post; raises RotationError with the
    refusal code and writes nothing when it breaks the rotation. `allow_repeat`: a repeated
    format or repository is recorded anyway (the drafting loop had no other choice); an unknown
    format is still refused."""
    try:
        _entry(dict(entry.__dict__))
    except (ValueError, RotationError):
        raise RotationError("ENTRY_INVALID") from None
    current = load(state)
    reason = current.refusal(entry)
    if reason is not None and not (allow_repeat and reason in ("FORMAT_REPEATED", "REPO_REPEATED")):
        raise RotationError(reason)
    updated = Rotation((*current.entries, entry)[-KEEP:])
    document = {
        "rotation_version": ROTATION_VERSION,
        "entries": [e.__dict__ for e in updated.entries],
    }
    state.mkdir(parents=True, exist_ok=True)
    temporary = state / f".{ROTATION_FILE}.tmp"
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, state / ROTATION_FILE)
    return updated


def _entry(item: object) -> Entry:
    if not isinstance(item, dict) or set(item) != {"story_id", "repo", "format", "voice"}:
        raise ValueError
    entry = Entry(**{key: item[key] for key in ("story_id", "repo", "format", "voice")})
    if not all(isinstance(v, str) for v in entry.__dict__.values()):
        raise ValueError
    if not _STORY.fullmatch(entry.story_id) or not _REPO.fullmatch(entry.repo):
        raise ValueError
    if entry.format not in formats(entry.voice):
        raise ValueError
    return entry
