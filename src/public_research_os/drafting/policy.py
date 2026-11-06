"""Allowed models, prices, limits and cost estimates for `draft-model`. Pure; no network.

Every dollar figure here is an ESTIMATE, never a billing guarantee:
- Prices are Anthropic's published per-million-token list prices as of `PRICES_AS_OF`, copied by
  hand. They are not read from the provider and can go stale; re-check them before a live run.
- The offline estimate (`--dry-run`) guesses tokens from UTF-8 bytes. It is not a token count and
  not a bound.
- The live estimate starts from the provider's own count (`messages.count_tokens`, a separate
  request that generates nothing), then adds a relative discrepancy allowance and a fixed
  allowance for structured-output overhead the count might not include. Both allowances are
  judgement, not provider guarantees.
- The output side is `MAX_OUTPUT_TOKENS` (the request's `max_tokens`, which the provider enforces
  per request, thinking included) at the output price.

The only cap across runs is the provider-side workspace spend limit, configured by the operator.
"""

import math
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from public_research_os.drafting.models import Usage


@dataclass(frozen=True)
class Price:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


PRICES_AS_OF = "2026-10-06"
MODELS: dict[str, Price] = {
    "claude-opus-5-5": Price(Decimal("4.00"), Decimal("20.00")),
    "claude-sonnet-5-5": Price(Decimal("2.00"), Decimal("10.00")),
}
DEFAULT_MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_OUTPUT_TOKENS = 16_000
MAX_MODEL_BRIEF_BYTES = 128 * 1024
OFFLINE_BYTES_PER_TOKEN = 3
COUNT_DISCREPANCY = Decimal("0.10")
SCHEMA_OVERHEAD_TOKENS = 2_000

_MILLION = Decimal(1_000_000)
_PRECISION = Decimal("0.0001")
_BUDGET = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,2})?")


@dataclass(frozen=True)
class Estimate:
    basis: Literal["offline", "counted"]
    input_tokens: int  # the offline guess, or the provider's count
    max_input_tokens: int  # with both allowances
    max_output_tokens: int
    max_cost_usd: Decimal


def parse_budget(text: str) -> Decimal | None:
    """A positive dollar amount such as `0.60`; None for anything else (no exponent, sign, NaN)."""
    if _BUDGET.fullmatch(text) is None:
        return None
    amount = Decimal(text)
    return amount if amount > 0 else None


def offline_estimate(model: str, request_bytes: int) -> Estimate:
    return _estimate("offline", model, math.ceil(request_bytes / OFFLINE_BYTES_PER_TOKEN))


def counted_estimate(
    model: str, counted_input_tokens: int, max_output_tokens: int = MAX_OUTPUT_TOKENS
) -> Estimate:
    """`max_output_tokens`: the request's own `max_tokens` (a fact check or repair asks for far
    fewer than a draft, and the provider enforces it)."""
    return _estimate("counted", model, counted_input_tokens, max_output_tokens)


def charge_estimate(model: str, usage: Usage) -> Decimal:
    """List-price cost of the usage the provider reported. Still an estimate, not the bill."""
    price = MODELS[model]
    cost = (
        usage.input_tokens * price.input_usd_per_mtok
        + usage.output_tokens * price.output_usd_per_mtok
    ) / _MILLION
    return _round_up(cost)


def _estimate(
    basis: Literal["offline", "counted"],
    model: str,
    input_tokens: int,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
) -> Estimate:
    price = MODELS[model]
    widened = (Decimal(input_tokens) * (1 + COUNT_DISCREPANCY)).to_integral_value(ROUND_CEILING)
    max_input = int(widened) + SCHEMA_OVERHEAD_TOKENS
    cost = (
        max_input * price.input_usd_per_mtok + max_output_tokens * price.output_usd_per_mtok
    ) / _MILLION
    return Estimate(
        basis=basis,
        input_tokens=input_tokens,
        max_input_tokens=max_input,
        max_output_tokens=max_output_tokens,
        max_cost_usd=_round_up(cost),
    )


def _round_up(amount: Decimal) -> Decimal:
    return amount.quantize(_PRECISION, rounding=ROUND_CEILING)
