"""hermes telemetry pricing backfill — seed a current pricing snapshot for every
historical (provider, model) in llm_calls that has none yet.

Coverage seed, NOT historical reconstruction: each written row carries the tariff
the core resolves TODAY (captured_at = now), using the same dated->canonical
fallback as the live capture. Dry-run by default; fail-open; idempotent (skips
already-covered keys, re-tries unresolvable ones on every run).
"""

from __future__ import annotations

import json

from . import core_pricing, db


def run(apply: bool) -> dict:
    """Enumerate uncovered models, resolve each, and (if apply) write snapshots.

    Returns a result dict: applied, distinct_total, already_covered, to_process,
    resolvable (list of {provider, model, resolved_model}), unresolvable (list of
    {provider, model}), written, via_fallback.
    """
    distinct_total = db.count_distinct_llm_models()
    models = db.models_needing_pricing_snapshot()
    resolvable: list[dict] = []
    unresolvable: list[dict] = []
    written = 0
    for provider, model in models:
        # base_url intentionally omitted: llm_calls does not store the per-call
        # endpoint. A model that only resolves via a base_url /models fetch already
        # has a live snapshot (captured with its base_url) and is excluded by
        # models_needing_pricing_snapshot(), so nothing extra is recoverable here.
        snap, resolved_model = core_pricing.resolve_with_fallback(model, provider)
        if snap:
            resolvable.append(
                {"provider": provider, "model": model, "resolved_model": resolved_model}
            )
            if apply and db.record_pricing_snapshot(
                provider, model, snap, resolved_model=resolved_model
            ):
                written += 1
        else:
            unresolvable.append({"provider": provider, "model": model})
    via_fallback = sum(1 for r in resolvable if r["resolved_model"])
    return {
        "applied": apply,
        "distinct_total": distinct_total,
        "already_covered": distinct_total - len(models),
        "to_process": len(models),
        "resolvable": resolvable,
        "unresolvable": unresolvable,
        "written": written,
        "via_fallback": via_fallback,
    }


def render(result: dict) -> str:
    """Human-readable report for a run() result."""
    if result["applied"]:
        return (
            "Pricing snapshot backfill\n"
            f"  Wrote {result['written']} snapshot rows "
            f"({result['via_fallback']} via dated fallback), "
            f"{len(result['unresolvable'])} unresolvable, "
            f"{result['already_covered']} already covered."
        )
    lines = [
        "Pricing snapshot backfill (dry-run)",
        f"  Distinct (provider, model) in llm_calls: {result['distinct_total']}",
        f"  Already have a snapshot:                 {result['already_covered']}   (skipped)",
        f"  To process:                              {result['to_process']}",
        f"    Resolvable (would write):              {len(result['resolvable'])}"
        f"   (via dated fallback: {result['via_fallback']})",
        f"    Unresolvable (no core price):          {len(result['unresolvable'])}",
    ]
    for u in result["unresolvable"]:
        lines.append(f"      - {u['model']}  ({u['provider']})")
    lines.append(f"  Run with --apply to write {len(result['resolvable'])} snapshot rows.")
    return "\n".join(lines)


def to_json(result: dict) -> str:
    return json.dumps(result, default=str)
