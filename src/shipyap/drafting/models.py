"""The provider boundary of `draft-model`: what is asked of a drafting model and what comes back.

Only `provider.AnthropicDrafter` implements `Drafter` for real; tests use fakes. Keeping these
contracts here means nothing but `provider.py` imports the provider SDK, and an offline run
(`--dry-run`) never loads it.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelRequest:
    """Everything sent to the provider, for the token count and the generation alike."""

    model: str
    system_prompt: str
    user_text: str  # the verified brief.md, verbatim
    output_schema: dict[str, Any]
    max_tokens: int
    effort: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelReply:
    """The provider's reply, reduced to what the drafting service needs. `texts` are the reply's
    text blocks in order; other block kinds (for example empty thinking blocks) are dropped."""

    model: str
    request_id: str | None
    stop_reason: str | None
    texts: tuple[str, ...]
    usage: Usage


class Drafter(Protocol):
    def count_input_tokens(self, request: ModelRequest) -> int:
        """The provider's input-token count for `request`. Generates nothing."""
        ...

    def generate(self, request: ModelRequest) -> ModelReply:
        """Exactly one generation request, never retried."""
        ...
