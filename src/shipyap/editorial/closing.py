"""The closing line (C2c): the repository link, written by the tool after drafting, never by a model.

A post may end with one extra paragraph, the segment CLOSING_ID: the repository's URL alone, or,
for a repository the owner marked `built_by_me = true` in pros.toml, the owner's wording from the
author style file around it ("Built this one myself: <url>" by default). It is FRAMING with no
references and it counts toward the post's length. The checker does not judge it (it states
nothing about the code, and its first person is the owner's own configured wording), so it is
recognised only by its exact shape: the reserved id, the last segment, no references, and text
that ends with the brief's own repository URL after at most MAX_LEAD characters of lead-in.

Before the tool adds its line it removes any segment that carries the reserved id, and a model
segment at the end that is the bare repository URL, so a model can neither write the closing
itself nor leave the link twice. Pure: no files, no environment.
"""

import json
import re

from shipyap.evidence.report import canonical_json

CLOSING_ID = "S99"
URL_FIELD = "{url}"
DEFAULT_BUILT_BY_ME = "Built this one myself: {url}"
MAX_LEAD = 100
# In the author style file: one line "Built-by-me closing: <wording with {url} at its end>".
# The line is taken out of the style text before the prompt sees it.
_DIRECTIVE = re.compile(r"^[ \t]*built-by-me closing:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.M)
_GITHUB_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


def split_style(text: str) -> tuple[str, str]:
    """(the style text without the closing directive, the built-by-me wording). Without a
    directive the text is returned unchanged with the default wording. ValueError: more than one
    directive, or a wording that is not one line ending in {url} with a short lead-in."""
    found = list(_DIRECTIVE.finditer(text))
    if not found:
        return text, DEFAULT_BUILT_BY_ME
    if len(found) > 1:
        raise ValueError("more than one built-by-me closing line")
    wording = found[0].group(1)
    check_wording(wording)
    match = found[0]
    rest = re.sub(r"\n{3,}", "\n\n", text[: match.start()] + text[match.end() :]).strip()
    return rest, wording


def check_wording(wording: str) -> None:
    lead = wording.removesuffix(URL_FIELD)
    if (
        not wording.endswith(URL_FIELD)
        or URL_FIELD in lead
        or not lead.strip()
        or len(lead) > MAX_LEAD
        or any(c in lead for c in "{}\n\r")
        or "http" in lead.casefold()
    ):
        raise ValueError("the built-by-me closing must be one short line ending in {url}")


def closing_text(source_url: str, built_by_me: bool, wording: str = DEFAULT_BUILT_BY_ME) -> str:
    """The closing line for a repository: its URL, or the owner's wording around it."""
    if not _GITHUB_URL.fullmatch(source_url):
        raise ValueError("not a github.com repository URL")
    if not built_by_me:
        return source_url
    check_wording(wording)
    return wording.removesuffix(URL_FIELD) + source_url


def is_closing(segment, source_url: str | None = None) -> bool:
    """`segment` has the closing line's shape. With `source_url`, it must end with that URL;
    without, with any github.com repository URL (for callers that hold no brief)."""
    if segment.id != CLOSING_ID or segment.source_refs or segment.author_refs:
        return False
    if str(segment.basis) != "FRAMING":
        return False
    text = segment.text
    if source_url is None:
        match = _GITHUB_URL.search(text)
        if match is None or match.end() != len(text):
            return False
        source_url = match.group()
    if not text.endswith(source_url):
        return False
    lead = text[: -len(source_url)]
    return len(lead) <= MAX_LEAD and "\n" not in lead and "http" not in lead.casefold()


def add_closing(raw: bytes, text: str | None, source_url: str, max_segments: int) -> bytes:
    """The response bytes with the tool's closing line as the last segment. `text` None: `raw`
    unchanged. A NO_POST reply, or a post already at `max_segments`, gets no closing."""
    if text is None:
        return raw
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw  # the strict parser refuses it, closing or not
    segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segments, list) or not segments:
        return raw
    kept = [s for s in segments if not (isinstance(s, dict) and s.get("id") == CLOSING_ID)]
    last = kept[-1] if kept else None
    if (
        isinstance(last, dict)
        and last.get("basis") == "FRAMING"
        and not last.get("source_refs")
        and not last.get("author_refs")
        and str(last.get("text", "")).strip().rstrip(".") == source_url
    ):
        kept.pop()
    if not kept or len(kept) >= max_segments:
        return raw
    kept.append(
        {
            "id": CLOSING_ID,
            "paragraph_break_before": True,
            "text": text,
            "basis": "FRAMING",
            "source_refs": [],
            "author_refs": [],
        }
    )
    data["segments"] = kept
    return canonical_json(data).encode("utf-8")
