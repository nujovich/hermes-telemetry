"""hermes telemetry pricing recalculate — recompute historical llm_calls.cost_usd
against the current pricing.yaml.

Offline and additive: it rewrites ``cost_usd`` for rows that were explicitly
priced at record time (``estimated = 0 AND provider_assumed = 0``), so a
``pricing drift --apply`` that corrected a rate is reflected in historical spend
instead of leaving old-price costs frozen in the DB (issue #83). Dry-run by
default; rows whose model no longer resolves to an explicit price are skipped,
never zeroed.
"""

from __future__ import annotations

import json

from . import db, pricing

# Float epsilon: ignore cost differences below this so binary float noise at the
# recompute boundary (old stored value vs freshly computed value for an
# unchanged rate) does not count as a change worth rewriting.
_EPS = 1e-9


def run(apply: bool = False) -> dict:
    """Recompute cost for explicitly-priced rows under current pricing.

    Returns a result dict: applied, candidates, changed, unchanged,
    skipped_unpriced, old_total, new_total, delta. ``old_total``/``new_total``
    sum only the CHANGED rows (their before and after cost), so ``delta`` is the
    net effect of the recalc on historical spend.
    """
    rows = db.list_recalculable_llm_calls()

    changed: list[dict] = []
    skipped_unpriced: list[dict] = []
    old_total = 0.0
    new_total = 0.0

    for row in rows:
        model = row["model"] or ""
        provider = row["provider"] or ""
        old_cost = float(row["cost_usd"] or 0.0)

        # Guard against zeroing a row whose model lost its price entirely since
        # it was recorded (e.g. the pricing.yaml entry was removed, not just
        # corrected). estimate_cost() returns 0.0 for an unknown model, which
        # would silently under-count spend — exactly what the recalc exists to
        # fix. Skip these and surface them instead.
        if not pricing.is_explicitly_priced(model, provider):
            skipped_unpriced.append({"model": row["model"], "provider": row["provider"]})
            continue

        usage = {
            "input_tokens": row["tokens_in"],
            "output_tokens": row["tokens_out"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_write_tokens": row["cache_write_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
        }
        new_cost = pricing.estimate_cost(usage, model, provider)
        if abs(new_cost - old_cost) > _EPS:
            changed.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "model": row["model"],
                    "provider": row["provider"],
                    "old_cost": old_cost,
                    "new_cost": new_cost,
                }
            )
            old_total += old_cost
            new_total += new_cost

    if apply:
        sessions: set[str] = set()
        for c in changed:
            db.set_llm_call_cost(c["id"], c["new_cost"])
            sessions.add(c["session_id"])
        for sid in sessions:
            db.refresh_run_cost(sid)

    return {
        "applied": apply,
        "candidates": len(rows),
        "changed": len(changed),
        "unchanged": len(rows) - len(changed) - len(skipped_unpriced),
        "skipped_unpriced": len(skipped_unpriced),
        "old_total": old_total,
        "new_total": new_total,
        "delta": new_total - old_total,
    }


def render(result: dict) -> str:
    """Human-readable report for a run() result."""
    if result["applied"]:
        return (
            "Pricing recalculate (--apply)\n"
            f"  Updated {result['changed']} row(s), "
            f"delta ${result['delta']:+.6f} "
            f"(${result['old_total']:.6f} -> ${result['new_total']:.6f})."
        )
    lines = [
        "Pricing recalculate (dry-run)",
        f"  Explicitly-priced rows: {result['candidates']}",
        f"  Would change:           {result['changed']}",
        f"  Unchanged:              {result['unchanged']}",
        f"  Skipped (no price now): {result['skipped_unpriced']}",
    ]
    if result["changed"]:
        lines.append(
            f"  Delta: ${result['delta']:+.6f} "
            f"(${result['old_total']:.6f} -> ${result['new_total']:.6f})"
        )
        lines.append("  Run with --apply to rewrite historical costs.")
    return "\n".join(lines)


def to_json(result: dict) -> str:
    return json.dumps(result, default=str)
