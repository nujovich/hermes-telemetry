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
# Basic calculation
# ---------------------------------------------------------------------------

def test_known_model_anthropic():
    cost = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=1_000_000, tokens_out=0)
    assert abs(cost - 3.00) < 1e-9


def test_known_model_output():
    cost = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=0, tokens_out=1_000_000)
    assert abs(cost - 15.00) < 1e-9


def test_known_model_both():
    # 1M in @ $3, 1M out @ $15 → $18
    cost = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=1_000_000, tokens_out=1_000_000)
    assert abs(cost - 18.00) < 1e-9


def test_small_token_count():
    # 1000 input tokens of claude-sonnet-4-6 @ $3/M → $0.003 (1000/1_000_000 * 3.0)
    cost = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=1000, tokens_out=0)
    assert abs(cost - 0.003) < 1e-9


def test_zero_tokens():
    cost = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=0, tokens_out=0)
    assert cost == 0.0


def test_deepseek_pricing():
    cost = pricing.estimate_cost("deepseek-chat", tokens_in=1_000_000, tokens_out=1_000_000)
    assert abs(cost - (0.27 + 1.10)) < 1e-6


def test_openai_gpt4o():
    cost = pricing.estimate_cost("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)
    assert abs(cost - (2.50 + 10.00)) < 1e-6


# ---------------------------------------------------------------------------
# Case-insensitive matching
# ---------------------------------------------------------------------------

def test_case_insensitive():
    cost_lower = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=1000, tokens_out=0)
    cost_upper = pricing.estimate_cost("CLAUDE-SONNET-4-6", tokens_in=1000, tokens_out=0)
    assert abs(cost_lower - cost_upper) < 1e-12


# ---------------------------------------------------------------------------
# Prefix matching
# ---------------------------------------------------------------------------

def test_prefix_match_unknown_variant():
    # A hypothetical future claude-sonnet-4-9-... should hit the prefix
    cost = pricing.estimate_cost("claude-sonnet-99", tokens_in=1_000_000, tokens_out=0)
    assert abs(cost - 3.00) < 1e-6


def test_prefix_match_opus():
    cost = pricing.estimate_cost("claude-opus-5-0-future", tokens_in=1_000_000, tokens_out=0)
    assert abs(cost - 5.00) < 1e-6


# ---------------------------------------------------------------------------
# Unknown model
# ---------------------------------------------------------------------------

def test_unknown_model_returns_zero(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost("totally-unknown-model-xyz", tokens_in=5000, tokens_out=2000)
    assert cost == 0.0


def test_unknown_model_warns_once(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        pricing.estimate_cost("new-unknown-model", tokens_in=100, tokens_out=100)
        pricing.estimate_cost("new-unknown-model", tokens_in=100, tokens_out=100)
    # Should only warn once
    warns = [r for r in caplog.records if "new-unknown-model" in r.message]
    assert len(warns) == 1


def test_empty_model_returns_zero():
    cost = pricing.estimate_cost("", tokens_in=1000, tokens_out=1000)
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

    cost = pricing.estimate_cost("my-custom-model", tokens_in=1_000_000, tokens_out=1_000_000)
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

    cost = pricing.estimate_cost("claude-sonnet-4-6", tokens_in=1_000_000, tokens_out=0)
    assert abs(cost - 99.00) < 1e-6


def test_custom_pricing_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    # No pricing.yaml file — should fall back to defaults silently
    pricing._custom_pricing = None
    cost = pricing.estimate_cost("gpt-4o", tokens_in=1_000_000, tokens_out=0)
    assert abs(cost - 2.50) < 1e-6


def test_custom_pricing_malformed_yaml(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir()
    (tele_dir / "pricing.yaml").write_text(":::invalid yaml:::")
    pricing._custom_pricing = None
    with caplog.at_level(logging.WARNING):
        cost = pricing.estimate_cost("gpt-4o", tokens_in=1_000_000, tokens_out=0)
    # Should fall back to defaults
    assert abs(cost - 2.50) < 1e-6
