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

try:
    from . import paths
except ImportError:  # pragma: no cover - pricing.py loaded standalone (no package context)
    import paths

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
    # ── NVIDIA NIM (build.nvidia.com direct) ─────────────────────────────────
    # Hermes canonicalizes the NIM provider to "nvidia" (aliases nim/nvidia-nim/
    # nemotron normalize to it). Seeds live here, source-neutral and in code, so
    # they (1) survive an OpenRouter sync untouched and (2) are selected when the
    # provider-aware guard excludes a same-id OpenRouter entry for a NIM call.
    # Prices per build.nvidia.com, verified 2026-06. `:free` promo variants
    # (e.g. nemotron-3-super-120b-a12b:free) resolve to $0 via the ":free"
    # suffix rule in `_lookup_form` and need no entry — see issue #12.
    "nvidia/nemotron-3-super-120b-a12b": dict(input=0.10, output=0.50),
    "nvidia/nemotron-super-49b": dict(input=0.10, output=0.40),
    "nvidia/nemotron-70b-instruct": dict(input=1.20, output=1.20),
    "nvidia/nemotron-nano-12b-vl": dict(input=0.20, output=0.60),
    "nvidia/nemotron-nano-9b": dict(input=0.04, output=0.16),
    # nemotron-3-ultra: the `:free` promo ends 2026-06-18, after which the
    # gateway drops the `:free` suffix and bills `nvidia/nemotron-3-ultra` (and
    # the suffixed `…-550b-a55b` form, caught by prefix match). While the promo
    # is live, the `…:free` ids resolve to $0 via the ":free" suffix rule (not
    # this seed). Seeding the paid price here makes cost>0 once the promo ends,
    # which is what fires the free→paid transition alert (issues #16/#32). Price
    # is the OpenRouter rate pending confirmation of the NIM-direct figure.
    "nvidia/nemotron-3-ultra": dict(input=0.50, output=2.50),
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

_warned_unknown: set[tuple[str, str]] = set()
# (model, provider) pairs already warned about a provider-assumed price (issue
# #42): a source-ineligible entry was applied as a best-effort estimate rather
# than recording a silent $0. Deduped so the warning fires once per pair.
_warned_provider_assumed: set[tuple[str, str]] = set()
_custom_pricing: dict | None = None  # parsed YAML data (models + defaults)


def _empty_custom_pricing() -> dict:
    return {"models": {}, "defaults": {}, "model_sources": {}, "subscription_models": set()}


def _load_custom_pricing() -> dict:
    """Load custom pricing YAML.

    Returns a dict with keys:
      models               — {model_lc: {price keys}}  (``_``-prefixed keys stripped)
      defaults             — {multiplier name: float}
      model_sources        — {model_lc: source}  (from each entry's ``_source``)
      subscription_models  — set of model_lc flagged ``_subscription: true``

    ``model_sources`` is what powers the provider-aware guard (issue #24): the
    price-key dict has the ``_``-prefixed metadata stripped, so the source has
    to be captured separately before stripping or the guard can't see it.
    """
    global _custom_pricing
    if _custom_pricing is not None:
        return _custom_pricing
    pricing_file = paths.get_pricing_path()
    if not pricing_file.exists():
        _custom_pricing = _empty_custom_pricing()
        return _custom_pricing
    try:
        import yaml

        with open(pricing_file) as f:
            data = yaml.safe_load(f) or {}

        models: dict[str, dict] = {}
        defaults: dict[str, float] = {}
        model_sources: dict[str, str] = {}
        subscription_models: set[str] = set()

        # New format has top-level "models:"/"defaults:"; legacy is a flat map of
        # model_name -> entry. Either way, entries are dicts whose price keys we
        # keep and whose ``_``-prefixed metadata (_source, _subscription, ...) we
        # capture then strip.
        if "models" in data or "defaults" in data:
            raw_models = data.get("models") or {}
            raw_defaults = data.get("defaults") or {}
            for k, v in raw_defaults.items():
                with contextlib.suppress(TypeError, ValueError):
                    defaults[str(k)] = float(v)
        else:
            raw_models = data

        for model, entry in raw_models.items():
            if not isinstance(entry, dict):
                continue
            key = str(model).lower()
            models[key] = {
                k: float(v) for k, v in entry.items() if v is not None and not k.startswith("_")
            }
            src = entry.get("_source")
            if src:
                model_sources[key] = str(src).lower()
            if entry.get("_subscription"):
                subscription_models.add(key)

        _custom_pricing = {
            "models": models,
            "defaults": defaults,
            "model_sources": model_sources,
            "subscription_models": subscription_models,
        }
    except Exception as exc:
        logger.warning("Failed to load custom pricing from %s: %s", pricing_file, exc)
        _custom_pricing = _empty_custom_pricing()
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


def _source_eligible(source: str | None, provider: str) -> bool:
    """Whether a pricing entry from `source` may cost a call served by `provider`.

    Provider-aware guard (issue #24): an OpenRouter-sourced price must never
    cost a call a *different* provider actually served — that silently applies
    the wrong rate (e.g. the OpenRouter Qwen price on a Nous Portal call, or the
    OpenRouter rate on a same-id NVIDIA NIM call). Rules:

    - Source-less entries (`_DEFAULT_PRICING`, the prefix table, hand-added
      overrides with no `_source`) are provider-neutral → always eligible.
    - `_source: openrouter` entries are eligible only when the call has no
      provider (empty → backward-compat / unknown) or is itself OpenRouter-routed.
    - Other named sources (e.g. `google-ai`) are not restricted: their prices are
      direct-provider rates that stay reasonable for any caller of that model id.
    """
    if not source or source != "openrouter":
        return True
    if not provider:
        return True
    return "openrouter" in provider.lower()


def _lookup_form(model_lc: str, provider: str = "") -> dict | None:
    """Exact-then-prefix lookup against custom + defaults + prefix tables.

    Custom wins over defaults wins over the curated prefix table (matching
    the precedence in `_lookup_base`'s callers). Among equal-length prefixes,
    the stable sort preserves source order so the higher-precedence source
    still wins.

    `provider` drives the source guard (`_source_eligible`): a source-ineligible
    custom entry is skipped so the lookup falls through to the next candidate
    (e.g. a NIM call skips the same-id OpenRouter entry and lands on the
    source-neutral `_DEFAULT_PRICING` seed).

    Inverted last-resort (issue #42): if NO source-eligible candidate matches but
    a source-ineligible one would have, the lookup returns that price tagged
    ``_provider_assumed: True`` instead of ``None``. For a cost tracker, applying
    a best-effort rate (with a one-time warning at `estimate_cost`) beats
    silently recording $0 on a real paid call. Source-eligible matches and the
    `_DEFAULT_PRICING`/`:free` rules always win first, so this only fires when the
    sole price available is one the guard would otherwise reject — the common
    "popular model resold at the OpenRouter rate" case (e.g. Nous Portal serving
    `moonshotai/kimi-k2.6`). A source-neutral override or `_subscription` entry
    still pre-empts it.
    """
    custom = _load_custom_pricing()
    custom_models = custom.get("models", {})
    model_sources = custom.get("model_sources", {})

    # Best source-ineligible match seen, used only if nothing eligible matches.
    # An ineligible *exact* match outranks any ineligible prefix match.
    assumed: dict | None = None

    if model_lc in custom_models:
        if _source_eligible(model_sources.get(model_lc), provider):
            return custom_models[model_lc]
        assumed = custom_models[model_lc]
    if model_lc in _DEFAULT_PRICING:
        return _DEFAULT_PRICING[model_lc]
    # Free-tier suffix: OpenRouter (and similar gateways) advertise free variants
    # with a ":free" suffix, e.g. "nvidia/nemotron-3-ultra-550b-a55b:free". These
    # are $0 by definition. Short-circuit to an explicit zero price BEFORE the
    # prefix scan, for two reasons:
    #   1. Otherwise the suffixed free id inherits its paid base price via prefix
    #      (e.g. the "nvidia/nemotron-3-ultra" seed would price "…-550b-a55b:free"
    #      at $0.50/$2.50 — billing a free call as paid).
    #   2. Returning an explicit zero dict (not the unknown-model None) makes the
    #      call resolve as known-free: no estimated-price warning, and recorded in
    #      known_free_models so the free→paid alert fires when the gateway later
    #      drops the ":free" suffix and starts charging (issues #16/#32).
    # A user's explicit ":free" entry (matched above) still wins over this rule.
    if model_lc.endswith(":free"):
        return {"input": 0.0, "output": 0.0}
    # Prefix fallback: scan ALL known keys — custom (auto-refreshed + user) and
    # default exact keys, plus the curated family-prefix table — longest prefix
    # wins. This lets an auto-refreshed key like 'google/gemini-3-flash-preview'
    # cover the dated variants the gateway actually sends, e.g.
    # 'google/gemini-3-flash-preview-20251217'. Source-ineligible custom keys are
    # skipped here too.
    candidates: list[tuple[str, dict, str | None]] = [
        *((k, v, model_sources.get(k)) for k, v in custom_models.items()),
        *((k, v, None) for k, v in _DEFAULT_PRICING.items()),
        *((k, v, None) for k, v in _PREFIX_PRICING),
    ]
    for prefix, prices, source in sorted(candidates, key=lambda x: -len(x[0])):
        if model_lc.startswith(prefix):
            if _source_eligible(source, provider):
                return prices
            if assumed is None:
                # Longest ineligible prefix (candidates are sorted longest-first).
                assumed = prices
    if assumed is not None:
        return {**assumed, "_provider_assumed": True}
    return None


def _lookup_base(model: str, provider: str = "") -> dict | None:
    """Return the raw pricing dict for a model (no cache derivation yet).

    Two-pass strategy: first try the model id as-is. If that misses, try the
    Google-AI alternate form (`gemini-X` ↔ `google/gemini-X`) so direct-Google
    and OpenRouter-routed callers get identical pricing without requiring
    both entries to coexist in the pricing data. Non-Google prefixes never
    get this treatment — see `_google_alt_form`.
    """
    model_lc = model.lower()
    result = _lookup_form(model_lc, provider)
    if result is not None:
        return result
    alt = _google_alt_form(model_lc)
    if alt is not None:
        return _lookup_form(alt, provider)
    return None


def _resolve_pricing(model: str, provider: str = "") -> dict | None:
    """Return a fully-resolved pricing dict with all 5 keys.

    Derives cache prices from multipliers if not explicitly set.
    Returns None if model is completely unknown.
    """
    base = _lookup_base(model, provider)
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

    resolved = dict(
        input=input_price,
        output=output_price,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
    )
    # Propagate the provider-assumed marker (issue #42) so estimate_cost can warn.
    if base.get("_provider_assumed"):
        resolved["_provider_assumed"] = True
    return resolved


def estimate_cost(usage: dict, model: str, provider: str = "") -> float:
    """Return estimated cost in USD for a usage dict.

    usage dict keys (all optional/nullable):
      input_tokens        — non-cached input tokens
      output_tokens       — output tokens
      cache_read_tokens   — tokens served from cache (cheaper)
      cache_write_tokens  — tokens written to cache (more expensive)
      reasoning_tokens    — reasoning/thinking tokens (billed as output by default)

    `provider` (verbatim from the gateway, e.g. "nous", "nvidia", "openrouter")
    makes the lookup provider-aware (issue #24): an OpenRouter-sourced price is
    never applied to a call another provider served. `provider=""` keeps the
    historical provider-blind behaviour for backward compatibility.

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

    prices = _resolve_pricing(model, provider)
    if prices is None:
        # Dedup per (model, provider): the same model id can be unpriced under
        # one provider yet priced under another, so each pairing warns once.
        warn_key = (model, provider)
        if warn_key not in _warned_unknown:
            _warned_unknown.add(warn_key)
            if provider:
                logger.warning(
                    "hermes-telemetry: no price for model %r under provider %r — "
                    "cost recorded as $0.00. Add it (under the provider's native "
                    "model id) to ~/.hermes/telemetry/pricing.yaml to fix.",
                    model,
                    provider,
                )
            else:
                logger.warning(
                    "hermes-telemetry: unknown model %r — cost recorded as $0.00. "
                    "Add to ~/.hermes/telemetry/pricing.yaml to fix.",
                    model,
                )
        return 0.0

    # Provider-assumed price (issue #42): the only matching entry was source-
    # ineligible for this provider, so we applied it as a best-effort estimate
    # rather than recording a silent $0. Warn once per (model, provider) so the
    # user can pin the rate explicitly — far safer than a silent under-count.
    if prices.get("_provider_assumed"):
        warn_key = (model, provider)
        if warn_key not in _warned_provider_assumed:
            _warned_provider_assumed.add(warn_key)
            logger.warning(
                "hermes-telemetry: no price registered for model %r under provider "
                "%r — applying an OpenRouter-sourced rate as a best-effort estimate "
                "(cost may be inaccurate if %r bills differently). To pin the rate "
                "and silence this, add models[%r] to ~/.hermes/telemetry/pricing.yaml "
                "(omit _source, or set _subscription: true for a flat-rate/free plan).",
                model,
                provider,
                provider,
                model,
            )

    cost = (
        input_tokens * prices["input"]
        + output_tokens * prices["output"]
        + cache_read_tok * prices["cache_read"]
        + cache_write_tok * prices["cache_write"]
        + reasoning_tok * prices["reasoning"]
    ) / 1_000_000

    return cost


def is_explicitly_priced(model: str, provider: str = "") -> bool:
    """Return True if *model* has an explicit pricing entry (even if $0).

    Distinguishes genuinely-free models (explicit zero price or _subscription)
    from unknown models (no entry at all, which also produce cost==0 via the
    fallback). Only explicitly-priced-at-$0 models are recorded in
    known_free_models and can trigger the free→paid transition alert.
    """
    return _resolve_pricing(model, provider) is not None


def is_provider_assumed(model: str, provider: str = "") -> bool:
    """Return True if pricing *model* under *provider* relies on a provider-assumed
    rate (issue #42).

    True only when the lookup had no source-eligible price and fell back to a
    source-ineligible entry (an OpenRouter rate applied to a different provider)
    as a best-effort estimate. The cost is real and counted as spend, but it is
    flagged so the request can be surfaced as "assumed" in stats / dashboard and
    the user prompted to pin the rate. Returns False for unknown models, genuine
    source-eligible matches, `_DEFAULT_PRICING` seeds, and `:free`/`_subscription`
    entries.
    """
    resolved = _resolve_pricing(model, provider)
    return bool(resolved and resolved.get("_provider_assumed"))


def get_known_free_models() -> list:
    """Return all model names that are explicitly priced at input=0 AND output=0.

    Used at plugin load to backfill known_free_models for pre-v5 installs that
    had subscription or zero-price models but no persistent free-model rows yet.
    Only models that would actually produce cost==0 via estimate_cost are included —
    this mirrors the condition in post_api_request that calls record_free_model.
    Subscription models with nonzero prices are excluded (they cost money and
    should never appear in known_free_models).
    """
    result: list[str] = []
    seen: set[str] = set()
    custom = _load_custom_pricing()
    custom_models = custom.get("models", {})

    for model, prices in custom_models.items():
        if (
            model not in seen
            and float(prices.get("input", -1)) == 0.0
            and float(prices.get("output", -1)) == 0.0
        ):
            result.append(model)
            seen.add(model)

    for model, prices in _DEFAULT_PRICING.items():
        if (
            model not in seen
            and float(prices.get("input", -1)) == 0.0
            and float(prices.get("output", -1)) == 0.0
        ):
            result.append(model)
            seen.add(model)

    return result


def reload_custom_pricing() -> None:
    """Force-reload the user pricing file (useful in tests)."""
    global _custom_pricing
    _custom_pricing = None
