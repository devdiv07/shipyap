"""Editorial input contracts: the author contribution, the verified snapshot gate and the
drafter response. Every refusal is a fixed code; rejected values are never echoed."""

import json

import pytest
from editorial_fake import (
    CLEAN_SEGMENTS,
    SENTINEL,
    contribution,
    make_snapshot,
    ref,
    response,
    seg,
)

from public_research_os.editorial import sources
from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.inputs import parse_contribution, parse_response
from public_research_os.editorial.sources import load_verified_snapshot, split_lines
from public_research_os.evidence.report import ErrorCode
from public_research_os.evidence.validation import UnreadableInput
from public_research_os.ingestion.assemble import SNAPSHOT_DIR

E = EditorialErrorCode
SHA = "c" * 64


def code_of(function, *args) -> EditorialErrorCode:
    with pytest.raises(EditorialError) as caught:
        function(*args)
    assert caught.value.__cause__ is None
    return caught.value.code


def raw(data: dict) -> bytes:
    return json.dumps(data).encode("utf-8")


# --- contribution ----------------------------------------------------------------------------


def test_a_valid_contribution_exposes_addressable_items():
    parsed = parse_contribution(raw(contribution(learned=["first", "second"])))
    items = parsed.referenceable_items()
    assert items["insight"] == "A real capture can still show the wrong scene."
    assert items["learned.2"] == "second" and "learned.3" not in items
    assert parsed.relationship == "reader" and parsed.hashtags == ()


MISSING = object()


@pytest.mark.parametrize(
    "change",
    [
        {"insight": MISSING},  # the human perspective is required
        {"limitations": MISSING},
        {"limitations": []},
        {"limitations": ["   "]},
        {"attempted": ["x"] * 7},
        {"insight": "too short"},
        {"relationship": "co-author"},
        {"channel": "x"},
        {"hashtags": ["#" + SENTINEL]},
        {"hashtags": ["#Agents", "#agents"]},
        {"author_name": f"bidi‮{SENTINEL}"},
        {"audience": f"two\nlines {SENTINEL}"},
        {"focus_paths": ["../escape"]},
        {"unexpected": SENTINEL},
    ],
    ids=lambda change: next(iter(change)),
)
def test_contribution_schema_violations_are_refused_without_echoing_values(change):
    data = {k: v for k, v in contribution(**change).items() if v is not MISSING}
    with pytest.raises(EditorialError) as caught:
        parse_contribution(raw(data))
    assert caught.value.code == E.CONTRIBUTION_INVALID
    assert caught.value.details and SENTINEL not in " ".join(caught.value.details)
    assert SENTINEL not in str(caught.value)


def test_an_unknown_key_is_masked_in_the_details():
    with pytest.raises(EditorialError) as caught:
        parse_contribution(raw(contribution(**{SENTINEL: 1})))
    assert caught.value.details == ("<extra>: unexpected field",)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'{"insight": "a", "insight": "b"}', E.CONTRIBUTION_INVALID_JSON),
        (b'{"contribution_version": "1"', E.CONTRIBUTION_INVALID_JSON),
        (b"\xff\xfe", E.CONTRIBUTION_INVALID_JSON),
        (UnreadableInput(ErrorCode.INPUT_UNREADABLE), E.CONTRIBUTION_UNREADABLE),
        (UnreadableInput(ErrorCode.INPUT_TOO_LARGE), E.CONTRIBUTION_TOO_LARGE),
    ],
)
def test_unreadable_or_ambiguous_contribution_bytes_are_refused(data, expected):
    assert code_of(parse_contribution, data) == expected


# --- verified snapshot gate ------------------------------------------------------------------


def test_a_complete_snapshot_loads_as_verified_text(tmp_path):
    snapshot = load_verified_snapshot(make_snapshot(tmp_path))
    assert snapshot.paths() == ("README.md", "docs/LIMITS.md")  # record order: sorted by path
    readme, limits = snapshot.sources
    assert readme.lines[4].startswith("1. **Finding one.**") and len(readme.lines) == 10
    assert limits.lines == ("# Limits", "", "Only one synthetic configuration was run.")  # CRLF
    assert snapshot.source_url == "https://github.com/example-owner/example-repo"


@pytest.mark.parametrize(
    ("text", "lines"),
    [("", ()), ("a", ("a",)), ("a\n", ("a",)), ("a\r\nb\n", ("a", "b")), ("a\n\n", ("a", ""))],
)
def test_line_splitting_is_one_based_and_has_no_phantom_final_line(text, lines):
    assert split_lines(text) == lines


@pytest.mark.parametrize("damage", ["missing", "extra-file", "byte-flipped", "record-removed"])
def test_a_missing_incomplete_or_modified_snapshot_is_refused(tmp_path, damage):
    root = tmp_path / "missing" if damage == "missing" else make_snapshot(tmp_path)
    if damage == "extra-file":
        (root / SNAPSHOT_DIR / "extra.md").write_bytes(b"planted")
    elif damage == "byte-flipped":
        target = root / SNAPSHOT_DIR / "README.md"
        target.write_bytes(b"X" + target.read_bytes()[1:])
    elif damage == "record-removed":
        (root / "ingestion-record.json").unlink()
    assert code_of(load_verified_snapshot, root) == E.SNAPSHOT_INCOMPLETE


def test_a_file_changed_after_the_completion_check_never_enters_a_brief(tmp_path, monkeypatch):
    root = make_snapshot(tmp_path)
    real_verify = sources.verify_completion

    def verify_then_swap(directory):
        real_verify(directory)  # the snapshot was complete when checked...
        target = directory / SNAPSHOT_DIR / "README.md"
        target.write_bytes(target.read_bytes().replace(b"12/12", b"99/99"))  # ...then changed

    monkeypatch.setattr(sources, "verify_completion", verify_then_swap)
    assert code_of(load_verified_snapshot, root) == E.SNAPSHOT_INCOMPLETE


@pytest.mark.parametrize(
    "files",
    [
        {"README.md": b"\xff\xfe not utf-8"},
        {"README.md": b"binary\x00data"},
        {"README.md": b"x" * (256 * 1024 + 1)},
        {"a.md": b"x" * (200 * 1024), "b.md": b"y" * (200 * 1024), "c.md": b"z" * (200 * 1024)},
    ],
    ids=["not-utf8", "nul", "file-too-large", "total-too-large"],
)
def test_non_text_or_oversized_sources_are_refused_not_truncated(tmp_path, files):
    root = make_snapshot(tmp_path, files)
    assert code_of(load_verified_snapshot, root) == E.SNAPSHOT_SOURCE_UNSUPPORTED


# --- drafter response ------------------------------------------------------------------------


def test_a_valid_response_parses():
    parsed = parse_response(raw(response(SHA)))
    assert [s.id for s in parsed.segments] == ["S01", "S02", "S03", "S04", "S05"]
    assert parsed.generator.kind == "human"


VIOLATIONS = {
    "source-without-refs": [seg("S01", "Text.", "SOURCE")],
    "source-with-author-ref": [
        seg("S01", "Text.", "SOURCE", src=[ref("README.md", 1, 1)], auth=["insight"])
    ],
    "author-with-source-ref": [
        seg("S01", "Text.", "AUTHOR", src=[ref("README.md", 1, 1)], auth=["insight"])
    ],
    "interpretation-without-refs": [seg("S01", "Text.", "INTERPRETATION")],
    "framing-with-refs": [seg("S01", "Text.", "FRAMING", auth=["insight"])],
    "duplicate-ids": [seg("S01", "A.", "FRAMING"), seg("S01", "B.", "FRAMING")],
    "bad-id": [seg("X1", "Text.", "FRAMING")],
    "bad-author-ref": [seg("S01", "Text.", "AUTHOR", auth=["insight.2"])],
    "reversed-range": [seg("S01", "Text.", "SOURCE", src=[ref("README.md", 6, 5)])],
    "range-too-wide": [seg("S01", "Text.", "SOURCE", src=[ref("README.md", 1, 11)])],
    "unsafe-path": [seg("S01", "Text.", "SOURCE", src=[ref("../README.md", 1, 1)])],
    "bidi-control": [seg("S01", f"Looks fine‮{SENTINEL}", "FRAMING")],
    "newline": [seg("S01", f"two\nlines {SENTINEL}", "FRAMING")],
    "outer-space": [seg("S01", " padded", "FRAMING")],
    "too-long": [seg("S01", "x" * 701, "FRAMING")],
}


@pytest.mark.parametrize("name", list(VIOLATIONS))
def test_structurally_invalid_segments_are_refused(name):
    with pytest.raises(EditorialError) as caught:
        parse_response(raw(response(SHA, VIOLATIONS[name])))
    assert caught.value.code == E.RESPONSE_INVALID
    assert SENTINEL not in " ".join(caught.value.details)


@pytest.mark.parametrize(
    "overrides",
    [
        {"segments": []},
        {"outcome": "NO_POST", "no_post_reason": None, "segments": []},
        {"outcome": "NO_POST", "no_post_reason": "Not enough material."},
        {"brief_sha256": "not-a-digest"},
        {"generator": {"kind": "verified_human", "label": "x"}},
        {"response_version": "2"},
    ],
    ids=["draft-empty", "no-post-no-reason", "no-post-with-segments", "bad-sha", "kind", "ver"],
)
def test_inconsistent_responses_are_refused(overrides):
    data = response(SHA)
    data.update(overrides)
    assert code_of(parse_response, raw(data)) == E.RESPONSE_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [("line_start", "5"), ("line_end", 6.0), ("paragraph_break_before", "yes")],
)
def test_numbers_and_flags_are_not_coerced(field, value):
    segment = dict(CLEAN_SEGMENTS[0])
    if field == "paragraph_break_before":
        segment[field] = value
    else:
        segment["source_refs"] = [dict(segment["source_refs"][0], **{field: value})]
    assert code_of(parse_response, raw(response(SHA, [segment]))) == E.RESPONSE_INVALID


@pytest.mark.parametrize(
    "data",
    [
        b'{"response_version": "1", "outcome": "DRAFT", "outcome": "NO_POST"}',
        b'{"response_version": "1", "segments": [',  # truncated, e.g. a model hit max_tokens
        b'{"response_version": NaN}',
    ],
)
def test_ambiguous_or_truncated_response_json_is_refused(data):
    assert code_of(parse_response, data) == E.RESPONSE_INVALID_JSON
