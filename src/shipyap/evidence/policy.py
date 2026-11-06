"""Pure, portable policies for untrusted source URLs and repository-relative paths.

No I/O. Violations are reported as fixed codes and messages that never echo the input,
so a rejected value (which may contain a credential) cannot leak into reports or logs.
"""

import unicodedata
from enum import StrEnum

from pydantic import HttpUrl

MAX_PATH_LENGTH = 1024


class UrlViolation(StrEnum):
    NOT_HTTPS = "URL_NOT_HTTPS"
    CREDENTIALS = "URL_HAS_CREDENTIALS"
    QUERY = "URL_HAS_QUERY"
    FRAGMENT = "URL_HAS_FRAGMENT"


class PathViolation(StrEnum):
    EMPTY = "PATH_EMPTY"
    TOO_LONG = "PATH_TOO_LONG"
    NOT_NFC = "PATH_NOT_NFC"
    FORBIDDEN_CHARACTER = "PATH_FORBIDDEN_CHARACTER"
    ABSOLUTE = "PATH_ABSOLUTE"
    BAD_SEGMENT = "PATH_BAD_SEGMENT"
    RESERVED_NAME = "PATH_RESERVED_NAME"
    TRAILING_DOT_OR_SPACE = "PATH_TRAILING_DOT_OR_SPACE"


URL_MESSAGES: dict[UrlViolation, str] = {
    UrlViolation.NOT_HTTPS: "source URL must use HTTPS",
    UrlViolation.CREDENTIALS: "source URL must not contain credentials",
    UrlViolation.QUERY: "source URL must not contain a query string",
    UrlViolation.FRAGMENT: "source URL must not contain a fragment",
}

PATH_MESSAGES: dict[PathViolation, str] = {
    PathViolation.EMPTY: "source path must not be empty",
    PathViolation.TOO_LONG: f"source path must be at most {MAX_PATH_LENGTH} characters",
    PathViolation.NOT_NFC: "source path must be NFC-normalized Unicode",
    PathViolation.FORBIDDEN_CHARACTER: "source path contains a forbidden character",
    PathViolation.ABSOLUTE: "source path must be relative",
    PathViolation.BAD_SEGMENT: "source path must not contain empty, '.' or '..' segments",
    PathViolation.RESERVED_NAME: "source path must not contain a Windows reserved device name",
    PathViolation.TRAILING_DOT_OR_SPACE: "source path segments must not end with '.' or ' '",
}

# Rejected on every OS so a manifest means the same thing on Windows and POSIX hosts.
# ':' covers drive letters and NTFS alternate data streams; '\\' covers Windows separators.
_FORBIDDEN_CHARACTERS = frozenset('\\:<>"|?*')

_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{prefix}{suffix}" for prefix in ("COM", "LPT") for suffix in "0123456789¹²³"}
)


def check_source_url(url: HttpUrl) -> UrlViolation | None:
    """Canonical source URLs are plain HTTPS locations; they are never fetched."""
    if url.scheme != "https":
        return UrlViolation.NOT_HTTPS
    if url.username is not None or url.password is not None:
        return UrlViolation.CREDENTIALS
    if url.query is not None:
        return UrlViolation.QUERY
    if url.fragment is not None:
        return UrlViolation.FRAGMENT
    return None


def check_relative_path(value: str) -> PathViolation | None:
    """Portable repository-relative POSIX path policy; stricter than EvidenceSource."""
    if not value:
        return PathViolation.EMPTY
    if len(value) > MAX_PATH_LENGTH:
        return PathViolation.TOO_LONG
    if unicodedata.normalize("NFC", value) != value:
        return PathViolation.NOT_NFC
    if any(char in _FORBIDDEN_CHARACTERS or _is_control(char) for char in value):
        return PathViolation.FORBIDDEN_CHARACTER
    if value.startswith("/"):
        return PathViolation.ABSOLUTE
    for segment in value.split("/"):
        if segment in {"", ".", ".."}:
            return PathViolation.BAD_SEGMENT
        if segment.endswith((".", " ")):
            return PathViolation.TRAILING_DOT_OR_SPACE
        if segment.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_DEVICE_NAMES:
            return PathViolation.RESERVED_NAME
    return None


def _is_control(char: str) -> bool:
    # Cs: lone surrogates, which strict JSON decoding can produce from "\ud800" escapes.
    return unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
