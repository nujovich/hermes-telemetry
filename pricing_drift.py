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

import json
import logging

from . import db, paths, pricing

logger = logging.getLogger(__name__)


def _drift_pct(local: float, snap: float) -> float | None:
    """Signed drift of local vs snapshot as a percentage: (local/snap - 1) * 100.

    Returns None when snap is 0 — the ratio is undefined (division by zero),
    not an infinite drift. Callers that need a drift *decision* (not a display
    percentage) for the zero-snapshot case should use `_is_drift` instead.
    """
    if snap == 0:
        return None
    return (local / snap - 1.0) * 100.0


def _is_drift(local: float, snap: float, threshold_pct: float) -> bool:
    """True if local drifts from snap beyond threshold. A zero snapshot with a
    nonzero local price is always drift (was free / unpriced, now priced).

    A tiny epsilon is added to the threshold to absorb binary floating-point
    noise at the exact boundary (e.g. 1.01 / 1.0 evaluates to
    1.0000000000000009, not 1.0, purely from float representation) so a value
    at *exactly* the threshold percentage doesn't spuriously read as drift.
    """
    if snap == 0:
        return local != 0
    return abs((local / snap - 1.0) * 100.0) > threshold_pct + 1e-9


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
        if prices is None or prices.get("_provider_assumed"):
            no_local_price.append({"provider": provider, "model": write_key})
            continue
        local_in = float(prices["input"])
        local_out = float(prices["output"])
        in_drift = _is_drift(local_in, float(snap_in), threshold_pct)
        out_drift = _is_drift(local_out, float(snap_out), threshold_pct)
        if in_drift or out_drift:
            drifted.append(
                {
                    "provider": provider,
                    "model": write_key,
                    "local_input": local_in,
                    "local_output": local_out,
                    "snap_input": float(snap_in),
                    "snap_output": float(snap_out),
                    "input_drift_pct": _drift_pct(local_in, float(snap_in)),
                    "output_drift_pct": _drift_pct(local_out, float(snap_out)),
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
        # Global by design: a data-health signal about the whole snapshot store,
        # independent of `model` — NOT scoped by the model filter above. A
        # narrower drift run should still surface store-wide coverage gaps.
        "coverage_gap": len(db.models_needing_pricing_snapshot()),
        "written": written,
    }


def _fmt_pct(pct: float | None) -> str:
    """Format a drift percentage. None means the snapshot rate was 0 (the model
    was free / unpriced), so a ratio is undefined — show that instead of a number."""
    return "was $0" if pct is None else f"{pct:+.1f}%"


def render(result: dict) -> str:
    """Human-readable report for a run() result."""
    if result["applied"]:
        return (
            "Pricing drift (--apply)\n"
            f"  Rewrote {result['written']} model(s) in pricing.yaml from core snapshots."
        )
    lines = [
        f"Pricing drift (dry-run, threshold {result['threshold_pct']:.2f}%)",
        f"  Compared (snapshot + local price): {result['compared']}",
        f"  In sync:                           {result['in_sync']}",
        f"  Drifted:                           {len(result['drifted'])}",
    ]
    for d in result["drifted"]:
        lines.append(
            f"    - {d['model']} ({d['provider']}): "
            f"input {d['local_input']:.4f} vs {d['snap_input']:.4f} "
            f"({_fmt_pct(d['input_drift_pct'])}), "
            f"output {d['local_output']:.4f} vs {d['snap_output']:.4f} "
            f"({_fmt_pct(d['output_drift_pct'])})"
        )
    if result["skipped_subscription"]:
        lines.append(f"  Skipped (subscription): {len(result['skipped_subscription'])}")
    if result["no_local_price"]:
        lines.append(f"  Snapshot but no pricing.yaml entry: {len(result['no_local_price'])}")
        for n in result["no_local_price"]:
            lines.append(f"      - {n['model']} ({n['provider']})")
    # Coverage-gap nudge is DB-wide; only surface it on an unfiltered run so a
    # scoped --model check doesn't nag about unrelated uncovered models.
    if result["coverage_gap"] and result["model_filter"] is None:
        lines.append(
            f"  ! {result['coverage_gap']} model(s) in llm_calls have NO snapshot — "
            "run `hermes telemetry pricing backfill --apply` for full coverage."
        )
    if result["drifted"]:
        lines.append(
            f"  Run with --apply to rewrite {len(result['drifted'])} model(s) in pricing.yaml."
        )
    return "\n".join(lines)


def to_json(result: dict) -> str:
    return json.dumps(result, default=str)


def _apply_drift(drifted: list[dict]) -> int:
    """Write each drifted model's snapshot input/output rates into pricing.yaml.

    Merge (never clobber): load the existing file, update only input/output on the
    matching (case-insensitive) model entry, tag it _source: core-snapshot, and
    dump back with sort_keys=False (preserve insertion order). Routes through
    paths.get_pricing_path() at call time (honors HERMES_TELEMETRY_HOME), then
    hot-reloads pricing.py's cache. Returns the number of entries written.

    pricing.yaml is keyed by model only, so if the same model drifted under two
    providers (run() keys canonical by (provider, model)), only the first is
    written; a conflicting second rate is logged and skipped rather than silently
    overwriting.
    """
    if not drifted:
        return 0

    import yaml

    path = paths.get_pricing_path()
    data: dict = {}
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    models = data.setdefault("models", {}) if "models" in data or "defaults" in data else data

    written = 0
    applied: dict[str, tuple] = {}
    for d in drifted:
        target = d["model"].lower()
        rates = (d["snap_input"], d["snap_output"])
        if target in applied:
            if applied[target] != rates:
                logger.warning(
                    "hermes-telemetry: conflicting core rates for model %r across "
                    "providers (%s vs %s) — keeping the first, skipping the rest. "
                    "pricing.yaml is keyed by model only, so per-provider rates "
                    "cannot both be pinned.",
                    d["model"],
                    applied[target],
                    rates,
                )
            continue
        existing_key = next((k for k in models if str(k).lower() == target), None)
        if existing_key is not None:
            entry = models[existing_key]
            if not isinstance(entry, dict):
                entry = {}
                models[existing_key] = entry
        else:
            entry = {}
            models[target] = entry
        entry["input"] = d["snap_input"]
        entry["output"] = d["snap_output"]
        entry["_source"] = "core-snapshot"
        applied[target] = rates
        written += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    pricing.reload_custom_pricing()
    return written
