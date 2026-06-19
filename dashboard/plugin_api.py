"""FastAPI router for the hermes-telemetry dashboard plugin.

Mounted by the Hermes dashboard at ``/api/plugins/hermes-telemetry/*``.
Verified against NousResearch/hermes-agent@main:
- Loader: ``hermes_cli/web_server.py::_discover_dashboard_plugins`` scans
  ``~/.hermes/plugins/<name>/dashboard/manifest.json``.
- Mount:  ``hermes_cli/web_server.py::_mount_plugin_api_routes`` imports this
  file via ``importlib.util.spec_from_file_location`` and registers the
  module-level ``router`` attribute.

This module is intentionally **self-contained** — a single file, no relative
imports, no shared code with the standalone dashboard at ``dashboard/serve.py``.
The two surfaces co-locate in ``dashboard/`` (the loader requires it) but
share zero Python. The contract is enforced by
``tests/test_dashboard_plugin_isolation.py``.

Read-only: opens ``telemetry.db`` with ``PRAGMA query_only=ON``; capture
stays in the runtime hooks.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

# ---------------------------------------------------------------------------
# DB helpers (inlined; the loader imports this file standalone, so we can't
# rely on a sibling ``_db`` module being on sys.path).
# ---------------------------------------------------------------------------
_local = threading.local()


def _db_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "telemetry" / "telemetry.db"


def _conn() -> sqlite3.Connection:
    if not getattr(_local, "c", None):
        _local.c = sqlite3.connect(str(_db_path()), isolation_level=None)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA busy_timeout=5000")
        _local.c.execute("PRAGMA query_only=ON")
    return _local.c


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _one(sql: str, params: tuple = ()) -> dict:
    r = _conn().execute(sql, params).fetchone()
    return dict(r) if r else {}


def _coerce_window_hours(value) -> int:
    try:
        wh = int(value)
    except (TypeError, ValueError):
        return 24
    return max(0, wh)


def _since_clause(window_hours, col: str = "started_at") -> str:
    wh = _coerce_window_hours(window_hours)
    if wh <= 0:
        return "1=1"
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=wh)).isoformat()
    return f"{col} >= '{cutoff}'"


# ---------------------------------------------------------------------------
# Health & summary
# ---------------------------------------------------------------------------
@router.get("/health")
def health() -> dict:
    """Smoke endpoint — confirms the plugin is mounted and DB is reachable."""
    try:
        row = _one("SELECT COUNT(*) AS n FROM runs")
        return {"ok": True, "runs_total": int(row.get("n") or 0)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/summary")
def summary(window_hours: int = 24) -> dict:
    sc = _since_clause(window_hours, "started_at")
    sc_ts = _since_clause(window_hours, "ts")
    runs = _one(f"""
        SELECT COUNT(*) AS total_runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status NOT IN ('ok','running') THEN 1 ELSE 0 END) AS failed_runs,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               AVG(duration_ms) AS avg_duration_ms
        FROM runs WHERE {sc}
    """)
    llm = _one(f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE {sc_ts}
    """)
    daily = _rows(f"""
        SELECT DATE(started_at) AS day,
               ROUND(SUM(cost_usd), 4) AS cost,
               COUNT(*) AS runs
        FROM runs WHERE {sc}
        GROUP BY DATE(started_at)
        ORDER BY day
    """)
    return {
        "window_hours": _coerce_window_hours(window_hours),
        "runs": runs,
        "llm": llm,
        "daily_cost": daily,
    }


@router.get("/token-breakdown")
def token_breakdown(window_hours: int = 24) -> dict:
    sc_ts = _since_clause(window_hours, "ts")
    return _one(f"""
        SELECT COALESCE(SUM(tokens_in), 0)         AS tokens_in,
               COALESCE(SUM(tokens_out), 0)        AS tokens_out,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(reasoning_tokens), 0)  AS reasoning_tokens,
               COALESCE(SUM(tokens_in), 0)
                 + COALESCE(SUM(tokens_out), 0)
                 + COALESCE(SUM(cache_read_tokens), 0)
                 + COALESCE(SUM(cache_write_tokens), 0)
                 + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls WHERE {sc_ts}
    """)


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
@router.get("/runs")
def runs(limit: int = 50, window_hours: int = 0) -> dict:
    sc = _since_clause(window_hours, "started_at")
    rows = _rows(
        f"""
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
               cost_usd, duration_ms, api_calls, tool_calls, estimated_llm_calls
        FROM runs
        WHERE {sc}
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 500)),),
    )
    total = _one(f"SELECT COUNT(*) AS n FROM runs WHERE {sc}").get("n") or 0
    return {"total_runs": int(total), "rows": rows}


@router.get("/requests")
def requests(limit: int = 100, window_hours: int = 0) -> dict:
    sc_ts = _since_clause(window_hours, "ts")
    rows = _rows(
        f"""
        SELECT lc.id, lc.ts, lc.session_id, lc.model, lc.provider,
               lc.tokens_in, lc.tokens_out, lc.cache_read_tokens,
               lc.cache_write_tokens, lc.reasoning_tokens,
               lc.cost_usd, lc.latency_ms, lc.estimated, lc.provider_assumed,
               r.platform, r.cron_job_id, r.status
        FROM llm_calls lc
        LEFT JOIN runs r ON r.session_id = lc.session_id
        WHERE {sc_ts}
        ORDER BY lc.ts DESC, lc.id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 1000)),),
    )
    total = _one(f"SELECT COUNT(*) AS n FROM llm_calls WHERE {sc_ts}").get("n") or 0
    return {"total_requests": int(total), "rows": rows}


@router.get("/providers")
def providers(window_hours: int = 24) -> dict:
    sc_ts = _since_clause(window_hours, "ts")
    rows = _rows(f"""
        SELECT COALESCE(provider, '—') AS provider,
               COUNT(*) AS total_calls,
               SUM(CASE WHEN estimated=0 THEN 1 ELSE 0 END) AS real_calls,
               SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END) AS estimated_calls,
               SUM(CASE WHEN provider_assumed=1 THEN 1 ELSE 0 END) AS provider_assumed_calls,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               COALESCE(SUM(tokens_in), 0)  AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(reasoning_tokens), 0)   AS reasoning_tokens
        FROM llm_calls
        WHERE {sc_ts}
        GROUP BY COALESCE(provider, '—')
        ORDER BY cost_usd DESC, total_calls DESC
    """)
    return {"rows": rows}


@router.get("/cron")
def cron(window_hours: int = 168) -> dict:
    sc = _since_clause(window_hours, "started_at")
    rows = _rows(f"""
        SELECT cron_job_id,
               COUNT(*) AS runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)    AS ok_runs,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed_runs,
               COALESCE(SUM(tokens_in), 0)  AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               AVG(duration_ms) AS avg_duration_ms,
               MAX(started_at)  AS last_run
        FROM runs
        WHERE cron_job_id IS NOT NULL AND {sc}
        GROUP BY cron_job_id
        ORDER BY last_run DESC
    """)
    return {"rows": rows}


# ---------------------------------------------------------------------------
# Single-resource detail
# ---------------------------------------------------------------------------
@router.get("/session/{session_id}")
def session_detail(session_id: str) -> dict:
    if not session_id:
        return {"error": "session_id is required"}
    run = _one(
        """
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
               cost_usd, duration_ms, api_calls, tool_calls, estimated_llm_calls
        FROM runs WHERE session_id = ?
        """,
        (session_id,),
    )
    if not run:
        return {"error": "session not found", "session_id": session_id}
    llm_summary = _one(
        """
        SELECT COUNT(*) AS api_calls,
               COALESCE(SUM(tokens_in), 0)  AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               COALESCE(SUM(cache_read_tokens), 0)  AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(reasoning_tokens), 0)   AS reasoning_tokens,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               AVG(latency_ms) AS avg_latency_ms,
               SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END) AS estimated_calls
        FROM llm_calls WHERE session_id = ?
        """,
        (session_id,),
    )
    tool_summary = _one(
        """
        SELECT COUNT(*) AS tool_calls,
               SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_calls,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
               AVG(latency_ms) AS avg_latency_ms
        FROM tool_calls WHERE session_id = ?
        """,
        (session_id,),
    )
    return {"run": run, "llm_summary": llm_summary, "tool_summary": tool_summary}


# ---------------------------------------------------------------------------
# Budget (read-only, global scope)
# ---------------------------------------------------------------------------
def _budget_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "telemetry" / "budget.yaml"


def _window_start_utc(window: str) -> str:
    """Local-tz day/month start, converted back to UTC ISO.

    Matches the convention used by the runtime budget engine: window math
    runs in the user's local tz, then is converted to UTC for the DB query.
    """
    local_now = datetime.now().astimezone()
    if window == "daily":
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "monthly":
        start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unsupported window: {window!r}")
    return start_local.astimezone(timezone.utc).isoformat()


@router.get("/budget")
def budget() -> dict:
    """Read-only view of the global budget scope."""
    path = _budget_path()
    if not path.exists():
        return {"enabled": False}
    try:
        import yaml
    except ImportError:
        return {"enabled": True, "error": "pyyaml not installed in dashboard process"}
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"enabled": True, "error": f"failed to parse budget.yaml: {exc}"}

    g = (cfg.get("budgets", {}) or {}).get("global", {}) or {}
    thresholds = cfg.get("thresholds", {}) or {}
    soft_pct = float(thresholds.get("soft_pct", 0.8))
    hard_pct = float(thresholds.get("hard_pct", 1.0))

    scopes = []
    for window in ("daily", "monthly"):
        limit = g.get(f"{window}_usd")
        if limit is None:
            continue
        start_utc = _window_start_utc(window)
        spend = _one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM runs WHERE started_at >= ?",
            (start_utc,),
        )
        spent = float(spend.get("spent") or 0.0)
        pct = spent / limit if limit > 0 else 0.0
        level = "hard" if pct >= hard_pct else ("soft" if pct >= soft_pct else "ok")
        scopes.append(
            {
                "scope": f"global/{window}",
                "window": window,
                "limit_usd": float(limit),
                "spent_usd": round(spent, 6),
                "pct": round(pct * 100, 1),
                "level": level,
                "window_start_utc": start_utc,
            }
        )

    return {
        "enabled": True,
        "scopes": scopes,
        "on_estimated": (cfg.get("on_estimated", {}) or {}).get("mode", "warn_only"),
    }
