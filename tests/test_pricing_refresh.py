"""Tests for pricing_refresh sources — GoogleAISource and registry integration."""

from __future__ import annotations

import datetime as dt
import textwrap

from pricing import _DEFAULT_PRICING
from pricing_refresh import (
    _SOURCES,
    GoogleAISource,
    OpenRouterSource,
    PricingSource,
    register_source,
)

# ---------------------------------------------------------------------------
# GoogleAISource — shape and contract
# ---------------------------------------------------------------------------


def test_google_ai_source_is_pricing_source_subclass():
    assert issubclass(GoogleAISource, PricingSource)


def test_google_ai_source_has_required_class_attrs():
    assert GoogleAISource.name == "google-ai"
    assert isinstance(GoogleAISource.LAST_VERIFIED, str)
    # Must parse as ISO date so the LAST_VERIFIED contract is enforceable.
    dt.date.fromisoformat(GoogleAISource.LAST_VERIFIED)


def test_google_ai_source_fetch_returns_non_empty_dict():
    result = GoogleAISource().fetch()
    assert isinstance(result, dict)
    assert len(result) > 0


def test_google_ai_source_entries_have_required_fields():
    result = GoogleAISource().fetch()
    for model, pricing in result.items():
        assert "input" in pricing, f"{model} missing input"
        assert "output" in pricing, f"{model} missing output"
        assert "cache_read" in pricing, f"{model} missing cache_read"
        assert isinstance(pricing["input"], (int, float))
        assert isinstance(pricing["output"], (int, float))
        assert isinstance(pricing["cache_read"], (int, float))


def test_google_ai_source_prices_are_positive():
    result = GoogleAISource().fetch()
    for model, pricing in result.items():
        assert pricing["input"] > 0, f"{model} has non-positive input price"
        assert pricing["output"] > 0, f"{model} has non-positive output price"
        assert pricing["cache_read"] > 0, f"{model} has non-positive cache_read price"


def test_google_ai_source_cache_read_below_input():
    """Cache reads are always cheaper than fresh input tokens (Google AI policy)."""
    result = GoogleAISource().fetch()
    for model, pricing in result.items():
        assert pricing["cache_read"] < pricing["input"], (
            f"{model}: cache_read ({pricing['cache_read']}) should be < input ({pricing['input']})"
        )


def test_google_ai_source_uses_bare_model_ids():
    """Keys must not carry the 'google/' prefix — that's OpenRouterSource's territory."""
    result = GoogleAISource().fetch()
    for model in result:
        assert not model.startswith("google/"), (
            f"{model} should not have 'google/' prefix in GoogleAISource"
        )
        assert model.startswith("gemini-"), f"{model} should be a Gemini model id"


# ---------------------------------------------------------------------------
# Consistency with _DEFAULT_PRICING in pricing.py
# ---------------------------------------------------------------------------


def test_google_ai_source_matches_default_pricing():
    """GoogleAISource must agree with the embedded _DEFAULT_PRICING for shared keys.

    If they diverge, either the source is stale (bump LAST_VERIFIED + table)
    or _DEFAULT_PRICING is stale (sync from source).
    """
    google = GoogleAISource().fetch()
    for model, pricing in google.items():
        if model not in _DEFAULT_PRICING:
            continue  # source may carry models not yet in defaults — OK
        default = _DEFAULT_PRICING[model]
        for field in ("input", "output", "cache_read"):
            if field not in default:
                continue
            assert pricing[field] == default[field], (
                f"{model}.{field}: source={pricing[field]} default={default[field]}"
            )


def test_google_ai_source_covers_all_default_gemini_models():
    """Every Gemini entry in _DEFAULT_PRICING should be present in GoogleAISource.

    Catches the failure mode where _DEFAULT_PRICING gains a new Gemini model
    but the new source isn't updated.
    """
    gemini_defaults = {k for k in _DEFAULT_PRICING if k.startswith("gemini-")}
    source_models = set(GoogleAISource().fetch())
    missing = gemini_defaults - source_models
    assert not missing, (
        f"GoogleAISource missing Gemini models from _DEFAULT_PRICING: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_google_ai_source_is_registered():
    assert GoogleAISource in _SOURCES


def test_openrouter_source_is_registered():
    """Sanity check — ensures we didn't replace OpenRouterSource by accident."""
    assert OpenRouterSource in _SOURCES


def test_register_source_appends_to_registry():
    class _DummySource(PricingSource):
        name = "dummy"

        def fetch(self) -> dict[str, dict]:
            return {}

    original_len = len(_SOURCES)
    try:
        register_source(_DummySource)
        assert _DummySource in _SOURCES
        assert len(_SOURCES) == original_len + 1
    finally:
        _SOURCES.remove(_DummySource)


# ---------------------------------------------------------------------------
# Fetch returns independent copies
# ---------------------------------------------------------------------------


def test_google_ai_source_fetch_returns_copies_not_references():
    """Mutating the returned dict must not corrupt the source's class table."""
    src = GoogleAISource()
    first = src.fetch()
    first_model = next(iter(first))
    first[first_model]["input"] = 99999.0
    second = src.fetch()
    assert second[first_model]["input"] != 99999.0, (
        "fetch() should return fresh copies, not references to class state"
    )


# ---------------------------------------------------------------------------
# Subscription-model meta + survival across refresh (issue #24, Option A)
# ---------------------------------------------------------------------------


def _StubSource(models: dict[str, dict]):
    class _S(PricingSource):
        name = "openrouter"  # impersonate OpenRouter so entries carry that source

        def fetch(self) -> dict[str, dict]:
            return {k: dict(v) for k, v in models.items()}

    return _S


def test_refresh_writes_auto_file_only(tmp_path, monkeypatch):
    """v0.6: refresh writes pricing.auto.yaml and never touches pricing.yaml.

    Manual `_subscription` entries live in pricing.yaml's `overrides:` and
    can't collide with auto entries — collision-free by construction.
    """
    import yaml

    import pricing_refresh

    pfile = tmp_path / "pricing.yaml"
    auto_file = tmp_path / "pricing.auto.yaml"
    manual_yaml = textwrap.dedent("""
        overrides:
          "*":
            "qwen3.7-plus":
              input: 0.0
              output: 0.0
              _subscription: true
        defaults:
          cache_read_multiplier: 0.10
          cache_write_multiplier: 1.25
        """)
    pfile.write_text(manual_yaml)
    monkeypatch.setattr(pricing_refresh, "PRICING_FILE", pfile)
    monkeypatch.setattr(pricing_refresh, "AUTO_FILE", auto_file)
    monkeypatch.setattr(
        pricing_refresh,
        "_SOURCES",
        [_StubSource({"qwen/qwen3.7-plus": {"input": 0.40, "output": 1.60}})],
    )

    changes, overrides = pricing_refresh.refresh_pricing()

    # pricing.yaml untouched
    assert pfile.read_text() == manual_yaml
    # pricing.auto.yaml created with the openrouter entry
    written = yaml.safe_load(auto_file.read_text())
    assert "qwen/qwen3.7-plus" in written["sources"]["openrouter"]
    assert written["sources"]["openrouter"]["qwen/qwen3.7-plus"]["input"] == 0.40
    # No manual-override tracking in the new schema
    assert overrides == []
    assert "qwen/qwen3.7-plus" in changes
