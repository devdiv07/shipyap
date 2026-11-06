"""Buffer's LinkedIn length rule: UTF-16 code units, URLs a fixed 24, limit 3000."""

import pytest

from shipyap.publication.errors import PublicationError, PublicationErrorCode
from shipyap.publication.linkedin import (
    LINKEDIN_MAX_UNITS,
    URL_UNITS,
    check_publishable_text,
    linkedin_units,
    utf16_units,
)

E = PublicationErrorCode


def code_of(text: str) -> PublicationErrorCode:
    with pytest.raises(PublicationError) as raised:
        check_publishable_text(text)
    return raised.value.code


def test_the_boundary_is_exactly_3000_units():
    check_publishable_text("a" * LINKEDIN_MAX_UNITS)
    assert code_of("a" * (LINKEDIN_MAX_UNITS + 1)) == E.TEXT_TOO_LONG


def test_characters_outside_the_bmp_count_two_units():
    assert utf16_units("\U0001d5d4") == 2  # a mathematical bold letter, as LinkedIn users paste
    assert utf16_units("\U0001f600") == 2  # an emoji
    check_publishable_text("\U0001f600" * 1500)  # 3000 units
    assert code_of("\U0001f600" * 1501) == E.TEXT_TOO_LONG
    assert code_of("\U0001f600" * 1499 + "ab" + "c") == E.TEXT_TOO_LONG  # 2998 + 3


def test_text_is_not_normalised_before_counting():
    assert linkedin_units("é") == 1  # precomposed
    assert linkedin_units("é") == 2  # e plus a combining accent


def test_every_url_counts_a_fixed_24_whatever_its_length():
    short = "https://a.io"
    long = "https://example.org/" + "x" * 500
    assert linkedin_units(short) == URL_UNITS
    assert linkedin_units(long) == URL_UNITS
    assert linkedin_units(f"see {long} now") == 4 + URL_UNITS + 4
    assert linkedin_units("www.example.org/path") == URL_UNITS


def test_trailing_punctuation_after_a_url_is_ordinary_text():
    assert linkedin_units("https://example.org/x.") == URL_UNITS + 1
    assert linkedin_units("(https://example.org/x)") == 1 + URL_UNITS + 1


def test_a_bare_domain_is_never_counted_below_24():
    """Whether Buffer treats it as a URL is undocumented; counting it as one cannot undercount."""
    assert linkedin_units("a.io") == URL_UNITS
    assert linkedin_units("example.org/" + "y" * 100) == len("example.org/" + "y" * 100)
    assert linkedin_units("version 3.14 is fine") == len("version 3.14 is fine")


def test_a_url_heavy_post_is_limited_by_the_fixed_count():
    urls = " ".join(["https://example.org/" + "z" * 60] * 100)  # raw length far above 3000
    assert len(urls) > LINKEDIN_MAX_UNITS
    assert linkedin_units(urls) == 100 * URL_UNITS + 99
    check_publishable_text(urls)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        " leading",
        "trailing ",
        "trailing\n",
        "tab\there",
        "bidi ‮override",
        "zero​width",
        "line separator",
        "nul\x00byte",
        "lone \ud800 surrogate",
    ],
)
def test_text_a_reviewer_cannot_see_completely_is_refused(text):
    assert code_of(text) == E.TEXT_NOT_PUBLISHABLE


def test_paragraph_breaks_are_allowed():
    check_publishable_text("First paragraph.\n\nSecond paragraph.")
