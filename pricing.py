"""Pricing table for cost estimation.

Prices are USD per 1 million tokens, loaded from:
  1. ~/.hermes/telemetry/pricing.yaml  (user override, optional)
  2. _DEFAULT_PRICING (embedded fallback)

YAML override format:
  models:
    "model-name":
      input: 3.00
      output: 15.00
      cache_read: 0.30      # optional
      cache_write: 3.75     # optional
      reasoning: 15.00      # optional
  defaults:
    cache_read_multiplier: 0.10
    cache_write_multiplier: 1.25

Unknown models: cost = 0.0, warning logged once per model name.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# USD per 1M tokens: dict with keys input, output, cache_read, cache_write
# (reasoning defaults to output price if not specified)
# Sources: official provider pricing pages, May 2026
_DEFAULT_PRICING: dict[str, dict] = {
    # ── Anthropic / Nous Portal ──────────────────────────────────────────────
    "claude-opus-4-8": dict(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-7": dict(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25),
    "claude-sonnet-4-6": dict(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-4-5": dict(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-haiku-4-5": dict(input=0.80, output=4.00, cache_read=0.08, cache_write=1.00),
    "claude-opus-4": dict(input=15.00, output=75.00, cache_read=1.50, cache_write=18.75),
    "claude-sonnet-4": dict(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-3-5-sonnet-20241022": dict(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-3-5-haiku-20241022": dict(input=0.80, output=4.00, cache_read=0.08, cache_write=1.00),
    "claude-3-opus-20240229": dict(input=15.00, output=75.00, cache_read=1.50, cache_write=18.75),
    "claude-3-haiku-20240307": dict(input=0.25, output=1.25, cache_read=0.03, cache_write=0.30),
    # ── OpenAI ──────────────────────────────────────────────────────────────
    "gpt-4o": dict(input=2.50, output=10.00),
    "gpt-4o-mini": dict(input=0.15, output=0.60),
    "gpt-4-turbo": dict(input=10.00, output=30.00),
    "gpt-4": dict(input=30.00, output=60.00),
    "gpt-3.5-turbo": dict(input=0.50, output=1.50),
    "o1": dict(input=15.00, output=60.00),
    "o1-mini": dict(input=3.00, output=12.00),
    "o3": dict(input=10.00, output=40.00),
    "o3-mini": dict(input=1.10, output=4.40),
    "o4-mini": dict(input=1.10, output=4.40),
    # ── DeepSeek ────────────────────────────────────────────────────────────
    "deepseek-chat": dict(input=0.27, output=1.10),
    "deepseek-v3": dict(input=0.27, output=1.10),
    "deepseek-r1": dict(input=0.55, output=2.19),
    # ── Nous Research (Portal) ───────────────────────────────────────────────
    "owl-alpha": dict(input=0.00, output=0.00),
    "hermes-3-llama-3.1-405b": dict(input=3.00, output=15.00),
    "hermes-3-llama-3.1-70b": dict(input=0.70, output=0.90),
    # ── Meta (via OpenRouter / providers) ───────────────────────────────────
    "meta-llama/llama-3.1-405b-instruct": dict(input=2.70, output=2.70),
    "meta-llama/llama-3.1-70b-instruct": dict(input=0.52, output=0.75),
    "meta-llama/llama-3.3-70b-instruct": dict(input=0.59, output=0.79),
    # ── Google ──────────────────────────────────────────────────────────────
    # Prices verified at https://ai.google.dev/gemini-api/docs/pricing on 2026-06-05.
    # Removed deprecated models: gemini-1.5-pro/-flash (off pricing page),
    # gemini-2.0-flash/-lite (sunset 2026-06-01). Tiered-pricing models
    # (gemini-2.5-pro, gemini-3.1-pro-preview) use the <=200k context tier;
    # >200k usage is undercounted (separate issue to track).
    "gemini-3.5-flash": dict(input=1.50, output=9.00, cache_read=0.15),
    "gemini-3.1-pro-preview": dict(input=2.00, output=12.00, cache_read=0.20),
    "gemini-3.1-flash-lite": dict(input=0.25, output=1.50, cache_read=0.025),
    "gemini-3-flash-preview": dict(input=0.50, output=3.00, cache_read=0.05),
    "gemini-2.5-pro": dict(input=1.25, output=10.00, cache_read=0.125),
    "gemini-2.5-flash": dict(input=0.30, output=2.50, cache_read=0.03),
    "gemini-2.5-flash-lite": dict(input=0.10, output=0.40, cache_read=0.01),
}

# Prefix-based fallback for model families (matched in order, longest first)
_PREFIX_PRICING: list[tuple[str, dict]] = [
    ("claude-opus", dict(input=5.00, output=25.00, cache_read=0.50, cache_write=6.25)),
    ("claude-sonnet", dict(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75)),
    ("claude-haiku", dict(input=0.80, output=4.00, cache_read=0.08, cache_write=1.00)),
    ("gpt-4o-mini", dict(input=0.15, output=0.60)),
    ("gpt-4o", dict(input=2.50, output=10.00)),
    ("gpt-4", dict(input=10.00, output=30.00)),
    ("gpt-3.5", dict(input=0.50, output=1.50)),
    ("o1-mini", dict(input=3.00, output=12.00)),
    ("o1", dict(input=15.00, output=60.00)),
    ("o3-mini", dict(input=1.10, output=4.40)),
    ("o3", dict(input=10.00, output=40.00)),
    ("o4-mini", dict(input=1.10, output=4.40)),
    ("deepseek-r1", dict(input=0.55, output=2.19)),
    ("deepseek", dict(input=0.27, output=1.10)),
    # Gemini family prefixes catch dated variants (e.g. gemini-3-flash-preview-20251217).
    # Specific prefixes only — no generic "gemini" catch-all, since Flash 1.5 is
    # deprecated and a bare "gemini" prefix would mis-price unknown models. An
    # unknown gemini variant now logs a warning instead of being silently mis-priced.
    ("gemini-3.1-flash-lite", dict(input=0.25, output=1.50, cache_read=0.025)),
    ("gemini-2.5-flash-lite", dict(input=0.10, output=0.40, cache_read=0.01)),
    ("gemini-3.5-flash", dict(input=1.50, output=9.00, cache_read=0.15)),
    ("gemini-3.1-pro", dict(input=2.00, output=12.00, cache_read=0.20)),
    ("gemini-3-flash", dict(input=0.50, output=3.00, cache_read=0.05)),
    ("gemini-2.5-flash", dict(input=0.30, output=2.50, cache_read=0.03)),
    ("gemini-2.5-pro", dict(input=1.25, output=10.00, cache_read=0.125)),
    ("llama-3.1-405", dict(input=2.70, output=2.70)),
    ("llama-3.1-70", dict(input=0.52, output=0.75)),
    ("llama-3.3-70", dict(input=0.59, output=0.79)),
]

_DEFAULT_CACHE_READ_MULTIPLIER = 0.10
_DEFAULT_CACHE_WRITE_MULTIPLIER = 1.25

_warned_unknown: set[str] = set()
_custom_pricing: dict | None = None  # parsed YAML data (models + defaults)


def _load_custom_pricing() -> dict:
    """Load custom pricing YAML. Returns dict with 'models' and 'defaults' keys."""
    global _custom_pricing
    if _custom_pricing is not None:
        return _custom_pricing
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    pricing_file = hermes_home / "telemetry" / "pricing.yaml"
    if not pricing_file.exists():
        _custom_pricing = {"models": {}, "defaults": {}}
        return _custom_pricing
    try:
        import yaml

        with open(pricing_file) as f:
            data = yaml.safe_load(f) or {}

        models: dict[str, dict] = {}
        defaults: dict[str, float] = {}

        # New format: top-level "models:" and "defaults:" sections
        if "models" in data or "defaults" in data:
            raw_models = data.get("models") or {}
            raw_defaults = data.get("defaults") or {}
            for model, entry in raw_models.items():
                if isinstance(entry, dict):
                    models[str(model).lower()] = {
                        k: float(v)
                        for k, v in entry.items()
                        if v is not None and not k.startswith("_")
                    }
            for k, v in raw_defaults.items():
                with contextlib.suppress(TypeError, ValueError):
                    defaults[str(k)] = float(v)
        else:
            # Legacy flat format: model_name: {input: ..., output: ...}
            for model, entry in data.items():
                if isinstance(entry, dict):
                    models[str(model).lower()] = {
                        k: float(v) for k, v in entry.items() if v is not None
                    }

        _custom_pricing = {"models": models, "defaults": defaults}
    except Exception as exc:
        logger.warning("Failed to load custom pricing from %s: %s", pricing_file, exc)
        _custom_pricing = {"models": {}, "defaults": {}}
    return _custom_pricing


def _google_alt_form(model_lc: str) -> str | None:
    """Return the alternate Google-AI form for symmetric lookup, or None.

    Maps the pair `gemini-X` ↔ `google/gemini-X` so a single canonical price
    table answers both direct-Google and OpenRouter-routed lookups. This is
    deliberately google-specific: other provider prefixes (`anthropic/`,
    `meta-llama/`, `openrouter/`) carry distinct pricing semantics and must
    never be stripped naively.
    """
    if model_lc.startswith("google/"):
        bare = model_lc[len("google/") :]
        return bare if bare.startswith("gemini-") else None
    if model_lc.startswith("gemini-"):
        return "google/" + model_lc
    return None


def _lookup_form(model_lc: str) -> dict | None:
    """Exact-then-prefix lookup against custom + defaults + prefix tables.

    Custom wins over defaults wins over the curated prefix table (matching
    the precedence in `_lookup_base`'s callers). Among equal-length prefixes,
    the stable sort preserves source order so the higher-precedence source
    still wins.
    """
    custom = _load_custom_pricing()
    custom_models = custom.get("models", {})
    if model_lc in custom_models:
        return custom_models[model_lc]
    if model_lc in _DEFAULT_PRICING:
        return _DEFAULT_PRICING[model_lc]
    # Prefix fallback: scan ALL known keys — custom (auto-refreshed + user) and
    # default exact keys, plus the curated family-prefix table — longest prefix
    # wins. This lets an auto-refreshed key like 'google/gemini-3-flash-preview'
    # cover the dated variants the gateway actually sends, e.g.
    # 'google/gemini-3-flash-preview-20251217'.
    candidates: list[tuple[str, dict]] = [
        *custom_models.items(),
        *_DEFAULT_PRICING.items(),
        *_PREFIX_PRICING,
    ]
    for prefix, prices in sorted(candidates, key=lambda x: -len(x[0])):
        if model_lc.startswith(prefix):
            return prices
    return None


def _lookup_base(model: str) -> dict | None:
    """Return the raw pricing dict for a model (no cache derivation yet).

    Two-pass strategy: first try the model id as-is. If that misses, try the
    Google-AI alternate form (`gemini-X` ↔ `google/gemini-X`) so direct-Google
    and OpenRouter-routed callers get identical pricing without requiring
    both entries to coexist in the pricing data. Non-Google prefixes never
    get this treatment — see `_google_alt_form`.
    """
    model_lc = model.lower()
    result = _lookup_form(model_lc)
    if result is not None:
        return result
    alt = _google_alt_form(model_lc)
    if alt is not None:
        return _lookup_form(alt)
    return None


def _resolve_pricing(model: str) -> dict | None:
    """Return a fully-resolved pricing dict with all 5 keys.

    Derives cache prices from multipliers if not explicitly set.
    Returns None if model is completely unknown.
    """
    base = _lookup_base(model)
    if base is None:
        return None

    custom = _load_custom_pricing()
    defaults = custom.get("defaults", {})
    cache_read_mult = float(defaults.get("cache_read_multiplier", _DEFAULT_CACHE_READ_MULTIPLIER))
    cache_write_mult = float(
        defaults.get("cache_write_multiplier", _DEFAULT_CACHE_WRITE_MULTIPLIER)
    )

    input_price = float(base.get("input", 0.0))
    output_price = float(base.get("output", 0.0))

    if "cache_read" in base:
        cache_read = float(base["cache_read"])
    else:
        cache_read = input_price * cache_read_mult
        logger.debug(
            "hermes-telemetry: model %r has no explicit cache_read price — "
            "deriving from input * %.2f = %.4f",
            model,
            cache_read_mult,
            cache_read,
        )

    if "cache_write" in base:
        cache_write = float(base["cache_write"])
    else:
        cache_write = input_price * cache_write_mult
        logger.debug(
            "hermes-telemetry: model %r has no explicit cache_write price — "
            "deriving from input * %.2f = %.4f",
            model,
            cache_write_mult,
            cache_write,
        )

    # reasoning defaults to output price unless overridden
    reasoning = float(base.get("reasoning", output_price))

    return dict(
        input=input_price,
        output=output_price,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
    )


def estimate_cost(usage: dict, model: str) -> float:
    """Return estimated cost in USD for a usage dict.

    usage dict keys (all optional/nullable):
      input_tokens        — non-cached input tokens
      output_tokens       — output tokens
      cache_read_tokens   — tokens served from cache (cheaper)
      cache_write_tokens  — tokens written to cache (more expensive)
      reasoning_tokens    — reasoning/thinking tokens (billed as output by default)

    prompt_tokens is intentionally ignored to avoid double-counting
    (prompt_tokens = input + cache_read + cache_write in Hermes canonical usage).

    Returns 0.0 for unknown models (with a one-time warning) or empty usage.
    """
    if not model:
        return 0.0
    if not isinstance(usage, dict):
        return 0.0

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read_tok = int(usage.get("cache_read_tokens") or 0)
    cache_write_tok = int(usage.get("cache_write_tokens") or 0)
    reasoning_tok = int(usage.get("reasoning_tokens") or 0)

    if (
        input_tokens == 0
        and output_tokens == 0
        and cache_read_tok == 0
        and cache_write_tok == 0
        and reasoning_tok == 0
    ):
        return 0.0

    prices = _resolve_pricing(model)
    if prices is None:
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning(
                "hermes-telemetry: unknown model %r — cost recorded as $0.00. "
                "Add to ~/.hermes/telemetry/pricing.yaml to fix.",
                model,
            )
        return 0.0

    cost = (
        input_tokens * prices["input"]
        + output_tokens * prices["output"]
        + cache_read_tok * prices["cache_read"]
        + cache_write_tok * prices["cache_write"]
        + reasoning_tok * prices["reasoning"]
    ) / 1_000_000

    return cost


def reload_custom_pricing() -> None:
    """Force-reload the user pricing file (useful in tests)."""
    global _custom_pricing
    _custom_pricing = None
