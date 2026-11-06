"""Step 6: the X text of a reviewed draft (`review.x_post`) and X's length rule (`publication.x`).

Offline and model-free. The replay tests run the rule on the stored real drafts in `evals/` and
on the Step 4 draft as auto-repair leaves it (its scope line added for free).
"""

import pytest

from public_research_os.publication.errors import PublicationError, PublicationErrorCode
from public_research_os.publication.x import X_MAX_WEIGHT, check_x_text, x_weight
from public_research_os.review.x_post import sentence_ends


def test_x_counts_wide_characters_and_emoji_twice_and_a_url_at_least_23():
    assert x_weight("plain ascii text") == 16
    assert x_weight("“quoted” — dash") == 15  # punctuation ranges count 1
    assert x_weight("漢字") == 4
    assert x_weight("\U0001f600") == 2
    assert x_weight("see https://a.co now") == len("see  now") + 23
    long_url = "https://github.com/sample-owner/retry-lab/blob/main/README.md"
    assert x_weight(long_url) == len(long_url)  # never lower than its own length
    assert x_weight("repo github.com/x") == 5 + 23


def test_280_weighted_characters_pass_and_281_are_too_long():
    check_x_text("a" * X_MAX_WEIGHT)
    with pytest.raises(PublicationError) as raised:
        check_x_text("a" * (X_MAX_WEIGHT + 1))
    assert raised.value.code == PublicationErrorCode.TEXT_TOO_LONG
    with pytest.raises(PublicationError):
        check_x_text("漢" * 141)  # 282 weighted


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("One claim. Two claims.", ["One claim.", "Two claims."]),
        (
            "It ran 3.5 times on v0.1. Then it stopped.",
            ["It ran 3.5 times on v0.1.", "Then it stopped."],
        ),
        (
            "Use a fake, e.g. FakeTransport. It records.",
            ["Use a fake, e.g. FakeTransport.", "It records."],
        ),
        ("Attempt 2 passed. 3 attempts ran.", ["Attempt 2 passed.", "3 attempts ran."]),
        ('It said "stop." Then nothing.', ['It said "stop."', "Then nothing."]),
        ("Is it? Yes! Done.", ["Is it?", "Yes!", "Done."]),
        ("a lowercase. start stays one", ["a lowercase. start stays one"]),
    ],
)
def test_sentences_end_at_terminal_punctuation_but_not_inside_numbers_or_abbreviations(text, parts):
    starts = [0, *sentence_ends(text)]
    assert [text[a:b].strip() for a, b in zip(starts, starts[1:], strict=False)] == parts
