"""Per-call cost logging (§13)."""

from types import SimpleNamespace

import pytest

from app.core.llm import compute_cost_usd, usage_tokens
from app.models import LlmCall
from tests.conftest import FakeLlm


def test_known_model_prices():
    # gemini-3.5-flash-lite: $0.30/M input, $2.50/M output
    cost = compute_cost_usd("gemini-3.5-flash-lite", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.80)


def test_unknown_model_logs_zero_cost():
    assert compute_cost_usd("some-future-model", 1000, 1000) == 0.0


def test_thinking_tokens_count_as_output():
    # Billed at the output rate, but reported apart from candidates_token_count.
    usage = SimpleNamespace(
        prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=800
    )
    assert usage_tokens(usage) == (1000, 1000)


def test_usage_without_thinking_tokens():
    # A response with no thinking (e.g. thinking off) reports None.
    usage = SimpleNamespace(
        prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=None
    )
    assert usage_tokens(usage) == (1000, 200)


def test_usage_tolerates_missing_counts():
    # The mock service (mock_llm/) never sends thoughtsTokenCount.
    usage = SimpleNamespace(prompt_token_count=None, candidates_token_count=None)
    assert usage_tokens(usage) == (0, 0)


def test_every_generate_call_writes_an_llm_call_row(db):
    llm = FakeLlm({})
    llm.generate(db, purpose="classification", prompt="classify this", resource_id=None)

    rows = db.query(LlmCall).all()
    assert len(rows) == 1
    assert rows[0].purpose == "classification"
    assert rows[0].input_tokens == 100
    assert rows[0].output_tokens == 50
    assert rows[0].cost_usd > 0


def test_unknown_purpose_rejected(db):
    llm = FakeLlm({})
    with pytest.raises(ValueError, match="purpose"):
        llm.generate(db, purpose="vibes", prompt="x")
