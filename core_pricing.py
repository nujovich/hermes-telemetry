"""Bridge to Hermes core pricing (``agent.usage_pricing``).

This is the ONLY module in the plugin that imports the Hermes core. Everywhere
else the plugin receives what it needs through hook kwargs; this seam lets us
capture the authoritative tariffs (with provenance) the core resolves for a
model without coupling the rest of the plugin — or the isolated test suite — to
``agent.*``.

``resolve()`` is lazy (the import happens inside the call) and fail-open: it
returns ``None`` on any failure (core absent, unknown model, endpoint fetch
error) and never raises, so a telemetry capture can never break an agent turn.
Verified against NousResearch/hermes-agent ``agent/usage_pricing.py`` (2026-07-11):
``get_pricing_entry(model_name, provider=None, base_url=None, api_key=None)``
returns a ``PricingEntry`` whose cost fields are ``Decimal | None`` per million
tokens, ``source`` is a ``CostSource`` enum, and ``fetched_at`` is a ``datetime``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("hermes_telemetry")

# PricingEntry numeric fields copied name-for-name, converted to float | None.
_RATE_FIELDS = (
    "input_cost_per_million",
    "output_cost_per_million",
    "cache_read_cost_per_million",
    "cache_write_cost_per_million",
    "request_cost",
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve(model: str, provider: str = "", base_url: str = "") -> dict | None:
    """Return a JSON-safe pricing snapshot for (model, provider, base_url), or None.

    Keys: the five ``*_per_million`` / ``request_cost`` floats, plus ``source``
    (str), ``source_url`` (str), ``pricing_version`` (str), ``fetched_at`` (ISO
    str). Any field the core leaves unset is ``None``. Returns ``None`` if the
    core cannot price the model or is not importable. Never raises.
    """
    if not model:
        return None
    try:
        from agent.usage_pricing import get_pricing_entry

        entry = get_pricing_entry(
            model,
            provider=provider or None,
            base_url=base_url or None,
            api_key="",
        )
        if entry is None:
            return None

        snap: dict = {field: _to_float(getattr(entry, field, None)) for field in _RATE_FIELDS}

        source = getattr(entry, "source", None)
        if source is None:
            snap["source"] = None
        elif hasattr(source, "value"):
            snap["source"] = str(source.value)
        else:
            snap["source"] = str(source)

        snap["source_url"] = getattr(entry, "source_url", None)
        snap["pricing_version"] = getattr(entry, "pricing_version", None)
        fetched_at = getattr(entry, "fetched_at", None)
        snap["fetched_at"] = fetched_at.isoformat() if fetched_at is not None else None
        return snap
    except Exception as exc:  # fail-open — telemetry must never break a turn
        logger.debug(
            "core_pricing.resolve failed for model=%r provider=%r: %s", model, provider, exc
        )
        return None
