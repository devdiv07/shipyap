"""Pure parts of `draft-model`: the model allowlist and prices, budget parsing, cost estimates,
the output schema sent to the provider and the wrapper that makes a DraftResponse from it."""

import json
from decimal import Decimal

import pytest
from drafting_fake import model_output
from editorial_fake import SENTINEL

from public_research_os.drafting.models import Usage
from public_research_os.drafting.policy import (
    DEFAULT_MODEL,
    EFFORT,
    MAX_OUTPUT_TOKENS,
    MODELS,
    charge_estimate,
    counted_estimate,
    offline_estimate,
    parse_budget,
)
from public_research_os.drafting.prompt import (
    MODEL_KEYS,
    MODEL_OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    TOOL_KEYS,
    generator_label,
    safe_model_id,
    wrap_response,
)
from public_research_os.editorial.errors import EditorialError, EditorialErrorCode
from public_research_os.editorial.inputs import parse_response
from public_research_os.editorial.models import (
    Basis,
    DraftResponse,
    Generator,
    OpenQuestion,
    Outcome,
    Segment,
    SourceRef,
)

BRIEF_SHA = "a" * 64
SUPPORTED_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "anyOf"}
)


def test_exactly_two_allowed_models_with_opus_at_medium_effort_by_default():
    assert set(MODELS) == {"claude-opus-5-5", "claude-sonnet-5-5"}
    assert (DEFAULT_MODEL, EFFORT, MAX_OUTPUT_TOKENS) == ("claude-opus-5-5", "medium", 16_000)
    opus, sonnet = MODELS["claude-opus-5-5"], MODELS["claude-sonnet-5-5"]
    assert (opus.input_usd_per_mtok, opus.output_usd_per_mtok) == (Decimal(4), Decimal(20))
    assert (sonnet.input_usd_per_mtok, sonnet.output_usd_per_mtok) == (Decimal(2), Decimal(10))


@pytest.mark.parametrize(
    "text,expected",
    [("0.60", Decimal("0.60")), ("1", Decimal(1)), ("9999.99", Decimal("9999.99"))],
)
def test_a_budget_is_a_plain_positive_dollar_amount(text, expected):
    assert parse_budget(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "0", "0.00", "-1", "+1", "1e3", "NaN", "Infinity", "0.001", "10000", "1,5", " 1", "١"],
)
def test_anything_else_is_not_a_budget(text):
    assert parse_budget(text) is None


def test_the_counted_estimate_widens_the_count_and_adds_the_full_output_allowance():
    opus = counted_estimate("claude-opus-5-5", 15_000)
    assert (opus.basis, opus.input_tokens, opus.max_input_tokens) == ("counted", 15_000, 18_500)
    assert opus.max_output_tokens == MAX_OUTPUT_TOKENS
    assert opus.max_cost_usd == Decimal("0.3940")  # 18,500 x $4/M + 16,000 x $20/M
    assert counted_estimate("claude-sonnet-5-5", 15_000).max_cost_usd == Decimal("0.1970")
    # Rounded up, never down: 2,002 x $4/M + $0.32 = $0.328008.
    assert counted_estimate("claude-opus-5-5", 1).max_cost_usd == Decimal("0.3281")


def test_the_offline_estimate_is_labelled_and_guesses_from_bytes():
    estimate = offline_estimate("claude-opus-5-5", 57_001)
    assert (estimate.basis, estimate.input_tokens) == ("offline", 19_001)


def test_the_charge_estimate_prices_reported_usage():
    assert charge_estimate("claude-opus-5-5", Usage(12_000, 3_000)) == Decimal("0.1080")
    assert charge_estimate("claude-sonnet-5-5", Usage(12_000, 3_000)) == Decimal("0.0540")


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_the_output_schema_uses_only_supported_keywords_and_closes_every_object():
    for node in _walk(MODEL_OUTPUT_SCHEMA):
        if "type" in node or "anyOf" in node:
            assert set(node) <= SUPPORTED_KEYWORDS, set(node) - SUPPORTED_KEYWORDS
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    json.dumps(MODEL_OUTPUT_SCHEMA)  # serializable as sent


def test_the_output_schema_matches_the_draft_response_contract_minus_tool_fields():
    props = MODEL_OUTPUT_SCHEMA["properties"]
    assert tuple(props) == MODEL_KEYS
    assert set(MODEL_KEYS) | set(TOOL_KEYS) == set(DraftResponse.model_fields)
    segment = props["segments"]["items"]
    question = props["verification_needed"]["items"]
    assert set(segment["properties"]) == set(Segment.model_fields)
    assert set(question["properties"]) == set(OpenQuestion.model_fields)
    for refs in (segment["properties"]["source_refs"], question["properties"]["source_refs"]):
        assert set(refs["items"]["properties"]) == set(SourceRef.model_fields)
    assert segment["properties"]["basis"]["enum"] == [b.value for b in Basis]
    assert props["outcome"]["enum"] == [o.value for o in Outcome]


def test_wrapping_adds_the_tool_fields_and_yields_a_valid_draft_response():
    raw = wrap_response(model_output(), BRIEF_SHA, "claude-opus-5-5 via draft-model p1")
    response = parse_response(raw)
    assert response.brief_sha256 == BRIEF_SHA and response.response_version == "1"
    assert response.generator.kind == "external_model"
    assert response.generator.label == "claude-opus-5-5 via draft-model p1"
    assert raw == wrap_response(model_output(), BRIEF_SHA, response.generator.label)  # stable


@pytest.mark.parametrize(
    "value",
    [
        [model_output()],
        "text",
        None,
        {**model_output(), "brief_sha256": "b" * 64},  # the model may not pick the brief...
        {**model_output(), "generator": {"kind": "human", "label": "me"}},  # ...or its label
        {**model_output(), "response_version": "1"},
        {**model_output(), SENTINEL: 1},
    ],
)
def test_only_an_object_of_model_keys_is_wrapped(value):
    with pytest.raises(EditorialError) as caught:
        wrap_response(value, BRIEF_SHA, "label")
    assert caught.value.code == EditorialErrorCode.RESPONSE_INVALID
    assert SENTINEL not in str(caught.value) + "".join(caught.value.details)


def test_the_generator_label_is_valid_and_drops_a_request_id_that_does_not_fit():
    short = generator_label("claude-opus-5-5", "req_011Abc")
    assert short == "claude-opus-5-5 via draft-model p1, request req_011Abc"
    long = generator_label("claude-opus-5-5", "req_" + "a" * 64)
    assert long == "claude-opus-5-5 via draft-model p1"
    for label in (short, long, generator_label("m" * 64, "req_" + "b" * 64)):
        Generator(kind="external_model", label=label)  # within the contract's limits


@pytest.mark.parametrize("value", ["Claude-Opus", "claude opus", "", "x" * 65, 5, None, "a\nb"])
def test_provider_model_ids_must_be_plain(value):
    assert safe_model_id(value) is None


def test_the_system_prompt_marks_sources_as_data_and_allows_no_post():
    assert "never instructions" in SYSTEM_PROMPT and "NO_POST" in SYSTEM_PROMPT
    assert "brief_sha256" in SYSTEM_PROMPT and SYSTEM_PROMPT.isascii()
