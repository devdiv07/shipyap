"""C3: the anthropic-api route sends byte-identical requests after the plan route was added.

The golden values below were recorded on master (0fca156) before any C3 change, with this same
scenario: discovery, draft, fact check and two repair rounds through the real engine and a
recording fake provider. Any change to a request (prompt, brief, schema, model, effort,
max_tokens), to its order, or to the money accounting fails this test.
"""

from decimal import Decimal

import pytest
from test_automation import FIRST_PERSON, Model, _with_author, execute, model_text
from test_automation import work as automation_work  # noqa: F401  (fixture)

from shipyap.automation import engine
from shipyap.automation.state import State

# (spending kind as the engine records it, sha256 of the canonical request), in call order.
GOLDEN_CALLS = [
    ("discovery", "5d74ca009fbb59267e2789155eb8d45f560636dd5ce83ead5fa4ef6d1e4dfc34"),
    ("draft", "df09218ae1eb0604d3d83831d60d5014ea23eaa8b00765839bec0d4f6dfa46b3"),
    ("fact_check", "bed34eed43566822dddf81d6b7062b31ee26554ab2d02bf30eae7d378ebeeddb"),
    ("repair", "cfda845a4bccf5e2e786efbb3a193b5d3bee7f5a9eafd028e6025a62882c0018"),
    ("repair", "755ef10b57fb8a704a541f99ca1145dfe8f1e2a7a8e568af835e82c118069aa6"),
]
GOLDEN_ESTIMATED_USD = "0.4650"
GOLDEN_CHARGED_USD = "0.2710"
GOLDEN_SPENDING = ["0.1750", "0.1750", "0.0550", "0.0300", "0.0300"]
GOLDEN_STATUSES = ["SHELVED", "NEW_STORY_DRAFTED", "NEEDS_ATTENTION"]


@pytest.fixture
def work(automation_work):  # noqa: F811  (the shared fixture, under this module's name)
    return automation_work


class Recording(Model):
    def __init__(self):
        super().__init__()
        self.sent = []

    def generate(self, request):
        self.sent.append(engine.request_hash(request))
        return super().generate(request)


def _observe(work):
    cfg, path = work
    cfg = _with_author(cfg, path)
    model = Recording()
    model.draft_text = model_text(FIRST_PERSON)
    model.verdicts = {"S03": ("OUTDATED", "The canary replaced this behaviour.")}
    result = execute(cfg, model=model, allow_paid=True)
    rows = (
        State(cfg.state / "automation.sqlite")
        .db.execute("SELECT kind, request_hash, estimated_usd FROM spending ORDER BY id")
        .fetchall()
    )
    return result, model, rows


def test_anthropic_api_requests_and_accounting_are_byte_identical_to_master(work):
    result, model, rows = _observe(work)
    calls = [(kind, digest) for kind, digest, _ in rows]
    assert [digest for _, digest in calls] == model.sent  # every sent request was accounted
    assert calls == GOLDEN_CALLS
    assert [usd for *_, usd in rows] == GOLDEN_SPENDING
    assert result.estimated_usd == Decimal(GOLDEN_ESTIMATED_USD)
    assert result.charged_usd == Decimal(GOLDEN_CHARGED_USD)
    assert result.statuses == GOLDEN_STATUSES
