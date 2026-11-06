"""Buffer's X (Twitter) text-length rule, from Buffer's developer guide "Character Limits".

Documented there (read 2026-11-03): a free X account allows 280 (Basic, Premium and Premium+
25,000); the count is X's own weighted count, where URLs are 23 and emoji are 2. The owner keeps
280 for every account (decision 2026-11-03), so a Premium account is never relied on.

X's weighting (its open `twitter-text` configuration): a code point in U+0000-U+10FF,
U+2000-U+200D, U+2010-U+201F or U+2032-U+2037 counts 1, every other code point 2.

NOT documented, so this module is deliberately conservative (never lower than X):
- a URL counts max(23, its own weighted length), scheme URL or bare domain alike (X counts 23,
  but what X recognises as a URL is not documented here);
- every code point of an emoji sequence (joiners and modifiers included) counts 2, where X
  counts the whole sequence 2;
- the text is not normalised first (X counts after NFC, which can only shorten it).
Buffer remains the authority and refuses an over-long post itself.
"""

from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.linkedin import _tokens, check_publishable_text

X_MAX_WEIGHT = 280
X_URL_WEIGHT = 23
_LIGHT = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))


def _weight(text: str) -> int:
    return sum(1 if any(lo <= ord(c) <= hi for lo, hi in _LIGHT) else 2 for c in text)


def x_weight(text: str) -> int:
    """X's weighted length of `text`, never lower than X's own count."""
    total = 0
    position = 0
    for match in _tokens(text):
        total += _weight(text[position : match.start()])
        total += max(X_URL_WEIGHT, _weight(match.group()))
        position = match.end()
    return total + _weight(text[position:])


def check_x_text(text: str) -> None:
    """The LinkedIn rules for what a reviewer can see, then X's 280."""
    check_publishable_text(text)
    if x_weight(text) > X_MAX_WEIGHT:
        raise PublicationError(PublicationErrorCode.TEXT_TOO_LONG)
