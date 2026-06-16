"""FastAPI router for the hermes-telemetry dashboard plugin.

Mounted by the Hermes dashboard at ``/api/plugins/hermes-telemetry/*``.
See ``hermes_cli/web_server.py::_mount_plugin_api_routes`` (verified against
NousResearch/hermes-agent@main).

Read-only: the plugin reads ``telemetry.db`` but never writes. Capture stays
in the runtime hooks.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import _db

router = APIRouter()


@router.get("/summary")
def summary(window_hours: int = 24) -> dict:
    sc = _db.since_clause(window_hours, "started_at")
    sc_ts = _db.since_clause(window_hours, "ts")
    runs = _db.one(f"""
        SELECT COUNT(*) AS total_runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status NOT IN ('ok','running') THEN 1 ELSE 0 END) AS failed_runs,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               AVG(duration_ms) AS avg_duration_ms
        FROM runs WHERE {sc}
    """)
    llm = _db.one(f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE {sc_ts}
    """)
    daily = _db.rows(f"""
        SELECT DATE(started_at) AS day,
               ROUND(SUM(cost_usd), 4) AS cost,
               COUNT(*) AS runs
        FROM runs WHERE {sc}
        GROUP BY DATE(started_at)
        ORDER BY day
    """)
    return {
        "window_hours": _db.coerce_window_hours(window_hours),
        "runs": runs,
        "llm": llm,
        "daily_cost": daily,
    }


@router.get("/health")
def health() -> dict:
    """Smoke endpoint — confirms the plugin is mounted and DB is reachable."""
    try:
        row = _db.one("SELECT COUNT(*) AS n FROM runs")
        return {"ok": True, "runs_total": int(row.get("n") or 0)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
