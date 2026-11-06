"""Tool text never reaches a post, and AUTHOR means the author's own words (burst draft f6d3ac71).

S05 cited `limitations.1` (the tool's provenance note) and repeated it; S04 was AUTHOR citing only
`insight`, the unverified backlog angle, in first person. Both are blocked; the free repair drops
S05 and then shelves the post, which falls under the house-style minimum.
"""

import pytest
from drafting_fake import REQUEST_ID, FakeApi
from editorial_fake import SENTINEL
from pydantic import SecretStr

from public_research_os.drafting import provider
from public_research_os.drafting.errors import DraftingError, DraftingErrorCode, Phase
from public_research_os.drafting.models import ModelRequest
from public_research_os.drafting.policy import EFFORT
from public_research_os.drafting.prompt import STORY_PROMPT_VERSION, STORY_SYSTEM_PROMPT

MODEL = "claude-sonnet-5-5"


def test_the_story_prompt_is_p4_and_says_limitations_are_not_post_text():
    assert STORY_PROMPT_VERSION == "p4"
    assert "Never cite them and never repeat or paraphrase them" in STORY_SYSTEM_PROMPT
    assert "Basis AUTHOR only for a segment that cites attempted or learned" in STORY_SYSTEM_PROMPT


def test_a_rejected_count_keeps_only_the_known_error_type_phase_and_request_id(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_LOG", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    api = FakeApi(count_status=400)
    client = provider.AnthropicDrafter(SecretStr("sk-ant-test-key-0000"), transport=api.transport)
    request = ModelRequest(
        model=MODEL,
        system_prompt="system",
        user_text="user",
        output_schema={"type": "object"},
        max_tokens=100,
        effort=EFFORT,
    )
    with pytest.raises(DraftingError) as caught:
        client.count_input_tokens(request)
    error = caught.value
    assert (error.code, error.phase) == (DraftingErrorCode.PROVIDER_REQUEST_REJECTED, Phase.COUNT)
    assert error.error_type == "api_error"
    detail = error.detail()
    assert detail == f" (phase COUNT, provider type api_error, request id {REQUEST_ID})"
    assert SENTINEL not in detail


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"type": "made_up_error", "message": "x"}},
        {"error": "billing_error"},
        ["billing_error"],
        None,
    ],
)
def test_an_unknown_or_malformed_error_type_is_dropped(body):
    assert provider._error_type(body) is None


def test_a_known_error_type_survives_without_its_message():
    body = {"error": {"type": "billing_error", "message": f"secret {SENTINEL}"}}
    assert provider._error_type(body) == "billing_error"
