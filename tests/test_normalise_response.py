"""The draft-response normaliser: every schema limit a model-written reply can break is either
repaired by narrowing, splitting or dropping, or still refuses the reply. Synthetic data only."""

import json
from pathlib import Path

import pytest
from editorial_fake import ref, response, seg

from shipyap.drafting.normalise import normalise_response
from shipyap.editorial.errors import EditorialError, EditorialErrorCode
from shipyap.editorial.inputs import parse_response

SHA = "a" * 64
LINES = tuple(f"line {n} of the record." for n in range(1, 41))
LINES = (*LINES[:19], "Exactly one parameter", "is removed.", *LINES[21:])  # lines 20-21
FILES = {"A.md": LINES}
MODEL = {"kind": "external_model", "label": "claude-sonnet-5-5 via draft-model p3"}
REPO = Path(__file__).resolve().parents[1]


def model_response(*segments, **overrides) -> dict:
    segments = segments or (
        seg("S01", "One parameter is removed.", "SOURCE", src=[ref("A.md", 20, 21)]),
    )
    return response(SHA, segments, generator=MODEL, **overrides)


def run(data: dict, files=FILES):
    return normalise_response(json.dumps(data).encode(), files)


def refused(data: bytes) -> tuple[str, ...]:
    with pytest.raises(EditorialError) as caught:
        parse_response(data)
    assert caught.value.code == EditorialErrorCode.RESPONSE_INVALID
    return caught.value.details


def refs_of(data: bytes, index: int = 0) -> list[tuple[int, int, str | None]]:
    segment = json.loads(data)["segments"][index]
    return [(r["line_start"], r["line_end"], r.get("quote")) for r in segment["source_refs"]]


# --- MAX_REF_LINES (the live failure) ------------------------------------------------------------


def test_an_over_long_quoted_segment_reference_is_narrowed_to_its_quote():
    data = model_response(
        seg(
            "S04",
            "Exactly one parameter is removed.",
            "SOURCE",
            src=[ref("A.md", 15, 33, "Exactly one parameter is removed.")],
        )
    )
    assert refused(json.dumps(data).encode()) == (
        "segments.0.source_refs.0: Value error, a reference spans at most 10 lines",
    )
    result = run(data)
    assert refs_of(result.data) == [(20, 21, "Exactly one parameter is removed.")]
    assert result.changes == ("S04: A.md:15-33 narrowed to A.md:20-21",)
    parse_response(result.data)


def test_an_over_long_unquoted_reference_is_split_and_never_widened():
    result = run(
        model_response(
            seg("S01", "The record lists the setup.", "SOURCE", src=[ref("A.md", 3, 27)])
        )
    )
    assert refs_of(result.data) == [(3, 12, None), (13, 22, None), (23, 27, None)]
    parse_response(result.data)


def test_a_split_past_four_references_keeps_the_first_four():
    data = model_response(
        seg(
            "S01",
            "The record lists the setup.",
            "SOURCE",
            src=[ref("A.md", 1, 10), ref("A.md", 11, 40)],
        )
    )
    result = run(data)
    assert refs_of(result.data) == [(1, 10, None), (11, 20, None), (21, 30, None), (31, 40, None)]
    parse_response(result.data)


def test_an_unrepairable_reference_is_dropped_only_while_a_valid_one_remains():
    lost_quote = ref("A.md", 1, 15, "words that are not there")
    kept = run(
        model_response(
            seg("S01", "One parameter is removed.", "SOURCE", src=[ref("A.md", 20, 21), lost_quote])
        )
    )
    assert refs_of(kept.data) == [(20, 21, None)]
    assert kept.changes == ("S01: A.md:1-15 dropped (QUOTE_NOT_FOUND)",)
    alone = model_response(seg("S01", "One parameter is removed.", "SOURCE", src=[lost_quote]))
    result = run(alone)
    assert result.changes == () and refused(result.data)  # the parser names the real problem


def test_schema_valid_references_are_left_for_grounding_to_judge():
    data = model_response(
        seg("S01", "One parameter is removed.", "SOURCE", src=[ref("NOT_SENT.md", 1, 2)])
    )
    result = run(data)
    assert result.changes == () and result.data == json.dumps(data).encode()


# --- the other limits --------------------------------------------------------------------------


def test_more_than_four_valid_segment_references_keep_the_first_four():
    many = [ref("A.md", n, n) for n in range(1, 7)]
    result = run(model_response(seg("S01", "The record lists the setup.", "SOURCE", src=many)))
    assert refs_of(result.data) == [(n, n, None) for n in range(1, 5)]
    assert result.changes[-1] == "S01: A.md:6-6 dropped (OVER_REF_LIMIT)"
    parse_response(result.data)


def test_author_refs_lose_duplicates_impossible_ids_and_extras_while_one_remains():
    auth = ["insight", "insight", "learned.9", *(f"attempted.{n}" for n in range(1, 7))]
    result = run(
        model_response(
            seg("S01", "A real capture can still show the wrong scene.", "AUTHOR", auth=auth)
        )
    )
    segment = json.loads(result.data)["segments"][0]
    assert segment["author_refs"] == ["insight", *(f"attempted.{n}" for n in range(1, 6))]
    parse_response(result.data)
    none_left = run(model_response(seg("S01", "A real capture.", "AUTHOR", auth=["learned.9"])))
    assert none_left.changes == () and refused(none_left.data)


def test_outer_whitespace_of_a_segment_is_trimmed():
    data = model_response(
        seg("S01", "One parameter is removed. ", "SOURCE", src=[ref("A.md", 20, 21)])
    )
    result = run(data)
    assert json.loads(result.data)["segments"][0]["text"] == "One parameter is removed."
    parse_response(result.data)


def test_review_questions_are_trimmed_or_dropped_never_the_reply():
    good = {"text": "Check the date.", "reason": "It rests on the note.", "source_refs": []}
    too_long = {**good, "text": "x" * 401}
    many_refs = {**good, "source_refs": [ref("A.md", n, n) for n in range(1, 7)]}
    questions = [too_long, many_refs, *([good] * 21)]
    result = run(model_response(verification_needed=questions))
    kept = json.loads(result.data)["verification_needed"]
    assert len(kept) == 20 and len(kept[0]["source_refs"]) == 4
    assert result.changes == (
        "question 1: dropped (does not fit the schema)",
        "question 2: references 6 kept 4",
        "questions: 22 kept the first 20",
    )
    parse_response(result.data)


def test_a_drafts_no_post_reason_is_dropped():
    result = run(model_response(no_post_reason="Nothing to add."))
    assert json.loads(result.data)["no_post_reason"] is None
    parse_response(result.data)


@pytest.mark.parametrize(
    "broken",
    [
        {"segments": [seg("S01", "x" * 701, "FRAMING")]},  # post text is never cut
        {"segments": [seg("S01", "A tab\there.", "FRAMING")]},
        {"segments": [seg("S1", "Plain words.", "FRAMING")]},
        {"segments": [seg("S01", "Plain words.", "SOURCE", auth=["insight"])]},  # basis
        {"segments": [seg("S01", "Plain.", "FRAMING"), seg("S01", "Words.", "FRAMING")]},
        {"segments": [seg(f"S{n:02d}", "Plain.", "FRAMING") for n in range(1, 62)]},
        {"outcome": "NO_POST", "no_post_reason": "Nothing."},  # with segments
        {"outcome": "DRAFT", "segments": []},
        {"outcome": "NO_POST", "segments": [], "no_post_reason": "x" * 601},
    ],
)
def test_limits_on_what_the_post_says_still_refuse_the_reply(broken):
    data = {**model_response(), **broken}
    result = run(data)
    assert result.changes == () and refused(result.data)


def test_a_human_response_and_a_valid_model_response_are_returned_unchanged():
    long_ref = seg("S01", "Exactly one parameter is removed.", "SOURCE", src=[ref("A.md", 15, 33)])
    human = response(SHA, (long_ref,))
    assert run(human).data == json.dumps(human).encode()
    valid = json.dumps(model_response()).encode()
    assert normalise_response(valid, FILES).data == valid


def test_repair_is_idempotent():
    data = model_response(
        seg(
            "S04",
            "Exactly one parameter is removed.",
            "SOURCE",
            src=[ref("A.md", 15, 33, "Exactly one parameter is removed.")],
        ),
        verification_needed=[{"text": "x" * 401, "reason": "r", "source_refs": []}],
    )
    once = run(data)
    twice = normalise_response(once.data, FILES)
    assert twice.data == once.data and twice.changes == ()
