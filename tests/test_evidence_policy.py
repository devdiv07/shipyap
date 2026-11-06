import os
from pathlib import PurePosixPath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import HttpUrl

from public_research_os.evidence.policy import (
    PathViolation,
    UrlViolation,
    check_relative_path,
    check_source_url,
)

UNSAFE_PATHS = {
    "../x": PathViolation.BAD_SEGMENT,
    "a/../../x": PathViolation.BAD_SEGMENT,
    "./x": PathViolation.BAD_SEGMENT,
    "a//b": PathViolation.BAD_SEGMENT,
    "a/": PathViolation.BAD_SEGMENT,
    ".": PathViolation.BAD_SEGMENT,
    "": PathViolation.EMPTY,
    "/etc/passwd": PathViolation.ABSOLUTE,
    "C:/Windows/win.ini": PathViolation.FORBIDDEN_CHARACTER,
    "C:x": PathViolation.FORBIDDEN_CHARACTER,
    "file.txt:stream": PathViolation.FORBIDDEN_CHARACTER,
    "a\\b": PathViolation.FORBIDDEN_CHARACTER,
    "\\\\server\\share\\x": PathViolation.FORBIDDEN_CHARACTER,
    "x\x00y": PathViolation.FORBIDDEN_CHARACTER,
    "x\ny": PathViolation.FORBIDDEN_CHARACTER,
    "x\u202ey": PathViolation.FORBIDDEN_CHARACTER,  # bidi override
    "x?.json": PathViolation.FORBIDDEN_CHARACTER,
    "x*.json": PathViolation.FORBIDDEN_CHARACTER,
    "e\u0301.json": PathViolation.NOT_NFC,
    "CON": PathViolation.RESERVED_NAME,
    "con": PathViolation.RESERVED_NAME,
    "nul.txt": PathViolation.RESERVED_NAME,
    "a/Com1.json": PathViolation.RESERVED_NAME,
    "LPT9": PathViolation.RESERVED_NAME,
    "aux.tar.gz": PathViolation.RESERVED_NAME,
    "COM\u00b9": PathViolation.RESERVED_NAME,
    "x.": PathViolation.TRAILING_DOT_OR_SPACE,
    "x ": PathViolation.TRAILING_DOT_OR_SPACE,
    "dir./f": PathViolation.TRAILING_DOT_OR_SPACE,
    "dir /f": PathViolation.TRAILING_DOT_OR_SPACE,
    "a" * 1025: PathViolation.TOO_LONG,
}


@pytest.mark.parametrize(("value", "violation"), UNSAFE_PATHS.items(), ids=repr)
def test_unsafe_paths_rejected(value, violation):
    assert check_relative_path(value) == violation


@pytest.mark.parametrize(
    "value", ["results.json", "data/run-01/results.json", ".github/workflows/ci.yml", "console.txt"]
)
def test_safe_paths_accepted(value):
    assert check_relative_path(value) is None


@settings(max_examples=500, deadline=None, database=None)  # database=None: no files written
@given(st.text(min_size=1, max_size=40))
def test_accepted_paths_never_leave_root_lexically(value):
    if check_relative_path(value) is not None:
        return
    assert not PurePosixPath(value).is_absolute()
    root = os.path.abspath("snapshot-root")
    joined = os.path.normpath(os.path.join(root, *value.split("/")))
    assert os.path.commonpath([root, joined]) == root and joined != root


@pytest.mark.parametrize(
    ("url", "violation"),
    [
        ("https://user:SENTINELtok@example.org/x", UrlViolation.CREDENTIALS),
        ("https://SENTINELtok@example.org/x", UrlViolation.CREDENTIALS),
        ("https://example.org/x?token=SENTINEL", UrlViolation.QUERY),
        ("https://example.org/x?", UrlViolation.QUERY),
        ("https://example.org/x#SENTINEL", UrlViolation.FRAGMENT),
        ("https://example.org/x#", UrlViolation.FRAGMENT),
        ("http://example.org/x", UrlViolation.NOT_HTTPS),
    ],
)
def test_url_policy(url, violation):
    assert check_source_url(HttpUrl(url)) == violation


def test_plain_https_url_accepted():
    assert check_source_url(HttpUrl("https://github.com/org/repo")) is None
