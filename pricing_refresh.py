#!/usr/bin/env python3
"""Pricing auto-refresh from remote sources.

Fetches model pricing from provider APIs and merges into ~/.hermes/telemetry/pricing.yaml.
Designed for extensibility: add new sources by subclassing PricingSource.

Usage:
    python -m hermes_telemetry.pricing_refresh          # refresh all sources
    python -m hermes_telemetry.pricing_refresh --check  # show what would change, don't write
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PRICING_FILE = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "telemetry" / "pricing.yaml"


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
        import urllib.request
        import urllib.error
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
    We convert to per-1M-tokens to match the existing pricing.yaml format.
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

            # OpenRouter returns prices per token as strings like "0.00000250"
            inp = self._parse_price(pricing.get("prompt", "0"))
            out = self._parse_price(pricing.get("completion", "0"))
            if inp == 0 and out == 0:
                continue

            # Convert per-token to per-1M-tokens
            inp = round(inp * 1_000_000, 4)
            out = round(out * 1_000_000, 4)

            # Negative prices are OpenRouter placeholders for models without
            # fixed pricing (e.g. auto-routing, experimental). Mark them as
            # estimated so they don't pollute cost calculations or budgets.
            estimated_price = inp < 0 or out < 0
            if estimated_price:
                inp = 0.0
                out = 0.0

            entry = {"input": inp, "output": out}
            if estimated_price:
                entry["_estimated_price"] = True

            result[model_id] = entry

        logger.info("OpenRouterSource: fetched %d models", len(result))
        return result

    @staticmethod
    def _parse_price(val: Any) -> float:
        """Parse a price string like '0.00000250' to float."""
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0


# ---------------------------------------------------------------------------
# Source registry -- add new sources here
# ---------------------------------------------------------------------------
_SOURCES: list[type[PricingSource]] = [
    OpenRouterSource,
    # Future: AnthropicSource, OpenAISource, etc.
]


def register_source(cls: type[PricingSource]) -> None:
    """Register a new pricing source. Call from plugins or config."""
    _SOURCES.append(cls)


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {"models": {}, "defaults": {"cache_read_multiplier": 0.10, "cache_write_multiplier": 1.25}}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {"models": {}, "defaults": {}}
    except Exception as exc:
        logger.error("Failed to load %s: %s", path, exc)
        return {"models": {}, "defaults": {}}


def _save_yaml(path: Path, data: dict) -> None:
    try:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception as exc:
        logger.error("Failed to save %s: %s", path, exc)


def refresh_pricing(dry_run: bool = False) -> tuple[dict[str, dict], list[dict]]:
    """Fetch all pricing sources and merge into pricing.yaml.

    Merge strategy:
    - Existing manual entries are preserved (user overrides take priority)
    - New models from remote sources are added
    - Existing auto-refreshed models are updated
    - Models are tagged with source and last_updated for traceability

    Returns:
        (changes, manual_overrides) where
        changes: {model: {old, new, source}}
        manual_overrides: [{model, local_input, remote_input, local_output, remote_output}]
    """
    existing = _load_yaml(PRICING_FILE)
    existing_models = existing.get("models", {})
    defaults = existing.get("defaults", {"cache_read_multiplier": 0.10, "cache_write_multiplier": 1.25})

    # Track which models came from auto-refresh vs manual
    # We use a special _meta key to track auto-refreshed models
    meta = existing.get("_meta", {})
    auto_models = set(meta.get("auto_models", []))

    changes: dict[str, dict] = {}
    all_fetched: dict[str, dict] = {}

    # Fetch all sources
    for source_cls in _SOURCES:
        source = source_cls()
        try:
            fetched = source.fetch()
            for model, pricing in fetched.items():
                if model not in all_fetched:
                    all_fetched[model] = {**pricing, "_source": source.name}
        except Exception as exc:
            logger.error("Source %s failed: %s", source.name, exc)

    # Merge: only update models that were auto-refreshed or are new
    new_auto_models = set()
    manual_overrides: list[dict] = []
    for model, pricing in all_fetched.items():
        source = pricing.pop("_source", "unknown")
        new_auto_models.add(model)

        if model in existing_models:
            # A model that exists but was never auto-fetched is manual.
            # Never overwrite manual entries — user overrides take priority.
            is_manual = model not in auto_models
            if is_manual:
                fetched_input = pricing.get("input", 0)
                existing_input = existing_models[model].get("input", 0)
                if fetched_input != existing_input:
                    logger.info(
                        "Manual override preserved for %s "
                        "(remote=%s, local=%s) — update pricing.yaml manually if needed",
                        model, fetched_input, existing_input,
                    )
                    manual_overrides.append({
                        "model": model,
                        "local_input": existing_input,
                        "remote_input": fetched_input,
                        "local_output": existing_models[model].get("output", 0),
                        "remote_output": pricing.get("output", 0),
                    })
                new_auto_models.discard(model)
                continue

            # It was auto-refreshed before — update if values changed
            old = {k: v for k, v in existing_models[model].items() if not k.startswith("_")}
            if old != pricing:
                changes[model] = {"old": old, "new": pricing, "source": source}
            existing_models[model] = {**pricing, "_auto": True, "_source": source}
        else:
            # New model -- add it
            changes[model] = {"old": None, "new": pricing, "source": source}
            existing_models[model] = {**pricing, "_auto": True, "_source": source}

    # Update meta
    meta["auto_models"] = sorted(new_auto_models)
    meta["estimated_price_models"] = sorted(
        m for m, e in existing_models.items() if e.get("_estimated_price")
    )
    meta["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["sources"] = [s.name for s in _SOURCES]

    existing["models"] = existing_models
    existing["defaults"] = defaults
    existing["_meta"] = meta

    if not dry_run and changes:
        _save_yaml(PRICING_FILE, existing)
        logger.info("pricing.yaml updated: %d changes", len(changes))
    elif dry_run:
        logger.info("dry-run: %d changes would be made", len(changes))
    else:
        logger.info("no changes detected")

    return changes, manual_overrides 


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

    changes, manual_overrides = refresh_pricing(dry_run=args.check)

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

    if manual_overrides:
        print(f"\n⚠  Manual overrides preserved ({len(manual_overrides)} model(s) with differing remote prices):\n")
        for mo in sorted(manual_overrides, key=lambda x: x["model"]):
            print(f"    {mo['model']}")
            if mo["local_input"] != mo["remote_input"]:
                print(f"      input:  local={mo['local_input']:.4f}  remote={mo['remote_input']:.4f}")
            if mo["local_output"] != mo["remote_output"]:
                print(f"      output: local={mo['local_output']:.4f}  remote={mo['remote_output']:.4f}")

    if not changes and not manual_overrides:
        print("No changes. Pricing is up to date.")


if __name__ == "__main__":
    main()
