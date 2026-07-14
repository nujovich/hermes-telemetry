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
tokens, ``source`` is a plain string (``CostSource`` is a ``Literal[...]`` string
type alias — verified in agent/usage_pricing.py:19), and ``fetched_at`` a datetime.
"""

from __future__ import annotations

import logging
import re
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


_DATED_SUFFIX = re.compile(r"-\d{8}(?=:|$)")


def canonical_model_name(model: str) -> str:
    """Strip a trailing dated-version suffix (``-YYYYMMDD``) from a model id.

    Intended ONLY as a FALLBACK, called after ``resolve(raw)`` has already
    returned ``None``. Hermes core's ``/models`` catalog lists canonical
    (undated) names, but the gateway calls with dated ids like
    ``deepseek/deepseek-v4-pro-20260423`` that miss the catalog
    (``get_pricing_entry`` → ``None``). Removing the date recovers the name the
    core knows. The lookahead matches the 8-digit token just before end-of-id OR
    a ``:``-suffix (generic; the gateway uses ``:free`` in practice), so both
    ``deepseek/deepseek-v4-pro-20260423`` → ``deepseek/deepseek-v4-pro`` and
    ``tencent/hy3-20260706:free`` → ``tencent/hy3:free`` work. Returns the input
    unchanged when there is no dated suffix. Pure; never raises for a str model
    id (a non-str would raise ``TypeError``, but callers only pass the hook's
    str model, and the capture block is itself fail-open).

    A wrong strip is harmless: this runs only after a direct-resolve miss, so a
    model id that legitimately ends in 8 digits and is resolvable would have
    resolved directly and never reach here — no regression is possible. If the
    stripped name is unknown to the core, ``resolve()`` fail-opens to ``None`` →
    no capture (same as before). We capture only when the stripped name resolves.
    """
    return _DATED_SUFFIX.sub("", model)


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

        # source is a plain string (CostSource = Literal[...]), verified against
        # agent/usage_pricing.py. str() coerces defensively but is a no-op for the
        # real string value.
        source = getattr(entry, "source", None)
        snap["source"] = str(source) if source is not None else None

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
