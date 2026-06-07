"""Tests for pricing.py — cost calculation and unknown-model handling."""

from __future__ import annotations

import textwrap

import hermes_telemetry.pricing as pricing
import pytest


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
    cost = pricing.estimate_cost({"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "gpt-4o")
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
    (tele_dir / "pricing.yaml").write_text(
        textwrap.dedent("""
        my-custom-model:
          input: 10.00
          output: 20.00
    """)
    )
    pricing._custom_pricing = None

    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "my-custom-model"
    )
    assert abs(cost - 30.00) < 1e-6


def test_custom_pricing_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir()
    (tele_dir / "pricing.yaml").write_text(
        textwrap.dedent("""
        claude-sonnet-4-6:
          input: 99.00
          output: 99.00
    """)
    )
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


def test_cache_read_and_write_split_exact():
    """cache_read and cache_write are billed at their own rates, separately
    from fresh input — and the total is the exact per-component sum (no
    double-counting via prompt_tokens)."""
    usage = {
        "input_tokens": 100_000,
        "cache_read_tokens": 500_000,
        "cache_write_tokens": 200_000,
        "output_tokens": 50_000,
        # prompt_tokens would be input+cache_read+cache_write = 800_000; must be ignored
        "prompt_tokens": 800_000,
    }
    cost = pricing.estimate_cost(usage, "claude-sonnet-4-6")
    # claude-sonnet-4-6: input 3.00, output 15.00, cache_read 0.30, cache_write 3.75
    expected = (100_000 * 3.00 + 500_000 * 0.30 + 200_000 * 3.75 + 50_000 * 15.00) / 1_000_000
    assert abs(cost - expected) < 1e-9

    # cache_read is cheaper than fresh input; cache_write is more expensive.
    all_fresh = pricing.estimate_cost(
        {"input_tokens": 800_000, "output_tokens": 50_000}, "claude-sonnet-4-6"
    )
    # Our split has 500k at the cheap cache_read rate, so total input-side
    # cost must be LOWER than treating all 800k as fresh input despite the
    # 200k cache_write premium.
    assert cost < all_fresh


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


# ---------------------------------------------------------------------------
# Auto-refreshed (dateless) key covers dated model variants by prefix
# (CAMBIO 1: the gateway records models with a date suffix, but pricing.yaml
#  keys arrive from OpenRouter without the date.)
# ---------------------------------------------------------------------------


def _write_pricing_yaml(tmp_path, monkeypatch, body: str):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tele_dir = tmp_path / "telemetry"
    tele_dir.mkdir(exist_ok=True)
    (tele_dir / "pricing.yaml").write_text(body)
    pricing._custom_pricing = None


def test_dated_model_covered_by_dateless_prefix_key(tmp_path, monkeypatch):
    """A dated model from the gateway is costed via the dateless auto key by
    prefix. This is the bug fix: was $0.00 before, real cost now."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "google/gemini-3-flash-preview":
            input: 0.5
            output: 3.0
        defaults:
          cache_read_multiplier: 0.10
          cache_write_multiplier: 1.25
    """),
    )

    cost = pricing.estimate_cost(
        {"input_tokens": 19291, "output_tokens": 704},
        "google/gemini-3-flash-preview-20251217",
    )
    expected = (19291 * 0.5 + 704 * 3.0) / 1e6
    assert abs(cost - expected) < 1e-12


def test_exact_match_still_preferred_over_prefix(tmp_path, monkeypatch):
    """An exact key resolves by exact match, not the dateless-prefix path."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "google/gemini-2.5-flash-lite":
            input: 0.10
            output: 0.40
          "google/gemini-2.5-flash":
            input: 99.0
            output: 99.0
    """),
    )

    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "google/gemini-2.5-flash-lite",
    )
    # Must use the exact lite price, NOT the longer/other 'flash' key.
    assert abs(cost - (0.10 + 0.40)) < 1e-9


def test_regression_dated_claude_resolves_by_prefix():
    """A dated Claude model still resolves to $3/$15 via prefix (no custom file)."""
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "claude-sonnet-4-20250514",
    )
    assert abs(cost - (3.00 + 15.00)) < 1e-9


def test_regression_unknown_model_no_false_positive(tmp_path, monkeypatch):
    """Widening the prefix scan must not turn unknown models into matches."""
    # Even with custom keys present, a truly unrelated model stays $0.00.
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "google/gemini-3-flash-preview":
            input: 0.5
            output: 3.0
    """),
    )
    cost = pricing.estimate_cost(
        {"input_tokens": 5000, "output_tokens": 2000}, "totally-unknown-model-xyz"
    )
    assert cost == 0.0


def test_cache_multiplier_fallback(caplog):
    """Models without explicit cache_read use input * 0.10 default multiplier."""
    import logging

    # gpt-4o has no explicit cache_read price
    with caplog.at_level(logging.DEBUG, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost({"cache_read_tokens": 1_000_000}, "gpt-4o")
    # Expected: 1M * (2.50 * 0.10) / 1M = $0.25
    expected = 2.50 * 0.10
    assert abs(cost - expected) < 1e-9


# ---------------------------------------------------------------------------
# Gemini direct-provider lookups
#
# Hermes hooks fire with model=gemini-3-flash-preview, provider=gemini when
# calling Google AI Studio directly (not via OpenRouter). Verify the bare
# Gemini family names resolve to the correct entries, including the
# previously-buggy fallthrough where Gemini 3 was priced as Gemini 1.5 Flash.
# ---------------------------------------------------------------------------


def test_gemini_3_flash_preview_direct():
    """gemini-3-flash-preview (no google/ prefix) resolves to its real price."""
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 0}, "gemini-3-flash-preview"
    )
    assert abs(cost - 0.50) < 1e-6  # not 0.075 (legacy Flash 1.5 fallback)


def test_gemini_3_flash_preview_cache_read_explicit():
    cost = pricing.estimate_cost({"cache_read_tokens": 1_000_000}, "gemini-3-flash-preview")
    assert abs(cost - 0.05) < 1e-6  # 10% of input — also matches Google's published price


def test_gemini_2_5_flash_direct():
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "gemini-2.5-flash"
    )
    expected = 0.30 + 2.50
    assert abs(cost - expected) < 1e-6


def test_gemini_2_5_flash_lite_direct():
    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "gemini-2.5-flash-lite")
    assert abs(cost - 0.10) < 1e-6


def test_gemini_3_5_flash_direct():
    cost = pricing.estimate_cost({"output_tokens": 1_000_000}, "gemini-3.5-flash")
    assert abs(cost - 9.00) < 1e-6


def test_gemini_dated_variant_resolves_by_prefix():
    """gemini-3-flash-preview-20251217 (dated) hits the gemini-3-flash prefix."""
    cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "gemini-3-flash-preview-20251217")
    assert abs(cost - 0.50) < 1e-6


def test_gemini_1_5_no_longer_resident(caplog):
    """gemini-1.5-flash was removed from the default table (deprecated 2026-Q2).

    Without the legacy 'gemini' catch-all prefix, an unknown 1.5 variant now
    surfaces as a warning instead of being silently priced. This is the desired
    behavior — a deprecated entry shouldn't be the silent default.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "gemini-1.5-flash-tuned")
    # 1.5-flash prefix is gone — falls through to unknown
    assert cost == 0.0


def test_gemini_no_generic_catchall_regression(caplog):
    """A bare 'gemini-anything' must NOT silently match the old generic 'gemini' prefix.

    Regression guard: previously the bare 'gemini' prefix swept any unknown
    variant into Flash 1.5 pricing ($0.075/$0.30), underestimating Gemini 3
    direct-Google calls by ~6.5x. After the cleanup, the catch-all is removed.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "gemini-future-foo")
    assert cost == 0.0  # not 0.075
