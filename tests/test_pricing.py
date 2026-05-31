"""Tests for pricing.py — cost calculation and unknown-model handling."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

import hermes_telemetry.pricing as pricing


@pytest.fixture(autouse=True)
def reset_pricing():
    """Reset cached state before each test."""
    pricing._custom_pricing = None
    pricing._warned_unknown.clear()
    yield
    pricing._custom_pricing = None
    pricing._warned_unknown.clear()


# ---------------------------------------------------------------------------
# Basic calculation — new usage-dict signature
# ---------------------------------------------------------------------------

def test_known_model_anthropic():
    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "claude-sonnet-4-6")
    assert abs(cost - 3.00) < 1e-9


def test_known_model_output():
    cost = pricing.estimate_cost({"output_tokens": 1_000_000}, "claude-sonnet-4-6")
    assert abs(cost - 15.00) < 1e-9


def test_known_model_both():
    # 1M in @ $3, 1M out @ $15 → $18
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "claude-sonnet-4-6"
    )
    assert abs(cost - 18.00) < 1e-9


def test_small_token_count():
    # 1000 input tokens of claude-sonnet-4-6 @ $3/M → $0.003 (1000/1_000_000 * 3.0)
    cost = pricing.estimate_cost({"input_tokens": 1000}, "claude-sonnet-4-6")
    assert abs(cost - 0.003) < 1e-9


def test_zero_tokens():
    cost = pricing.estimate_cost({"input_tokens": 0, "output_tokens": 0}, "claude-sonnet-4-6")
    assert cost == 0.0


def test_deepseek_pricing():
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "deepseek-chat"
    )
    assert abs(cost - (0.27 + 1.10)) < 1e-6


def test_openai_gpt4o():
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "gpt-4o"
    )
    assert abs(cost - (2.50 + 10.00)) < 1e-6


# ---------------------------------------------------------------------------
# Case-insensitive matching
# ---------------------------------------------------------------------------

def test_case_insensitive():
    cost_lower = pricing.estimate_cost({"input_tokens": 1000}, "claude-sonnet-4-6")
    cost_upper = pricing.estimate_cost({"input_tokens": 1000}, "CLAUDE-SONNET-4-6")
    assert abs(cost_lower - cost_upper) < 1e-12


# ---------------------------------------------------------------------------
# Prefix matching
# ---------------------------------------------------------------------------

def test_prefix_match_unknown_variant():
    # A hypothetical future claude-sonnet-4-9-... should hit the prefix
    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "claude-sonnet-99")
    assert abs(cost - 3.00) < 1e-6


def test_prefix_match_opus():
    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "claude-opus-5-0-future")
    assert abs(cost - 5.00) < 1e-6


# ---------------------------------------------------------------------------
# Unknown model
# ---------------------------------------------------------------------------

def test_unknown_model_returns_zero(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost(
            {"input_tokens": 5000, "output_tokens": 2000}, "totally-unknown-model-xyz"
        )
    assert cost == 0.0


def test_unknown_model_warns_once(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        pricing.estimate_cost({"input_tokens": 100, "output_tokens": 100}, "new-unknown-model")
        pricing.estimate_cost({"input_tokens": 100, "output_tokens": 100}, "new-unknown-model")
    # Should only warn once
    warns = [r for r in caplog.records if "new-unknown-model" in r.message]
    assert len(warns) == 1


def test_empty_model_returns_zero():
    cost = pricing.estimate_cost({"input_tokens": 1000, "output_tokens": 1000}, "")
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Custom pricing via YAML override
# ---------------------------------------------------------------------------

def test_custom_pricing_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir()
    (tele_dir / "pricing.yaml").write_text(textwrap.dedent("""
        my-custom-model:
          input: 10.00
          output: 20.00
    """))
    pricing._custom_pricing = None

    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "my-custom-model"
    )
    assert abs(cost - 30.00) < 1e-6


def test_custom_pricing_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir()
    (tele_dir / "pricing.yaml").write_text(textwrap.dedent("""
        claude-sonnet-4-6:
          input: 99.00
          output: 99.00
    """))
    pricing._custom_pricing = None

    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "claude-sonnet-4-6")
    assert abs(cost - 99.00) < 1e-6


def test_custom_pricing_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    # No pricing.yaml file — should fall back to defaults silently
    pricing._custom_pricing = None
    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "gpt-4o")
    assert abs(cost - 2.50) < 1e-6


def test_custom_pricing_malformed_yaml(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir()
    (tele_dir / "pricing.yaml").write_text(":::invalid yaml:::")
    pricing._custom_pricing = None
    with caplog.at_level(logging.WARNING):
        cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "gpt-4o")
    # Should fall back to defaults
    assert abs(cost - 2.50) < 1e-6


# ---------------------------------------------------------------------------
# New tests for cache/reasoning split (Refinement 1)
# ---------------------------------------------------------------------------

def test_cache_pricing_cheaper_than_fresh_input():
    """Cache reads should cost less than fresh input tokens for the same total."""
    # 500k cache_read + 100k input (total 600k "input-side")
    cost_with_cache = pricing.estimate_cost(
        {"cache_read_tokens": 500_000, "input_tokens": 100_000},
        "claude-sonnet-4-6",
    )
    # All 600k as fresh input
    cost_all_fresh = pricing.estimate_cost(
        {"input_tokens": 600_000},
        "claude-sonnet-4-6",
    )
    assert cost_with_cache < cost_all_fresh


def test_no_double_count_prompt_tokens():
    """prompt_tokens must NOT be used — only input + cache_read are counted."""
    # If prompt_tokens were counted, cost would be higher
    usage = {
        "prompt_tokens": 1000,
        "input_tokens": 800,
        "cache_read_tokens": 200,
    }
    cost = pricing.estimate_cost(usage, "claude-sonnet-4-6")
    # Should only count 800 input + 200 cache_read, NOT 1000 prompt_tokens
    expected_input = 800 * 3.00 / 1_000_000
    expected_cache = 200 * 0.30 / 1_000_000
    assert abs(cost - (expected_input + expected_cache)) < 1e-10


def test_reasoning_tokens_billed_as_output():
    """reasoning_tokens should be billed at the output rate by default."""
    cost = pricing.estimate_cost(
        {"reasoning_tokens": 1000, "output_tokens": 0},
        "claude-sonnet-4-6",
    )
    expected = 1000 * 15.00 / 1_000_000
    assert abs(cost - expected) < 1e-10


def test_estimate_cost_empty_usage():
    """`estimate_cost({}, model)` should return 0.0."""
    assert pricing.estimate_cost({}, "claude-sonnet-4-6") == 0.0


def test_cache_multiplier_fallback(caplog):
    """Models without explicit cache_read use input * 0.10 default multiplier."""
    import logging
    # gpt-4o has no explicit cache_read price
    with caplog.at_level(logging.DEBUG, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost(
            {"cache_read_tokens": 1_000_000}, "gpt-4o"
        )
    # Expected: 1M * (2.50 * 0.10) / 1M = $0.25
    expected = 2.50 * 0.10
    assert abs(cost - expected) < 1e-9
