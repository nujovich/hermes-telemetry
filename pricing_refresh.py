#!/usr/bin/env python3
"""Pricing auto-refresh from remote sources.

Fetches model pricing from provider APIs and writes ~/.hermes/telemetry/
pricing.auto.yaml. The file is fully replaced on each refresh (no merge into
pricing.yaml), which is what makes the v0.6 split between manual overrides
and auto-fetched entries collision-free by construction.

Usage:
    python -m hermes_telemetry.pricing_refresh          # refresh all sources
    python -m hermes_telemetry.pricing_refresh --check  # show what would change, don't write
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRICING_FILE = (
    Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "telemetry" / "pricing.yaml"
)
AUTO_FILE = (
    Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "telemetry" / "pricing.auto.yaml"
)


# ---------------------------------------------------------------------------
# Pricing source base
# ---------------------------------------------------------------------------
class PricingSource(ABC):
    """A pricing source that can fetch model prices from a remote API.

    Subclass this to add new sources (Anthropic, OpenAI, etc.).
    """

    name: str = "abstract"

    @abstractmethod
    def fetch(self) -> dict[str, dict]:
        """Return pricing dict: {model_name: {input: float, output: float, ...}}"""
        ...

    def _get(self, url: str, timeout: int = 15) -> dict:
        """Simple HTTP GET using stdlib only."""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "hermes-telemetry/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("PricingSource %s: fetch failed: %s", self.name, exc)
            return {}


# ---------------------------------------------------------------------------
# OpenRouter source
# ---------------------------------------------------------------------------
class OpenRouterSource(PricingSource):
    """Fetch pricing from OpenRouter's public models API.

    API: https://openrouter.ai/api/v1/models
    No auth required. Returns pricing in USD per token (not per 1M).
    We convert to per-1M-tokens to match the existing pricing format.
    """

    name = "openrouter"
    API_URL = "https://openrouter.ai/api/v1/models"

    def fetch(self) -> dict[str, dict]:
        data = self._get(self.API_URL)
        if not data:
            return {}

        models = data.get("data", [])
        result = {}
        for m in models:
            model_id = m.get("id", "")
            if not model_id:
                continue
            pricing = m.get("pricing", {})
            if not pricing:
                continue

            inp = self._parse_price(pricing.get("prompt", "0"))
            out = self._parse_price(pricing.get("completion", "0"))
            if inp == 0 and out == 0:
                continue

            inp = round(inp * 1_000_000, 4)
            out = round(out * 1_000_000, 4)

            # Negative prices are OpenRouter placeholders for models without
            # fixed pricing (e.g. auto-routing, experimental). Mark them as
            # estimated so they don't pollute cost calculations or budgets.
            estimated_price = inp < 0 or out < 0
            if estimated_price:
                inp = 0.0
                out = 0.0

            entry: dict = {"input": inp, "output": out}
            if estimated_price:
                entry["_estimated_price"] = True

            result[model_id] = entry

        logger.info("OpenRouterSource: fetched %d models", len(result))
        return result

    @staticmethod
    def _parse_price(val: Any) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0


# ---------------------------------------------------------------------------
# Google AI Studio source
# ---------------------------------------------------------------------------
class GoogleAISource(PricingSource):
    """Google AI Studio direct-provider pricing.

    Constant table mirroring https://ai.google.dev/gemini-api/docs/pricing.
    Refresh manually on each release cycle: update _PRICES, bump LAST_VERIFIED,
    run tests, ship.

    Keys are bare model IDs (no `google/` prefix). Tiered-pricing models use
    the <=200k context tier only.
    """

    name = "google-ai"
    LAST_VERIFIED = "2026-06-05"

    _PRICES: dict[str, dict] = {
        "gemini-3.5-flash": {"input": 1.50, "output": 9.00, "cache_read": 0.15},
        "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00, "cache_read": 0.20},
        "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cache_read": 0.025},
        "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "cache_read": 0.05},
        "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cache_read": 0.125},
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
        "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_read": 0.01},
    }

    def fetch(self) -> dict[str, dict]:
        result = {model: dict(pricing) for model, pricing in self._PRICES.items()}
        logger.info(
            "GoogleAISource: returned %d models (last verified %s)",
            len(result),
            self.LAST_VERIFIED,
        )
        return result


# ---------------------------------------------------------------------------
# Source registry -- add new sources here
# ---------------------------------------------------------------------------
_SOURCES: list[type[PricingSource]] = [
    OpenRouterSource,
    GoogleAISource,
]


def register_source(cls: type[PricingSource]) -> None:
    """Register a new pricing source. Call from plugins or config."""
    _SOURCES.append(cls)


# ---------------------------------------------------------------------------
# Refresh — full replacement of pricing.auto.yaml
# ---------------------------------------------------------------------------
def _load_auto(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return {}


def _save_auto(path: Path, data: dict) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def refresh_pricing(dry_run: bool = False) -> tuple[dict[str, dict], list[dict]]:
    """Fetch all pricing sources and rewrite ``pricing.auto.yaml``.

    The auto file is fully replaced — the v0.6 split moves manual overrides to
    pricing.yaml, so the refresher no longer needs to preserve user edits or
    track auto_models. Anything not returned by a source this run is gone from
    the file (which is what you want for prices that vanished upstream).

    Returns:
        (changes, manual_overrides) where:
          - changes: {model: {"old": prev_dict|None, "new": curr_dict, "source": src_name}}
          - manual_overrides: always [] in v0.6 (kept for backward-compatible
            unpacking; collisions are structurally impossible now).
    """
    prev = _load_auto(AUTO_FILE)
    prev_sources = prev.get("sources") or {}
    prev_flat: dict[str, tuple[str, dict]] = {}
    if isinstance(prev_sources, dict):
        for src, models in prev_sources.items():
            if not isinstance(models, dict):
                continue
            for m, entry in models.items():
                prev_flat[m] = (src, {k: v for k, v in entry.items() if not k.startswith("_")})

    new_sources: dict[str, dict[str, dict]] = {}
    estimated_models: list[str] = []
    source_names: list[str] = []

    for source_cls in _SOURCES:
        source = source_cls()
        source_names.append(source.name)
        try:
            fetched = source.fetch()
        except Exception as exc:
            logger.error("Source %s failed: %s", source.name, exc)
            continue
        bucket = new_sources.setdefault(source.name, {})
        for model, entry in fetched.items():
            # First source to claim a model id wins (matches pre-v0.6 behaviour
            # where OpenRouter populated first and Google-AI's bare gemini-* ids
            # never collided with openrouter's google/-prefixed ones).
            if any(model in s for s in new_sources.values() if s is not bucket):
                continue
            clean = dict(entry)
            estimated = bool(clean.pop("_estimated_price", False))
            bucket[model] = clean
            if estimated:
                estimated_models.append(model)
                # Round-trip the flag on disk so the loader picks it up.
                bucket[model]["_estimated_price"] = True

    # Compute diff vs previous auto file for the CLI report.
    changes: dict[str, dict] = {}
    new_flat: dict[str, tuple[str, dict]] = {}
    for src, models in new_sources.items():
        for m, entry in models.items():
            new_flat[m] = (src, {k: v for k, v in entry.items() if not k.startswith("_")})
    for m, (src, prices) in new_flat.items():
        prev_prices = prev_flat.get(m, (None, None))[1]
        if prev_prices is None:
            changes[m] = {"old": None, "new": prices, "source": src}
        elif prev_prices != prices:
            changes[m] = {"old": prev_prices, "new": prices, "source": src}

    out: dict = {"sources": new_sources}
    if estimated_models:
        out["estimated_price_models"] = sorted(set(estimated_models))
    out["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["sources_list"] = source_names

    if not dry_run:
        _save_auto(AUTO_FILE, out)
        logger.info("pricing.auto.yaml written: %d models, %d changes", len(new_flat), len(changes))
    else:
        logger.info("dry-run: %d changes would be made", len(changes))

    return changes, []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Refresh pricing from remote sources")
    parser.add_argument("--check", action="store_true", help="dry run, don't write")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    changes, _ = refresh_pricing(dry_run=args.check)

    if changes:
        label = "Would update" if args.check else "Updated"
        print(f"\n{label} {len(changes)} model(s):\n")
        for model, diff in sorted(changes.items()):
            src = diff["source"]
            if diff["old"] is None:
                print(f"  + {model}  ({src})")
                print(f"      input={diff['new']['input']:.4f} output={diff['new']['output']:.4f}")
            else:
                print(f"  ~ {model}  ({src})")
                for key in ("input", "output"):
                    old_v = diff["old"].get(key, 0)
                    new_v = diff["new"].get(key, 0)
                    if old_v != new_v:
                        print(f"      {key}: {old_v:.4f} → {new_v:.4f}")
    else:
        print("No changes. Pricing is up to date.")


if __name__ == "__main__":
    main()
