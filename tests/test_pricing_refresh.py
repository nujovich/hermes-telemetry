"""Tests for pricing_refresh sources — GoogleAISource and registry integration."""

from __future__ import annotations

import datetime as dt

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


def test_manual_subscription_entry_survives_refresh(tmp_path, monkeypatch):
    """A hand-added `_subscription` entry under the provider-native (bare) id is
    never returned by OpenRouter, so a refresh leaves it untouched and records
    it in _meta.subscription_models — not in estimated_price_models."""
    import yaml

    import paths
    import pricing_refresh

    pfile = paths.get_pricing_path()
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "qwen3.7-plus": {"input": 0.0, "output": 0.0, "_subscription": True},
                },
                "defaults": {"cache_read_multiplier": 0.10, "cache_write_multiplier": 1.25},
            }
        )
    )
    # OpenRouter returns the PREFIXED form — a different key, so no collision.
    monkeypatch.setattr(
        pricing_refresh,
        "_SOURCES",
        [_StubSource({"qwen/qwen3.7-plus": {"input": 0.40, "output": 1.60}})],
    )

    changes, _overrides = pricing_refresh.refresh_pricing()

    written = yaml.safe_load(pfile.read_text())
    models = written["models"]
    meta = written["_meta"]
    # Manual subscription entry preserved verbatim
    assert models["qwen3.7-plus"]["_subscription"] is True
    assert models["qwen3.7-plus"]["input"] == 0.0
    # OpenRouter prefixed form added alongside (different key — no clobber)
    assert models["qwen/qwen3.7-plus"]["input"] == 0.40
    # Meta classifies the subscription model correctly
    assert "qwen3.7-plus" in meta["subscription_models"]
    assert "qwen3.7-plus" not in meta.get("estimated_price_models", [])


def test_refresh_pricing_honors_telemetry_home_over_hermes_home(tmp_path, monkeypatch):
    """refresh_pricing must resolve pricing.yaml via paths.get_pricing_path(),
    honoring HERMES_TELEMETRY_HOME over HERMES_HOME. Regression: it wrote to a
    module-level PRICING_FILE computed from HERMES_HOME at import time, so a set
    HERMES_TELEMETRY_HOME (the consolidated cost-center dir used to unify profiles
    on the VPS) was silently ignored and refresh landed in the wrong file."""
    import yaml

    import paths
    import pricing_refresh

    hermes_home = tmp_path / "hermes_home"
    telemetry_home = tmp_path / "telemetry_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(telemetry_home))

    monkeypatch.setattr(
        pricing_refresh,
        "_SOURCES",
        [_StubSource({"acme/model-x": {"input": 1.0, "output": 2.0}})],
    )

    pricing_refresh.refresh_pricing()

    # HERMES_TELEMETRY_HOME outranks HERMES_HOME → refresh must write here.
    target = telemetry_home / "telemetry" / "pricing.yaml"
    assert target == paths.get_pricing_path()
    assert target.exists()
    assert yaml.safe_load(target.read_text())["models"]["acme/model-x"]["input"] == 1.0
    # ...and must NOT write to the HERMES_HOME location.
    assert not (hermes_home / "telemetry" / "pricing.yaml").exists()
