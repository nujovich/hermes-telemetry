"""hermes telemetry pricing drift — compare pricing.yaml against the core-resolved
pricing_snapshots (ground truth) and report / repair drift.

Offline: reads DB snapshots + pricing.yaml only, no live core call. Input/output
rates only (cache/reasoning are derived, request_cost has no pricing.yaml analog).
Dry-run by default; --apply rewrites pricing.yaml (merge, never clobber), skipping
_subscription entries and tagging repaired models with _source: core-snapshot.
Coverage-gap aware: reminds you to run `pricing backfill` when models in llm_calls
lack any snapshot, so drift never gives a false all-clear.
"""

from __future__ import annotations

import json  # noqa: F401 -- used by later tasks in this plan (CLI --json output)

from . import db, pricing


def _drift_pct(local: float, snap: float) -> float:
    """Signed drift of local vs snapshot as a percentage: (local/snap - 1) * 100.

    When snap is 0: 0.0 if local is also 0, else infinite (a real, unbounded
    over-charge — treated as drift by any finite threshold).
    """
    if snap == 0:
        return 0.0 if local == 0 else float("inf")
    return (local / snap - 1.0) * 100.0


def run(*, apply: bool = False, threshold_pct: float = 1.0, model: str | None = None) -> dict:
    """Diff pricing.yaml against the latest core pricing snapshots.

    Returns a result dict: applied, threshold_pct, model_filter, compared,
    drifted (list), in_sync, skipped_subscription (list), no_local_price (list),
    coverage_gap (int), written (int).
    """
    snapshots = db.list_latest_pricing_snapshots()

    # Collapse each (provider, dated-model) snapshot to its canonical write-key
    # (resolved_model or model). When several dated names collapse to the same
    # canonical pair, keep the one captured latest (highest id).
    canonical: dict[tuple[str, str], dict] = {}
    for row in snapshots:
        write_key = row.get("resolved_model") or row["model"]
        key = (row["provider"], write_key)
        cur = canonical.get(key)
        if cur is None or row["id"] > cur["id"]:
            canonical[key] = row

    custom = pricing._load_custom_pricing()
    subscription = custom.get("subscription_models", set())

    drifted: list[dict] = []
    skipped_subscription: list[dict] = []
    no_local_price: list[dict] = []
    in_sync = 0

    for (provider, write_key), row in sorted(canonical.items()):
        if model is not None and write_key != model:
            continue
        snap_in = row.get("input_cost_per_million")
        snap_out = row.get("output_cost_per_million")
        if snap_in is None or snap_out is None:
            continue  # incomplete snapshot — nothing comparable
        if write_key.lower() in subscription:
            skipped_subscription.append({"provider": provider, "model": write_key})
            continue
        prices = pricing._resolve_pricing(write_key, provider)
        if prices is None:
            no_local_price.append({"provider": provider, "model": write_key})
            continue
        local_in = float(prices["input"])
        local_out = float(prices["output"])
        in_pct = _drift_pct(local_in, float(snap_in))
        out_pct = _drift_pct(local_out, float(snap_out))
        if abs(in_pct) > threshold_pct or abs(out_pct) > threshold_pct:
            drifted.append(
                {
                    "provider": provider,
                    "model": write_key,
                    "local_input": local_in,
                    "local_output": local_out,
                    "snap_input": float(snap_in),
                    "snap_output": float(snap_out),
                    "input_drift_pct": in_pct,
                    "output_drift_pct": out_pct,
                }
            )
        else:
            in_sync += 1

    written = _apply_drift(drifted) if apply else 0

    return {
        "applied": apply,
        "threshold_pct": threshold_pct,
        "model_filter": model,
        "compared": len(drifted) + in_sync,
        "drifted": drifted,
        "in_sync": in_sync,
        "skipped_subscription": skipped_subscription,
        "no_local_price": no_local_price,
        "coverage_gap": len(db.models_needing_pricing_snapshot()),
        "written": written,
    }


def _apply_drift(drifted: list[dict]) -> int:
    """Placeholder — implemented in Task 4."""
    return 0
