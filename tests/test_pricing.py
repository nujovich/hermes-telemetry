"""Tests for pricing.py — cost calculation and unknown-model handling."""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import hermes_telemetry.pricing as pricing
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def reset_pricing(tmp_path_factory, monkeypatch):
    """Isolate pricing from the developer's live ~/.hermes file.

    Point HERMES_HOME at a throwaway dir seeded with the committed, deterministic
    tests/fixtures/pricing.yaml — so tests run against known data, never the
    machine's auto-refreshed file. Uses tmp_path_factory (not tmp_path) so this
    dir never collides with tests that point HERMES_HOME at their own tmp_path.

    Tests that need a different custom file override HERMES_HOME themselves (a
    later monkeypatch.setenv wins) and reset the cache after writing.
    """
    home = tmp_path_factory.mktemp("pricing_home")
    tele = home / "telemetry"
    tele.mkdir()
    shutil.copy(FIXTURES / "pricing.yaml", tele / "pricing.yaml")
    monkeypatch.setenv("HERMES_HOME", str(home))

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


# ---------------------------------------------------------------------------
# Google name normalization — issue #2 follow-up (PR 2)
#
# The same Gemini model can reach the gateway under two forms:
#   - direct Google AI Studio  → bare "gemini-3-flash-preview"
#   - OpenRouter routing       → "google/gemini-3-flash-preview"
#
# Both forms must resolve to the same price entry, regardless of which form
# the pricing data carries. The normalization is google-specific — other
# provider prefixes (anthropic/, meta-llama/, openrouter/) must never be
# stripped naively, since they have distinct pricing semantics.
# ---------------------------------------------------------------------------


def test_google_prefix_and_bare_form_resolve_to_same_price():
    """gemini-3-flash-preview ↔ google/gemini-3-flash-preview return the same entry.

    The bare form lives in _DEFAULT_PRICING. The google/-prefixed form must
    resolve via the alt-form fallback to the same dict — same input/output/
    cache_read across the board.
    """
    bare = pricing._lookup_base("gemini-3-flash-preview")
    prefixed = pricing._lookup_base("google/gemini-3-flash-preview")
    assert bare is not None, "bare gemini-3-flash-preview missing from _DEFAULT_PRICING"
    assert prefixed is not None, "google/gemini-3-flash-preview should normalize to bare"
    for field in ("input", "output", "cache_read"):
        assert bare.get(field) == prefixed.get(field), (
            f"{field}: bare={bare.get(field)} prefixed={prefixed.get(field)}"
        )

    # End-to-end cost check — both forms must produce identical dollar amounts.
    usage = {"input_tokens": 1_000_000, "output_tokens": 500_000}
    cost_bare = pricing.estimate_cost(usage, "gemini-3-flash-preview")
    cost_prefixed = pricing.estimate_cost(usage, "google/gemini-3-flash-preview")
    assert abs(cost_bare - cost_prefixed) < 1e-9


def test_non_google_provider_prefix_resolves_to_its_own_entry():
    """meta-llama/llama-3.1-405b-instruct must keep resolving to its exact entry.

    Regression guard: a naive "strip any provider/ prefix" implementation
    would let meta-llama/* be re-interpreted as bare llama-*, potentially
    masking the dedicated meta-llama/* entries in _DEFAULT_PRICING. The
    google/ normalization is opt-in and must leave every other provider
    prefix untouched.
    """
    # The exact meta-llama/* entry stays authoritative — same dict before and
    # after this PR.
    prefixed = pricing._lookup_base("meta-llama/llama-3.1-405b-instruct")
    assert prefixed == {"input": 2.70, "output": 2.70}

    # A meta-llama/* variant that is NOT an exact key must NOT fall back to
    # the bare llama-3.1-405 prefix via naive stripping. It must stay None
    # so the unknown-model warning surfaces — exactly the failure mode the
    # google/ normalization is scoped to avoid recreating elsewhere.
    assert pricing._lookup_base("meta-llama/llama-99-imaginary") is None

    # And the alt-form helper must refuse to normalize meta-llama/*.
    assert pricing._google_alt_form("meta-llama/llama-3.1-405b-instruct") is None


def test_unprefixed_non_gemini_model_unaffected_by_normalization():
    """claude-sonnet-4-6 (no provider prefix, not Gemini) resolves identically before/after.

    The normalization triggers only when the model id starts with "google/"
    or "gemini-". Anything else must take the original lookup path and
    produce its existing price. Catches the failure mode where the alt-form
    branch is reached for models that shouldn't be normalized at all.
    """
    base = pricing._lookup_base("claude-sonnet-4-6")
    assert base is not None
    # Direct cost via estimate_cost should match the fixture's claude-sonnet-4-6 entry
    # (input=3.0, output=15.0).
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "claude-sonnet-4-6",
    )
    assert abs(cost - (3.0 + 15.0)) < 1e-9

    # And the alt-form helper must return None for non-Gemini, non-google/* ids.
    assert pricing._google_alt_form("claude-sonnet-4-6") is None
    assert pricing._google_alt_form("anthropic/claude-sonnet-4-6") is None
    assert pricing._google_alt_form("meta-llama/llama-3.1-405b-instruct") is None


# ---------------------------------------------------------------------------
# Review follow-ups requested on PR #15 — pin invariants the alt-form lookup
# depends on but doesn't currently exercise.
# ---------------------------------------------------------------------------


def test_equal_length_prefix_tie_break_prefers_custom_over_default(monkeypatch):
    """At equal prefix length, the higher-precedence source wins (custom > default).

    The prefix scan combines custom + defaults + prefix table and sorts by
    descending prefix length. Among equal-length prefixes, Python's stable sort
    preserves insertion order, so the custom source (inserted first) must win.
    Pins the invariant documented in _lookup_form's docstring.
    """
    shared_prefix = "tie-break-model-"
    custom_price = {"input": 1.0, "output": 2.0}
    default_price = {"input": 9.0, "output": 9.0}

    monkeypatch.setattr(
        pricing,
        "_load_custom_pricing",
        lambda: {
            "overrides": {"*": {shared_prefix: custom_price}},
            "sources": {},
            "defaults": {},
            "subscription_models": set(),
            "estimated_price_models": set(),
            "legacy": False,
        },
    )
    monkeypatch.setattr(pricing, "_DEFAULT_PRICING", {shared_prefix: default_price})
    monkeypatch.setattr(pricing, "_PREFIX_PRICING", [])

    # Not an exact key -> forces the prefix scan; both sources carry the same
    # prefix at equal length, so the stable-sort tie-break decides the winner.
    result = pricing._lookup_base(shared_prefix + "20251217")
    assert result == custom_price, f"expected custom to win the tie, got {result}"


def test_google_alt_form_resolves_dated_variant_via_longest_prefix(monkeypatch):
    """google/gemini-3-flash-preview-<date> resolves via alt-form + longest-prefix.

    A dated OpenRouter-routed id has no exact key and its google/ prefix blocks
    the bare-gemini entries on the first pass. The alt-form fallback strips
    google/ and the longest-prefix scan then matches the longest applicable key:
    the exact "gemini-3-flash-preview" entry in _DEFAULT_PRICING (len 22) wins
    over the shorter "gemini-3-flash" family prefix in _PREFIX_PRICING (len 14).
    Exercises the intersection of name normalization and the longest-prefix matcher.
    """
    monkeypatch.setattr(
        pricing,
        "_load_custom_pricing",
        lambda: {
            "overrides": {},
            "sources": {},
            "defaults": {},
            "subscription_models": set(),
            "estimated_price_models": set(),
            "legacy": False,
        },
    )

    dated_prefixed = "google/gemini-3-flash-preview-20251217"
    dated_bare = "gemini-3-flash-preview-20251217"

    resolved = pricing._lookup_base(dated_prefixed)
    assert resolved is not None, (
        "google/-prefixed dated variant should resolve via alt-form + prefix scan"
    )
    assert resolved == pricing._lookup_base(dated_bare)

    usage = {"input_tokens": 1_000_000, "output_tokens": 500_000}
    assert (
        abs(pricing.estimate_cost(usage, dated_prefixed) - pricing.estimate_cost(usage, dated_bare))
        < 1e-9
    )


# ---------------------------------------------------------------------------
# Provider-aware lookup (issue #24) + NVIDIA NIM seeds (issue #12 Phase 1)
#
# An OpenRouter-sourced price must never cost a call another provider served.
# The guard keys on each entry's _source vs the call's provider. Seeds with no
# _source (_DEFAULT_PRICING, prefix table, hand-added overrides) stay neutral.
# ---------------------------------------------------------------------------


def test_openrouter_entry_blocked_for_nous(tmp_path, monkeypatch, caplog):
    """An OpenRouter-sourced price is not eligible for a provider=nous call."""
    import logging

    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "qwen3.7-plus":
            input: 0.40
            output: 1.60
            _source: openrouter
    """),
    )
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            "qwen3.7-plus",
            provider="nous",
        )
    assert cost == 0.0  # NOT 0.40+1.60 — OpenRouter price must not leak to Nous
    assert any("nous" in r.message for r in caplog.records)


def test_openrouter_entry_used_for_openrouter(tmp_path, monkeypatch):
    """The same OpenRouter entry IS eligible for a provider=openrouter call."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "qwen3.7-plus":
            input: 0.40
            output: 1.60
            _source: openrouter
    """),
    )
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "qwen3.7-plus",
        provider="openrouter",
    )
    assert abs(cost - (0.40 + 1.60)) < 1e-9


def test_empty_provider_keeps_backward_compat(tmp_path, monkeypatch):
    """provider="" (default) keeps the historical provider-blind behaviour:
    an OpenRouter entry is still eligible."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "qwen3.7-plus":
            input: 0.40
            output: 1.60
            _source: openrouter
    """),
    )
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, "qwen3.7-plus"
    )
    assert abs(cost - (0.40 + 1.60)) < 1e-9


def test_sourceless_override_is_neutral_for_any_provider(tmp_path, monkeypatch):
    """A hand-added entry with no _source prices any provider's call."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "claude-sonnet-4-6":
            input: 99.00
            output: 99.00
    """),
    )
    for prov in ("anthropic", "nous", "openrouter", ""):
        cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "claude-sonnet-4-6", prov)
        assert abs(cost - 99.00) < 1e-6, f"failed for provider={prov!r}"


def test_subscription_model_zero_cost_no_warning(tmp_path, monkeypatch, caplog):
    """A _subscription model resolves to $0.00 WITHOUT an unknown-model warning
    (issue #24, Option A): it is a declared flat-sub rate, not a lookup miss."""
    import logging

    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "qwen3.7-plus":
            input: 0.0
            output: 0.0
            _subscription: true
    """),
    )
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing"):
        cost = pricing.estimate_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            "qwen3.7-plus",
            provider="nous",
        )
    assert cost == 0.0
    assert not any("qwen3.7-plus" in r.message for r in caplog.records)


def test_subscription_models_tracked_in_loader(tmp_path, monkeypatch):
    """The loader exposes _subscription flags and routes _source-tagged
    entries into the auto-source bucket (legacy shim path)."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "qwen3.7-plus":
            input: 0.0
            output: 0.0
            _subscription: true
          "qwen/qwen3.7-plus":
            input: 0.40
            output: 1.60
            _source: openrouter
    """),
    )
    cache = pricing._load_custom_pricing()
    assert "qwen3.7-plus" in cache["subscription_models"]
    # Legacy `_source: openrouter` entries land under sources.openrouter
    assert "qwen/qwen3.7-plus" in cache["sources"]["openrouter"]
    # Untagged manual entries land in the neutral overrides bucket
    assert "qwen3.7-plus" in cache["overrides"]["*"]
    # Price-key dicts must NOT carry the _-prefixed metadata
    assert "_subscription" not in cache["overrides"]["*"]["qwen3.7-plus"]
    assert "_source" not in cache["sources"]["openrouter"]["qwen/qwen3.7-plus"]


# ── NVIDIA NIM seeds + same-id collision with OpenRouter ──


def test_nim_seed_used_for_nvidia_provider():
    """A NIM model resolves to its build.nvidia.com seed (no custom file)."""
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "nvidia/nemotron-70b-instruct",
        provider="nvidia",
    )
    assert abs(cost - (1.20 + 1.20)) < 1e-9


def test_nim_openrouter_collision_excluded_for_nvidia(tmp_path, monkeypatch):
    """Same model id on both NIM and OpenRouter at different prices: a
    provider=nvidia call must fall through the OpenRouter entry to the seed."""
    _write_pricing_yaml(
        tmp_path,
        monkeypatch,
        textwrap.dedent("""
        models:
          "nvidia/nemotron-3-super-120b-a12b":
            input: 0.09
            output: 0.45
            _source: openrouter
    """),
    )
    # provider=nvidia → OpenRouter entry excluded → _DEFAULT_PRICING seed (0.10/0.50)
    cost_nim = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "nvidia/nemotron-3-super-120b-a12b",
        provider="nvidia",
    )
    assert abs(cost_nim - (0.10 + 0.50)) < 1e-9
    # provider=openrouter → OpenRouter entry eligible (0.09/0.45)
    cost_or = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "nvidia/nemotron-3-super-120b-a12b",
        provider="openrouter",
    )
    assert abs(cost_or - (0.09 + 0.45)) < 1e-9


def test_nim_ultra_free_suffix_resolves_zero():
    """A `:free` call resolves to $0 via the ":free" suffix rule, NOT the paid
    `nemotron-3-ultra` prefix — no manual pricing entry needed. Covers both the
    bare-id free form and the OpenRouter-style suffixed free form (issue #32)."""
    bare_free = pricing.estimate_cost(
        {"input_tokens": 1_000_000}, "nvidia/nemotron-3-ultra:free", provider="nvidia"
    )
    suffixed_free = pricing.estimate_cost(
        {"input_tokens": 1_000_000},
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        provider="openrouter",
    )
    assert bare_free == 0.0
    assert suffixed_free == 0.0
    # And both are explicitly priced → recorded as known-free → transition-capable.
    assert pricing.is_explicitly_priced("nvidia/nemotron-3-ultra:free", "nvidia")
    assert pricing.is_explicitly_priced("nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter")


def test_nim_super_free_suffix_resolves_zero():
    """Regression: a seeded model's `:free` variant must resolve to $0, not the
    seeded paid price via prefix. Pre-fix, `…-super-120b-a12b:free` billed at the
    paid $0.09/$0.45 rate via prefix match (issue #32)."""
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        "nvidia/nemotron-3-super-120b-a12b:free",
        provider="openrouter",
    )
    assert cost == 0.0


def test_free_suffix_overridable_by_explicit_entry(tmp_path, monkeypatch):
    """An explicit custom `:free` entry still wins over the built-in $0 rule, so
    users can pin a different price for a `:free` id if a gateway ever charges
    for one (issue #32)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    # Pin a NONZERO price to the `:free` id: if the built-in $0 rule fired first
    # this call would cost $0, so a nonzero result proves the explicit custom
    # entry takes precedence over the suffix rule.
    (tmp_path / "telemetry" / "pricing.yaml").write_text(
        'models:\n  "nvidia/nemotron-3-ultra:free":\n    input: 1.23\n    output: 0.0\n'
    )
    pricing.reload_custom_pricing()
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000}, "nvidia/nemotron-3-ultra:free", provider="nvidia"
    )
    assert abs(cost - 1.23) < 1e-9
    pricing.reload_custom_pricing()


def test_nim_ultra_paid_resolves_bare_and_suffixed():
    """The paid seed answers both the bare id and the OpenRouter-style suffixed
    id (via prefix), so cost>0 once the promo ends — what fires the alert."""
    bare = pricing.estimate_cost(
        {"input_tokens": 1_000_000}, "nvidia/nemotron-3-ultra", provider="nvidia"
    )
    suffixed = pricing.estimate_cost(
        {"input_tokens": 1_000_000}, "nvidia/nemotron-3-ultra-550b-a55b", provider="nvidia"
    )
    assert abs(bare - 0.50) < 1e-9
    assert abs(suffixed - 0.50) < 1e-9


def test_nim_seed_survives_simulated_refresh():
    """Seeds live in _DEFAULT_PRICING (code), so they are immune to a YAML
    sync overwriting the same model id — the seed still resolves for NIM."""
    # No custom file at all: the seed must answer directly.
    cost = pricing.estimate_cost(
        {"input_tokens": 1_000_000}, "nvidia/nemotron-nano-9b", provider="nvidia"
    )
    assert abs(cost - 0.04) < 1e-9


# ---------------------------------------------------------------------------
# is_explicitly_priced — distinguishes genuine-free from unknown (issue #16)
# ---------------------------------------------------------------------------


def test_is_explicitly_priced_known_model():
    assert pricing.is_explicitly_priced("claude-sonnet-4-6") is True
    assert pricing.is_explicitly_priced("gpt-4o") is True
    assert pricing.is_explicitly_priced("nvidia/nemotron-70b-instruct", "nvidia") is True


def test_is_explicitly_priced_zero_price_model():
    assert pricing.is_explicitly_priced("owl-alpha") is True  # input=0, output=0


def test_is_explicitly_priced_unknown_model():
    assert pricing.is_explicitly_priced("completely-unknown-xyz-model") is False
    # A genuinely unknown nvidia id (no prefix seed, no ":free") stays unpriced.
    # Note: any `…:free` id IS explicitly priced ($0 via the suffix rule, issue
    # #32) — that path is covered by the NIM free-suffix tests above.
    assert pricing.is_explicitly_priced("nvidia/totally-unknown-model") is False


def test_is_explicitly_priced_subscription_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "pricing.yaml").write_text(
        "models:\n  hermes-4-qwen-72b:\n    input: 0.0\n    output: 0.0\n    _subscription: true\n"
    )
    pricing.reload_custom_pricing()
    assert pricing.is_explicitly_priced("hermes-4-qwen-72b") is True
    pricing.reload_custom_pricing()


# ---------------------------------------------------------------------------
# get_known_free_models — backfill list for pre-v5 installs (issue #16)
# ---------------------------------------------------------------------------


def test_get_known_free_models_includes_zero_price_models():
    """Models with explicit input=0, output=0 in the fixture are returned."""
    models = pricing.get_known_free_models()
    assert "owl-alpha" in models
    assert "openrouter/owl-alpha" in models


def test_get_known_free_models_no_duplicates():
    """Each model appears at most once in the result."""
    models = pricing.get_known_free_models()
    assert len(models) == len(set(models))


def test_get_known_free_models_excludes_paid_models():
    """Paid models (input > 0 or output > 0) are NOT returned."""
    models = pricing.get_known_free_models()
    assert "claude-sonnet-4-6" not in models
    assert "gpt-4o" not in models


def test_get_known_free_models_includes_subscription_models(tmp_path, monkeypatch):
    """Subscription models (_subscription: true) appear in the backfill list."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    (tmp_path / "telemetry" / "pricing.yaml").write_text(
        "models:\n"
        "  hermes-4-qwen-72b:\n"
        "    input: 0.0\n"
        "    output: 0.0\n"
        "    _subscription: true\n"
        "  paid-model:\n"
        "    input: 3.0\n"
        "    output: 15.0\n"
        "    _subscription: true\n"
    )
    pricing.reload_custom_pricing()
    models = pricing.get_known_free_models()
    assert "hermes-4-qwen-72b" in models
    assert "paid-model" not in models
    pricing.reload_custom_pricing()
