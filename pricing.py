"""Pricing table for cost estimation.

Prices are USD per 1 million tokens, loaded from:
  1. ~/.hermes/telemetry/pricing.yaml  (user override, optional)
  2. _DEFAULT_PRICING (embedded fallback)

Unknown models: cost = 0.0, warning logged once per model name.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# USD per 1M tokens: (input_per_million, output_per_million)
# Sources: official provider pricing pages, May 2026
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # ── Anthropic / Nous Portal ──────────────────────────────────────────────
    "claude-opus-4-8":          (5.00,  25.00),
    "claude-opus-4-7":          (5.00,  25.00),
    "claude-sonnet-4-6":        (3.00,  15.00),
    "claude-sonnet-4-5":        (3.00,  15.00),
    "claude-haiku-4-5":         (0.80,   4.00),
    "claude-opus-4":            (15.00, 75.00),
    "claude-sonnet-4":          (3.00,  15.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022":  (0.80,  4.00),
    "claude-3-opus-20240229":   (15.00, 75.00),
    "claude-3-haiku-20240307":  (0.25,  1.25),
    # ── OpenAI ──────────────────────────────────────────────────────────────
    "gpt-4o":                   (2.50,  10.00),
    "gpt-4o-mini":              (0.15,   0.60),
    "gpt-4-turbo":              (10.00, 30.00),
    "gpt-4":                    (30.00, 60.00),
    "gpt-3.5-turbo":            (0.50,  1.50),
    "o1":                       (15.00, 60.00),
    "o1-mini":                  (3.00,  12.00),
    "o3":                       (10.00, 40.00),
    "o3-mini":                  (1.10,   4.40),
    "o4-mini":                  (1.10,   4.40),
    # ── DeepSeek ────────────────────────────────────────────────────────────
    "deepseek-chat":            (0.27,  1.10),
    "deepseek-v3":              (0.27,  1.10),
    "deepseek-r1":              (0.55,  2.19),
    # ── Nous Research (Portal) ───────────────────────────────────────────────
    "hermes-3-llama-3.1-405b":  (3.00,  15.00),
    "hermes-3-llama-3.1-70b":   (0.70,   0.90),
    # ── Meta (via OpenRouter / providers) ───────────────────────────────────
    "meta-llama/llama-3.1-405b-instruct": (2.70, 2.70),
    "meta-llama/llama-3.1-70b-instruct":  (0.52, 0.75),
    "meta-llama/llama-3.3-70b-instruct":  (0.59, 0.79),
    # ── Google ──────────────────────────────────────────────────────────────
    "gemini-1.5-pro":           (3.50,  10.50),
    "gemini-1.5-flash":         (0.075,  0.30),
    "gemini-2.0-flash":         (0.10,   0.40),
    "gemini-2.5-pro":           (1.25,  10.00),
}

# Prefix-based fallback for model families (matched in order, longest first)
_PREFIX_PRICING: list[tuple[str, tuple[float, float]]] = [
    ("claude-opus",    (5.00,  25.00)),
    ("claude-sonnet",  (3.00,  15.00)),
    ("claude-haiku",   (0.80,   4.00)),
    ("gpt-4o-mini",    (0.15,   0.60)),
    ("gpt-4o",         (2.50,  10.00)),
    ("gpt-4",          (10.00, 30.00)),
    ("gpt-3.5",        (0.50,   1.50)),
    ("o1-mini",        (3.00,  12.00)),
    ("o1",             (15.00, 60.00)),
    ("o3-mini",        (1.10,   4.40)),
    ("o3",             (10.00, 40.00)),
    ("o4-mini",        (1.10,   4.40)),
    ("deepseek-r1",    (0.55,   2.19)),
    ("deepseek",       (0.27,   1.10)),
    ("gemini-2.5",     (1.25,  10.00)),
    ("gemini-2",       (0.10,   0.40)),
    ("gemini-1.5-pro", (3.50,  10.50)),
    ("gemini",         (0.075,  0.30)),
    ("llama-3.1-405",  (2.70,   2.70)),
    ("llama-3.1-70",   (0.52,   0.75)),
    ("llama-3.3-70",   (0.59,   0.79)),
]

_warned_unknown: set[str] = set()
_custom_pricing: Optional[dict[str, tuple[float, float]]] = None


def _load_custom_pricing() -> dict[str, tuple[float, float]]:
    global _custom_pricing
    if _custom_pricing is not None:
        return _custom_pricing
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    pricing_file = hermes_home / "telemetry" / "pricing.yaml"
    if not pricing_file.exists():
        _custom_pricing = {}
        return _custom_pricing
    try:
        import yaml
        with open(pricing_file) as f:
            data = yaml.safe_load(f) or {}
        result: dict[str, tuple[float, float]] = {}
        for model, entry in data.items():
            if isinstance(entry, dict):
                inp = float(entry.get("input", 0.0))
                out = float(entry.get("output", 0.0))
                result[str(model)] = (inp, out)
        _custom_pricing = result
    except Exception as exc:
        logger.warning("Failed to load custom pricing from %s: %s", pricing_file, exc)
        _custom_pricing = {}
    return _custom_pricing


def _lookup(model: str) -> Optional[tuple[float, float]]:
    model_lc = model.lower()
    custom = _load_custom_pricing()
    if model_lc in custom:
        return custom[model_lc]
    if model_lc in _DEFAULT_PRICING:
        return _DEFAULT_PRICING[model_lc]
    # Prefix match
    for prefix, prices in sorted(_PREFIX_PRICING, key=lambda x: -len(x[0])):
        if model_lc.startswith(prefix):
            return prices
    return None


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Return estimated cost in USD. Returns 0.0 for unknown models."""
    if not model or (tokens_in == 0 and tokens_out == 0):
        return 0.0
    prices = _lookup(model)
    if prices is None:
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            logger.warning(
                "hermes-telemetry: unknown model %r — cost recorded as $0.00. "
                "Add to ~/.hermes/telemetry/pricing.yaml to fix.",
                model,
            )
        return 0.0
    inp_per_m, out_per_m = prices
    return (tokens_in * inp_per_m + tokens_out * out_per_m) / 1_000_000


def reload_custom_pricing() -> None:
    """Force-reload the user pricing file (useful in tests)."""
    global _custom_pricing
    _custom_pricing = None
