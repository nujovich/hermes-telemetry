"""Pricing table for cost estimation.

Prices are USD per 1 million tokens, loaded from:
  1. ~/.hermes/telemetry/pricing.yaml      — manual overrides (user-edited)
  2. ~/.hermes/telemetry/pricing.auto.yaml — auto-fetched from sources
  3. _DEFAULT_PRICING                       — embedded fallback

Manual override schema (v0.6+):
  overrides:
    nous:                                  # provider name (lowercase, matches
                                           # the `provider` kwarg from the hook)
      "deepseek/deepseek-v4-pro":
        input: 1.60
        output: 3.20
        cache_read: 0.14
    "*":                                   # provider-neutral bucket: applies to
                                           # any caller without a more specific
                                           # provider-namespaced entry.
      "some-model":
        input: 1.0
        output: 2.0
        _subscription: true                # optional: declares a flat-sub $0
  defaults:
    cache_read_multiplier: 0.10
    cache_write_multiplier: 1.25

Auto-fetched schema (machine-written; never hand-edit):
  sources:
    openrouter:
      "deepseek/deepseek-v4-pro": {input: 0.435, output: 0.87}
    google-ai:
      "gemini-2.5-pro": {input: 1.25, output: 10.0, cache_read: 0.125}
  estimated_price_models: [openrouter/auto, ...]   # `_estimated_price: true`
  last_refresh: "..."
  sources_list: [openrouter, google-ai]

Lookup precedence (most specific first):
  1. overrides.<provider>.<model>  — exact match, manual provider-namespaced
  2. sources.<source>.<model>      — exact, source-eligible for the provider
  3. overrides."*".<model>         — exact, neutral manual override
  4. _DEFAULT_PRICING[<model>]     — exact, built-in
  5. `:free` suffix → $0           — explicit zero, before any prefix scan
  6. Prefix match on the same candidate order (longest-first)
  7. Unknown → $0 + one-time warning per (model, provider) pair

Legacy schema (pre-v0.6 flat `models:` map with `_source`/`_auto`) is read
transparently by the shim until the next plugin load migrates it.

Unknown models: cost = 0.0, warning logged once per (model, provider) pair.
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
    # ── NVIDIA NIM (build.nvidia.com direct) ─────────────────────────────────
    # Hermes canonicalizes the NIM provider to "nvidia" (aliases nim/nvidia-nim/
    # nemotron normalize to it). Seeds live here, source-neutral and in code, so
    # they (1) survive an OpenRouter sync untouched and (2) are selected when the
    # provider-aware guard excludes a same-id OpenRouter entry for a NIM call.
    "nvidia/nemotron-3-super-120b-a12b": dict(input=0.10, output=0.50),
    "nvidia/nemotron-super-49b": dict(input=0.10, output=0.40),
    "nvidia/nemotron-70b-instruct": dict(input=1.20, output=1.20),
    "nvidia/nemotron-nano-12b-vl": dict(input=0.20, output=0.60),
    "nvidia/nemotron-nano-9b": dict(input=0.04, output=0.16),
    "nvidia/nemotron-3-ultra": dict(input=0.50, output=2.50),
    # ── Meta (via OpenRouter / providers) ───────────────────────────────────
    "meta-llama/llama-3.1-405b-instruct": dict(input=2.70, output=2.70),
    "meta-llama/llama-3.1-70b-instruct": dict(input=0.52, output=0.75),
    "meta-llama/llama-3.3-70b-instruct": dict(input=0.59, output=0.79),
    # ── Google ──────────────────────────────────────────────────────────────
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

# Single composite cache. Reset to None to force re-read of BOTH files. Tests
# reset this directly via `pricing._custom_pricing = None`, so the name stays
# even though the structure inside changed in v0.6.
#
# Shape when populated:
#   {
#     "overrides": {provider_lc: {model_lc: prices}},   # "*" is the neutral bucket
#     "sources":   {source_lc:   {model_lc: prices}},
#     "defaults":  {multiplier name: float},
#     "subscription_models": set[str],
#     "estimated_price_models": set[str],
#     "legacy": bool,        # True when reading a pre-v0.6 pricing.yaml shape
#   }
_custom_pricing: dict | None = None


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _pricing_paths() -> tuple[Path, Path]:
    tele = _hermes_home() / "telemetry"
    return tele / "pricing.yaml", tele / "pricing.auto.yaml"


def _empty_cache() -> dict:
    return {
        "overrides": {},
        "sources": {},
        "defaults": {},
        "subscription_models": set(),
        "estimated_price_models": set(),
        "legacy": False,
    }


def _is_legacy_shape(data: dict) -> bool:
    """Detect a pre-v0.6 pricing.yaml.

    Legacy is the flat ``models:`` map (or top-level model entries when even
    ``models:`` is implicit). The v0.6 schema has ``overrides:`` (and never
    ``models:``), so the presence of ``overrides:`` or ``sources:`` at the
    top level means "new schema".
    """
    if not isinstance(data, dict):
        return False
    if "overrides" in data or "sources" in data:
        return False
    # `models:` is the unambiguous legacy marker. A flat map of model entries
    # (no top-level keys at all) also counts.
    return "models" in data or any(isinstance(v, dict) for v in data.values())


def _load_yaml(path: Path) -> dict:
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return {}


def _load_manual(path: Path, cache: dict) -> None:
    """Populate overrides/defaults/subscription_models from pricing.yaml."""
    if not path.exists():
        return
    data = _load_yaml(path)
    if not data:
        return

    if _is_legacy_shape(data):
        cache["legacy"] = True
        _load_legacy_manual(data, cache)
        return

    raw_overrides = data.get("overrides") or {}
    raw_defaults = data.get("defaults") or {}
    for k, v in raw_defaults.items():
        with contextlib.suppress(TypeError, ValueError):
            cache["defaults"][str(k)] = float(v)

    if not isinstance(raw_overrides, dict):
        return
    for provider, models in raw_overrides.items():
        if not isinstance(models, dict):
            continue
        prov_lc = str(provider).lower()
        bucket = cache["overrides"].setdefault(prov_lc, {})
        for model, entry in models.items():
            if not isinstance(entry, dict):
                continue
            key = str(model).lower()
            bucket[key] = {
                k: float(v)
                for k, v in entry.items()
                if v is not None and not k.startswith("_") and not isinstance(v, bool)
            }
            if entry.get("_subscription"):
                cache["subscription_models"].add(key)
            if entry.get("_estimated_price"):
                cache["estimated_price_models"].add(key)


def _load_legacy_manual(data: dict, cache: dict) -> None:
    """Read a pre-v0.6 pricing.yaml flat-map shape.

    Entries with a `_source` tag are routed into ``sources[<source>]`` so the
    provider-aware guard still applies. Untagged entries land in
    ``overrides["*"]`` as provider-neutral. `_meta.estimated_price_models` /
    `_meta.subscription_models` are read out of the same file.
    """
    raw_models = data.get("models") if "models" in data else data
    raw_defaults = data.get("defaults") or {} if isinstance(data, dict) else {}
    meta = data.get("_meta") or {} if isinstance(data, dict) else {}

    for k, v in raw_defaults.items():
        with contextlib.suppress(TypeError, ValueError):
            cache["defaults"][str(k)] = float(v)

    if not isinstance(raw_models, dict):
        return
    neutral = cache["overrides"].setdefault("*", {})
    for model, entry in raw_models.items():
        if not isinstance(entry, dict):
            continue
        key = str(model).lower()
        prices = {
            k: float(v)
            for k, v in entry.items()
            if v is not None and not k.startswith("_") and not isinstance(v, bool)
        }
        src = entry.get("_source")
        if src:
            cache["sources"].setdefault(str(src).lower(), {})[key] = prices
        else:
            neutral[key] = prices
        if entry.get("_subscription"):
            cache["subscription_models"].add(key)
        if entry.get("_estimated_price"):
            cache["estimated_price_models"].add(key)

    for m in meta.get("estimated_price_models") or []:
        cache["estimated_price_models"].add(str(m).lower())
    for m in meta.get("subscription_models") or []:
        cache["subscription_models"].add(str(m).lower())


def _load_auto(path: Path, cache: dict) -> None:
    """Populate sources/estimated_price_models from pricing.auto.yaml."""
    if not path.exists():
        return
    data = _load_yaml(path)
    if not data:
        return

    raw_sources = data.get("sources") or {}
    if isinstance(raw_sources, dict):
        for source, models in raw_sources.items():
            if not isinstance(models, dict):
                continue
            src_lc = str(source).lower()
            bucket = cache["sources"].setdefault(src_lc, {})
            for model, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                key = str(model).lower()
                bucket[key] = {
                    k: float(v)
                    for k, v in entry.items()
                    if v is not None and not k.startswith("_") and not isinstance(v, bool)
                }
                if entry.get("_estimated_price"):
                    cache["estimated_price_models"].add(key)

    for m in data.get("estimated_price_models") or []:
        cache["estimated_price_models"].add(str(m).lower())


def _load_custom_pricing() -> dict:
    """Load and cache the combined manual + auto pricing data."""
    global _custom_pricing
    if _custom_pricing is not None:
        return _custom_pricing
    cache = _empty_cache()
    try:
        manual_path, auto_path = _pricing_paths()
        _load_manual(manual_path, cache)
        _load_auto(auto_path, cache)
    except Exception as exc:
        logger.warning("Failed to load pricing: %s", exc)
        cache = _empty_cache()
    _custom_pricing = cache
    return _custom_pricing


def _google_alt_form(model_lc: str) -> str | None:
    """Return the alternate Google-AI form for symmetric lookup, or None.

    Maps the pair `gemini-X` ↔ `google/gemini-X` so a single canonical price
    table answers both direct-Google and OpenRouter-routed lookups. This is
    deliberately google-specific.
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

    - Source-less entries (`_DEFAULT_PRICING`, the prefix table, manual
      overrides) are provider-neutral → always eligible.
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


def _candidate_chain(cache: dict, provider: str) -> list[tuple[dict[str, dict], str | None]]:
    """Return the ordered (table, source) pairs to scan for a lookup.

    Order matches the precedence in the module docstring:
      1. overrides[<provider>]              (source=None, provider-neutral guard ok)
      2. sources[<src>] for src in cache    (source=<src>, guard applies)
      3. overrides["*"]
      4. _DEFAULT_PRICING                   (source=None)

    The caller decides exact-vs-prefix on top of this.
    """
    chain: list[tuple[dict[str, dict], str | None]] = []
    prov_lc = (provider or "").lower()
    overrides = cache.get("overrides", {})
    if prov_lc and prov_lc in overrides:
        chain.append((overrides[prov_lc], None))
    # Auto sources, gated by _source_eligible
    for src, table in cache.get("sources", {}).items():
        chain.append((table, src))
    # Neutral overrides bucket
    if "*" in overrides and (not prov_lc or prov_lc != "*"):
        chain.append((overrides["*"], None))
    # Built-in defaults (always last among "exact" candidates)
    chain.append((_DEFAULT_PRICING, None))
    return chain


def _lookup_form(model_lc: str, provider: str = "") -> dict | None:
    """Exact-then-prefix lookup against the v0.6 candidate chain."""
    cache = _load_custom_pricing()
    chain = _candidate_chain(cache, provider)

    # Exact match across the chain, in precedence order.
    for table, source in chain:
        if not _source_eligible(source, provider):
            continue
        if model_lc in table:
            return table[model_lc]

    # Free-tier suffix: short-circuit BEFORE the prefix scan, so a `…:free`
    # variant of a seeded paid base does not inherit the paid price.
    if model_lc.endswith(":free"):
        return {"input": 0.0, "output": 0.0}

    # Prefix fallback: scan the same chain longest-first, plus the curated
    # family-prefix table at the end. Source-ineligible entries are skipped.
    candidates: list[tuple[str, dict, str | None]] = []
    for table, source in chain:
        candidates.extend((k, v, source) for k, v in table.items())
    candidates.extend((k, v, None) for k, v in _PREFIX_PRICING)
    for prefix, prices, source in sorted(candidates, key=lambda x: -len(x[0])):
        if model_lc.startswith(prefix) and _source_eligible(source, provider):
            return prices
    return None


def _lookup_base(model: str, provider: str = "") -> dict | None:
    """Return the raw pricing dict for a model (no cache derivation yet).

    Two-pass strategy: first try the model id as-is. If that misses, try the
    Google-AI alternate form (`gemini-X` ↔ `google/gemini-X`) so direct-Google
    and OpenRouter-routed callers get identical pricing without requiring
    both entries to coexist in the pricing data.
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
    """Return a fully-resolved pricing dict with all 5 keys."""
    base = _lookup_base(model, provider)
    if base is None:
        return None

    cache = _load_custom_pricing()
    defaults = cache.get("defaults", {})
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

    if "cache_write" in base:
        cache_write = float(base["cache_write"])
    else:
        cache_write = input_price * cache_write_mult

    reasoning = float(base.get("reasoning", output_price))

    return dict(
        input=input_price,
        output=output_price,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
    )


def estimate_cost(usage: dict, model: str, provider: str = "") -> float:
    """Return estimated cost in USD for a usage dict.

    usage dict keys (all optional/nullable):
      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens

    `provider` (verbatim from the gateway, e.g. "nous", "nvidia", "openrouter")
    makes the lookup provider-aware (issue #24).

    Returns 0.0 for unknown models (with a one-time warning per (model, provider)
    pair) or empty usage.
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


def get_known_free_models() -> list[str]:
    """Return all model names that are explicitly priced at input=0 AND output=0.

    Used at plugin load to backfill known_free_models. Excludes models tagged
    `_estimated_price: true` — those have input/output forced to 0 only as a
    placeholder for "no fixed price", not as a genuine free tier (fix for a
    latent bug surfaced by the v0.6 refactor).
    """
    result: list[str] = []
    seen: set[str] = set()
    cache = _load_custom_pricing()
    estimated = cache.get("estimated_price_models", set())

    # Manual overrides across all providers, then auto sources, then defaults.
    for table in cache.get("overrides", {}).values():
        for model, prices in table.items():
            if model in seen or model in estimated:
                continue
            if float(prices.get("input", -1)) == 0.0 and float(prices.get("output", -1)) == 0.0:
                result.append(model)
                seen.add(model)
    for table in cache.get("sources", {}).values():
        for model, prices in table.items():
            if model in seen or model in estimated:
                continue
            if float(prices.get("input", -1)) == 0.0 and float(prices.get("output", -1)) == 0.0:
                result.append(model)
                seen.add(model)
    for model, prices in _DEFAULT_PRICING.items():
        if model in seen or model in estimated:
            continue
        if float(prices.get("input", -1)) == 0.0 and float(prices.get("output", -1)) == 0.0:
            result.append(model)
            seen.add(model)
    return result


def get_estimated_price_models() -> list[str]:
    """Return models flagged as estimated-price (no fixed remote price).

    Single source of truth — replaces the three direct yaml.safe_load() reads
    that previously lived in budget.py, db.py, and stats.py.
    """
    cache = _load_custom_pricing()
    return sorted(cache.get("estimated_price_models", set()))


def reload_custom_pricing() -> None:
    """Force-reload pricing on next lookup (useful in tests and after writes)."""
    global _custom_pricing
    _custom_pricing = None
