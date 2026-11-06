"""Buffer's LinkedIn text-length rule, from Buffer's developer guide "Character Limits".

Documented there (read 2026-10-21): LinkedIn post text is limited to 3,000; length is counted in
UTF-16 code units (JavaScript `string.length`), so characters outside the Basic Multilingual
Plane, such as most emoji and the bold/italic Unicode letters, count 2; text is not normalised
(a precomposed "é" is 1, "e" plus a combining accent is 2); every URL counts a fixed 24.

NOT documented there, so this module is deliberately conservative (never lower than Buffer):
- what Buffer recognises as a URL. A `http(s)://` or `www.` token counts exactly 24. A bare
  domain-like token (`example.com/x`) might or might not be one for Buffer, so it counts
  max(24, its own length). Trailing sentence punctuation is counted as ordinary text.
- whether line breaks, mentions or hashtags cost extra. They are counted as plain characters.
The rule has not been compared with Buffer's server (UNVERIFIED); Buffer remains the authority and
rejects an over-long post itself.
"""

import re
import unicodedata

from public_research_os.publication.errors import PublicationError, PublicationErrorCode

LINKEDIN_MAX_UNITS = 3000
URL_UNITS = 24

_SCHEME_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_BARE_DOMAIN = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b(?:/\S*)?", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""
# Newline is the only control character a post may carry (paragraph breaks).
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def utf16_units(text: str) -> int:
    try:
        return len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError:  # a lone surrogate
        raise PublicationError(PublicationErrorCode.TEXT_NOT_PUBLISHABLE) from None


def linkedin_units(text: str) -> int:
    """Buffer's LinkedIn length of `text`, never lower than the documented rule."""
    total = 0
    position = 0
    for match in _tokens(text):
        total += utf16_units(text[position : match.start()])
        token, fixed = _split_trailing(match.group())
        own = utf16_units(token)
        total += URL_UNITS if fixed else max(URL_UNITS, own)
        total += utf16_units(match.group()[len(token) :])
        position = match.end()
    return total + utf16_units(text[position:])


def _tokens(text: str) -> list[re.Match[str]]:
    """Scheme URLs first; a bare domain only where no scheme URL already covers it."""
    found = list(_SCHEME_URL.finditer(text))
    covered = [(m.start(), m.end()) for m in found]
    for match in _BARE_DOMAIN.finditer(text):
        if not any(start <= match.start() < end for start, end in covered):
            found.append(match)
    return sorted(found, key=lambda m: m.start())


def _split_trailing(token: str) -> tuple[str, bool]:
    """(token without trailing punctuation, True when it is a scheme URL)."""
    fixed = _SCHEME_URL.fullmatch(token) is not None
    return token.rstrip(_TRAILING), fixed


def check_publishable_text(text: str) -> None:
    """Refuse text a reviewer could not see completely, then text over the limit."""
    if not text or not text.strip() or text != text.strip():
        raise PublicationError(PublicationErrorCode.TEXT_NOT_PUBLISHABLE)
    for char in text:
        if char != "\n" and unicodedata.category(char) in _HIDDEN:
            raise PublicationError(PublicationErrorCode.TEXT_NOT_PUBLISHABLE)
    utf16_units(text)
    if linkedin_units(text) > LINKEDIN_MAX_UNITS:
        raise PublicationError(PublicationErrorCode.TEXT_TOO_LONG)
