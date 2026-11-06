import pytest

from public_research_os.evidence.strict_json import JsonViolation, StrictJsonError, loads_strict


def violation(data: bytes) -> tuple[JsonViolation, str | None]:
    with pytest.raises(StrictJsonError) as exc:
        loads_strict(data)
    return exc.value.violation, exc.value.key


def test_valid_json_decodes():
    assert loads_strict(b'{"a": [1, 2.5, {"b": null}], "c": "\\u00e9"}') == {
        "a": [1, 2.5, {"b": None}],
        "c": "é",
    }


@pytest.mark.parametrize(
    "data",
    [
        b'{"status": "candidate", "status": "approved"}',
        b'{"outer": {"k": 1, "k": 2}}',
        b'{"list": [{"ok": 1}, {"k": 1, "k": 1}]}',
        b'[[{"deep": {"k": 1, "k": 2}}]]',
        b'{"st\\u0061tus": "candidate", "status": "approved"}',  # escape-equivalent key
    ],
    ids=["top-level", "nested-object", "object-in-array", "deep", "escaped-equivalent"],
)
def test_duplicate_keys_rejected_at_every_level(data):
    kind, key = violation(data)
    assert kind == JsonViolation.DUPLICATE_KEY
    assert key in {"status", "k"}


def test_same_key_in_sibling_objects_is_allowed():
    assert loads_strict(b'[{"k": 1}, {"k": 2}]') == [{"k": 1}, {"k": 2}]


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity", b"1e400", b"-1e400"])
def test_non_standard_numbers_rejected(literal):
    assert violation(b'{"n": ' + literal + b"}")[0] == JsonViolation.NON_STANDARD_NUMBER


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'{"a": 1,}',
        b"{'a': 1}",
        b'\xef\xbb\xbf{"a": 1}',  # UTF-8 BOM
        b'\xff\xfe{\x00"\x00a\x00"\x00:\x001\x00}\x00',  # UTF-16
        b'{"a": "\xff"}',  # invalid UTF-8
        b'{"a": 1} trailing',
        b'{"n": ' + b"9" * 5000 + b"}",  # exceeds int digit limit
    ],
    ids=[
        "empty",
        "trailing-comma",
        "single-quotes",
        "bom",
        "utf16",
        "bad-utf8",
        "trailing",
        "huge-int",
    ],
)
def test_malformed_input_rejected(data):
    assert violation(data)[0] == JsonViolation.MALFORMED


def test_excessive_nesting_rejected_without_crash():
    assert violation(b"[" * 200_000 + b"]" * 200_000)[0] == JsonViolation.TOO_DEEP


# --- lone surrogates (Codex R1 P2-02) ----------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b'{"a": "x\\ud800"}',
        b'{"a": "x\\udc00"}',
        b'{"a": "\\udfff"}',
        b'{"a": "\\ude00\\ud83d"}',  # reversed pair: two lone surrogates
        b'{"a": "\\ud83d x"}',  # high surrogate not followed by a low one
        b'{"x\\ud800": 1}',  # object key
        b'{"a": {"b": {"c": "\\ud800"}}}',  # nested object value
        b'{"a": ["ok", ["deeper", "\\udc00"]]}',  # nested arrays
        b'[{"k": "ok"}, {"\\udc00": "v"}]',  # key inside an object inside an array
        b'"\\ud800"',  # top-level string
    ],
    ids=[
        "lone-high",
        "lone-low",
        "lone-low-max",
        "reversed-pair",
        "unpaired-high",
        "key",
        "nested-object",
        "nested-array",
        "key-in-array",
        "top-level",
    ],
)
def test_lone_surrogates_rejected_everywhere(data):
    assert violation(data) == (JsonViolation.INVALID_UNICODE, None)


def test_error_never_carries_the_offending_text():
    with pytest.raises(StrictJsonError) as exc:
        loads_strict(b'{"a": "SENTINEL\\ud800"}')
    assert str(exc.value) == "INVALID_UNICODE"
    assert exc.value.key is None
    assert "SENTINEL" not in repr(exc.value)


def test_surrogate_found_at_the_deepest_level_json_accepts():
    """The walk is iterative: no RecursionError at any depth the decoder itself accepted."""
    depth = 0
    for candidate in (900, 500, 200, 100):
        try:
            loads_strict(b"[" * candidate + b"]" * candidate)
        except StrictJsonError:
            continue
        depth = candidate
        break
    assert depth >= 100
    nested = b"[" * depth + b'"\\ud800"' + b"]" * depth
    assert violation(nested)[0] == JsonViolation.INVALID_UNICODE


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'{"a": "\\ud83d\\ude00"}', {"a": "\U0001f600"}),  # escaped pair -> one code point
        (b'{"\\ud83d\\ude00": 1}', {"\U0001f600": 1}),  # escaped pair in a key
        ('{"a": "\U0001f600"}'.encode(), {"a": "\U0001f600"}),  # raw UTF-8 emoji
        ('{"a": "\U0001d11e"}'.encode(), {"a": "\U0001d11e"}),  # supplementary plane
        ('{"a": "é中�￿"}'.encode(), {"a": "é中�￿"}),
        (b'{"a": "\\ue000\\ud7ff"}', {"a": "퟿"}),  # just outside the range
    ],
    ids=["escaped-pair", "escaped-pair-key", "emoji", "supplementary", "bmp", "range-edges"],
)
def test_valid_unicode_is_accepted(data, expected):
    assert loads_strict(data) == expected


def test_encoded_surrogate_bytes_remain_malformed_utf8():
    assert violation(b'{"a": "\xed\xa0\x80"}')[0] == JsonViolation.MALFORMED


def test_duplicate_key_still_detected_alongside_unicode():
    data = '{"k": "\U0001f600", "k": "x"}'.encode()
    assert violation(data) == (JsonViolation.DUPLICATE_KEY, "k")
