"""The story-finding normaliser: it may only narrow or drop, never add. Synthetic data only."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from stories_fake import ref

from shipyap.drafting.normalise import ChangeKind, DropReason, normalise_finding
from shipyap.editorial.grounding import number_tokens
from shipyap.stories.findings import StoryFinding

LINES = tuple(f"line {n} of the notes." for n in range(1, 41))
LINES = (*LINES[:19], "Retries are capped", "at 2 per task.", *LINES[21:])  # lines 20-21
FILES = {"A.md": LINES, "B.md": ("The pilot used 453 trials.", "Plain words here.")}
SENT = {"A.md": 40, "B.md": 2}


def finding(*refs, angle=None, support=None) -> dict:
    return {
        "story_key": "retry-cap",
        "angle": angle or "Capping retries kept the evaluation runs comparable.",
        "support": support or "The notes state the cap on retries per task directly.",
        "source_refs": list(refs),
    }


def run(candidate, files=FILES, supplied=SENT):
    return normalise_finding(candidate, files, supplied)


def test_an_over_long_quoted_reference_is_narrowed_to_its_quote():
    result = run(finding(ref("A.md", 5, 35, "Retries are capped at 2 per task")))
    (narrowed,) = result.candidate["source_refs"]
    assert (narrowed["line_start"], narrowed["line_end"]) == (20, 21)
    assert [c.kind for c in result.changes] == [ChangeKind.NARROWED]
    StoryFinding.model_validate(result.candidate)


def test_an_over_long_unquoted_reference_is_split_into_valid_refs_covering_the_range():
    result = run(finding(ref("A.md", 3, 27)))
    spans = [(r["line_start"], r["line_end"]) for r in result.candidate["source_refs"]]
    assert spans == [(3, 12), (13, 22), (23, 27)]
    assert result.changes[0].kind == ChangeKind.SPLIT
    StoryFinding.model_validate(result.candidate)


def test_a_bad_reference_is_dropped_while_a_valid_one_remains():
    good = ref("A.md", 20, 21, "Retries are capped")
    bad = [
        (ref("NOPE.md", 1, 1), DropReason.PATH_NOT_IN_SNAPSHOT),
        (ref("A.md", 39, 45), DropReason.RANGE_INVALID),
        (ref("A.md", 1, 2, "words that are not there"), DropReason.QUOTE_NOT_FOUND),
        ("not a reference", DropReason.MALFORMED),
    ]
    for item, reason in bad:
        result = run(finding(item, good))
        assert result.candidate["source_refs"] == [good]
        assert [(c.kind, c.after) for c in result.changes] == [(ChangeKind.REF_DROPPED, reason)]


def test_lines_that_were_not_sent_are_dropped_as_a_reference():
    good = ref("A.md", 20, 21)
    result = run(finding(ref("A.md", 30, 31), good), supplied={"A.md": 25, "B.md": 2})
    assert result.candidate["source_refs"] == [good]
    assert result.changes[0].after == DropReason.NOT_SUPPLIED


def test_with_no_valid_reference_left_the_candidate_is_untouched_for_validation():
    candidate = finding(ref("NOPE.md", 1, 1), ref("A.md", 1, 2, "absent words"))
    result = run(candidate)
    assert result.candidate is candidate and result.changes == ()


def test_splitting_never_exceeds_four_references():
    result = run(finding(ref("A.md", 1, 40), ref("B.md", 1, 2)))
    assert len(result.candidate["source_refs"]) == 4
    dropped = [c for c in result.changes if c.kind == ChangeKind.REF_DROPPED]
    assert [c.after for c in dropped] == [DropReason.OVER_REF_LIMIT]
    StoryFinding.model_validate(result.candidate)


def test_a_sentence_with_a_number_outside_its_quote_becomes_a_review_question():
    support = "The notes state the cap on retries directly. It cut wall time by 40 percent."
    result = run(finding(ref("A.md", 20, 21, "Retries are capped at 2"), support=support))
    assert result.candidate["support"] == "The notes state the cap on retries directly."
    (change,) = result.changes
    assert change.kind == ChangeKind.SENTENCE_DROPPED and change.numbers == ("40",)
    assert "40" in result.review_questions[0]
    StoryFinding.model_validate(result.candidate)


def test_a_number_in_the_cited_lines_but_not_in_the_quote_is_not_evidence():
    angle = "That kept runs comparable. Retries are capped at 2 per task."
    result = run(finding(ref("A.md", 19, 21, "Retries are capped"), angle=angle))
    assert result.candidate["angle"] == "That kept runs comparable."
    assert result.changes[0].numbers == ("2",)


def test_a_quote_cut_out_of_a_larger_number_does_not_support_it():
    angle = "The notes record the trial count. The pilot used 45 trials."
    result = run(finding(ref("B.md", 1, 1, "used 45"), angle=angle))
    assert result.candidate["angle"] == "The notes record the trial count."
    assert result.changes[0].numbers == ("45",)


def test_an_unsupported_number_in_the_angles_first_sentence_refuses_the_finding():
    """The first sentence is the main claim: dropping it would pass an angle without it."""
    angle = "Retries are capped at 2 per task. That kept runs comparable."
    result = run(finding(ref("A.md", 19, 21, "Retries are capped"), angle=angle))
    assert result.core_claim_dropped
    assert result.candidate["angle"] == angle  # never edited; screening refuses it
    assert result.changes[0].numbers == ("2",)  # shown, so the owner sees why


def test_an_unquoted_reference_supports_the_numbers_in_its_lines():
    angle = "The pilot used 453 trials. The notes record the trial count."
    result = run(finding(ref("B.md", 1, 2), angle=angle))
    assert result.changes == () and result.candidate["angle"] == angle


@pytest.mark.parametrize(
    "support",
    ["The pilot ran 99 trials in total, by design.", "We had 99 trials. Short one."],
)
def test_no_sentence_is_dropped_when_a_text_would_become_too_short(support):
    result = run(finding(ref("B.md", 2, 2), support=support))
    assert result.candidate["support"] == support  # left for validation to refuse
    assert not [c for c in result.changes if c.kind == ChangeKind.SENTENCE_DROPPED]


def test_inputs_that_are_not_findings_are_returned_unchanged():
    for candidate in (None, "text", [], {"story_key": "k"}, {"source_refs": "A.md"}):
        assert run(candidate).candidate is candidate


_WORDS = st.sampled_from(["cap", "retries", "2", "45", "453", "per", "task.", "runs", "7."])


@settings(max_examples=200, deadline=None)
@given(
    start=st.integers(-2, 45),
    length=st.integers(0, 45),
    quote=st.one_of(st.none(), st.sampled_from(["Retries are capped", "at 2", "absent", ""])),
    angle=st.lists(_WORDS, min_size=3, max_size=14).map(" ".join),
)
def test_the_normaliser_never_adds_evidence_lines_or_numbers(start, length, quote, angle):
    candidate = finding(ref("A.md", start, start + length, quote), angle=angle)
    result = run(candidate)
    before = candidate["source_refs"][0]
    for after in result.candidate["source_refs"]:
        assert after["path"] == before["path"] and after.get("quote") == before.get("quote")
        assert before["line_start"] <= after["line_start"] <= after["line_end"]
        assert after["line_end"] <= before["line_end"]
    for field in ("angle", "support"):
        assert set(number_tokens(result.candidate[field])) <= set(number_tokens(candidate[field]))
        assert set(result.candidate[field].split()) <= set(candidate[field].split())
