#!/usr/bin/env python3
"""hermes-telemetry dashboard server -- zero dependencies, stdlib only.

Usage:
    python serve.py                            # http://localhost:8765 (loopback only)
    python serve.py --port 9090                # custom port, still loopback
    python serve.py 9090                       # positional port (back-compat)
    python serve.py --host 0.0.0.0             # bind all interfaces (no auth!)

The dashboard has no authentication. By default it binds to 127.0.0.1 so it is
unreachable from other hosts. To view it from another machine, either:

  - Open an SSH tunnel from your client:
        ssh -L 8765:localhost:8765 <user>@<server>
    then browse http://localhost:8765 on the client.

  - Or, on a trusted LAN only, pass --host 0.0.0.0 to bind all interfaces.
    Anyone who can reach the chosen port will see every captured token, cost,
    and tool-call detail with no login. Do not expose this to the public
    internet or to networks that include untrusted hosts.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

logger = logging.getLogger("hermes_telemetry.dashboard")

# ---------------------------------------------------------------------------
# DB / Hermes paths
# ---------------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HERMES_HOME / "telemetry" / "telemetry.db"

_local = threading.local()


def _conn():
    if not getattr(_local, "c", None):
        _local.c = sqlite3.connect(str(DB_PATH), isolation_level=None)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA busy_timeout=5000")
    return _local.c


def _rows(sql, params=()):
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _one(sql, params=()):
    r = _conn().execute(sql, params).fetchone()
    return dict(r) if r else {}


def _since_clause(window_hours, col="started_at"):
    """Return SQL WHERE clause for time window. 0 = all time (no filter).
    Args:
        window_hours: 0 = all time (no filter), otherwise hours back
        col: column name (default 'started_at', use 'ts' for llm_calls)
    """
    wh = int(window_hours)
    if wh == 0:
        return "1=1"
    return f"{col} >= datetime('now', '-{wh} hours')"


def _active_hermes_session_ids():
    """Return session IDs still present in Hermes' active/session files.

    Telemetry is append-only. Hermes session deletion does not delete rows from
    telemetry.db, so dashboard session lists soft-hide rows whose session record
    no longer exists. Cron telemetry is kept separately via session_id LIKE
    'cron_%' because cron runs are not represented in sessions.json.
    """
    hermes_home = HERMES_HOME
    sessions_dir = hermes_home / "sessions"
    sessions_json = sessions_dir / "sessions.json"
    ids = set()
    metadata_available = False

    try:
        if sessions_json.exists():
            metadata_available = True
            data = json.loads(sessions_json.read_text())
            if isinstance(data, dict):
                for item in data.values():
                    if isinstance(item, dict) and item.get("session_id"):
                        ids.add(str(item["session_id"]))
    except Exception:
        # Missing/corrupt Hermes session metadata should not take down telemetry.
        pass

    try:
        if sessions_dir.exists():
            metadata_available = True
            for path in sessions_dir.glob("session_*.json"):
                session_id = path.stem.removeprefix("session_")
                if session_id:
                    ids.add(session_id)
    except Exception:
        pass

    return ids, metadata_available


def _visible_sessions_clause(col="session_id", include_deleted=False):
    if include_deleted:
        return "1=1", []
    active_ids, metadata_available = _active_hermes_session_ids()
    if not metadata_available:
        # If Hermes session metadata is unavailable, degrade gracefully instead of
        # hiding almost every non-cron telemetry row.
        return "1=1", []
    active_ids = sorted(active_ids)
    clauses = [f"{col} LIKE 'cron_%'"]
    params = []
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        clauses.append(f"{col} IN ({placeholders})")
        params.extend(active_ids)
    return "(" + " OR ".join(clauses) + ")", params


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------
def api_summary(window_hours=24):
    since_clause = _since_clause(window_hours, "started_at")
    since_clause_ts = _since_clause(window_hours, "ts")

    runs = _one(f"""
        SELECT
            COUNT(*) AS total_runs,
            SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
            SUM(CASE WHEN status NOT IN ('ok','running') THEN 1 ELSE 0 END) AS failed_runs,
            SUM(tokens_in) AS tokens_in,
            SUM(tokens_out) AS tokens_out,
            ROUND(SUM(cost_usd), 6) AS cost_usd,
            AVG(duration_ms) AS avg_duration_ms,
            SUM(tool_calls) AS tool_calls,
            SUM(estimated_llm_calls) AS estimated_llm_calls
        FROM runs WHERE {since_clause}
    """)

    llm = _one(f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE {since_clause_ts}
    """)

    top_tools = _rows(f"""
        SELECT tool_name, COUNT(*) AS calls,
               SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures,
               AVG(latency_ms) AS avg_ms
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE {_since_clause(window_hours, "r.started_at")}
        GROUP BY tool_name ORDER BY calls DESC LIMIT 10
    """)

    # daily cost chart data (last 7 days for 24h/7d windows, last 30 days for 30d, last 90 days for 90d, unbounded for all-time)
    daily_window = int(window_hours)
    if daily_window == 0:
        daily_cost = _rows("""
            SELECT DATE(started_at) AS day,
                   ROUND(SUM(cost_usd), 4) AS cost,
                   COUNT(*) AS runs
            FROM runs
            GROUP BY DATE(started_at)
            ORDER BY day
        """)
    else:
        daily_cost = _rows(f"""
            SELECT DATE(started_at) AS day,
                   ROUND(SUM(cost_usd), 4) AS cost,
                   COUNT(*) AS runs
            FROM runs
            WHERE started_at >= datetime('now', '-{daily_window // 24} days')
            GROUP BY DATE(started_at)
            ORDER BY day
        """)

    return {
        "window_hours": int(window_hours),
        "runs": runs,
        "llm": llm,
        "top_tools": top_tools,
        "daily_cost": daily_cost,
    }


def api_token_breakdown(window_hours=24):
    """Get detailed token breakdown: input, output, cache_read, cache_write, reasoning.
    Returns None if the DB schema is older and missing token columns."""
    try:
        since_clause_ts = _since_clause(window_hours, "ts")
        # COALESCE each SUM individually: SUM() returns NULL when all rows are NULL,
        # which happens for reasoning_tokens on models that don't emit it.
        return _one(f"""
            SELECT
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)
                    + COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_write_tokens), 0)
                    + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
            FROM llm_calls WHERE {since_clause_ts}
        """)
    except Exception:
        return None


def api_cron(window_hours=168):
    since_clause = _since_clause(window_hours)
    return _rows(f"""
        SELECT cron_job_id,
               COUNT(*) AS runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed_runs,
               SUM(tokens_in) AS tokens_in,
               SUM(tokens_out) AS tokens_out,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               ROUND(SUM(cost_usd), 6) AS cost_usd,
               AVG(duration_ms) AS avg_duration_ms,
               MAX(started_at) AS last_run
        FROM runs
        WHERE cron_job_id IS NOT NULL
          AND {since_clause}
        GROUP BY cron_job_id
        ORDER BY cost_usd DESC
    """)


def api_providers(window_hours=24):
    since_clause_ts = _since_clause(window_hours, "ts")
    return _rows(f"""
        SELECT provider,
               COUNT(*) AS total_calls,
               SUM(CASE WHEN estimated=0 THEN 1 ELSE 0 END) AS real_calls,
               SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END) AS estimated_calls,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               COALESCE(SUM(tokens_in), 0)
                   + COALESCE(SUM(tokens_out), 0)
                   + COALESCE(SUM(cache_read_tokens), 0)
                   + COALESCE(SUM(cache_write_tokens), 0)
                   + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens,
               ROUND(COALESCE(SUM(cost_usd), 0) / NULLIF(COUNT(*), 0), 6) AS avg_cost_per_call
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY provider
        ORDER BY cost_usd DESC, total_tokens DESC, total_calls DESC
    """)


def _build_run_filters(
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    q: str | None = None,
):
    clauses = [_since_clause(window_hours, "started_at")]
    params = []
    if day:
        clauses.append("DATE(started_at) = ?")
        params.append(day)
    if model:
        clauses.append("COALESCE(model, '—') = ?")
        params.append(model)
    if provider:
        clauses.append("COALESCE(provider, '—') = ?")
        params.append(provider)
    if platform:
        clauses.append("COALESCE(platform, 'cli') = ?")
        params.append(platform)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if cron_job_id:
        clauses.append("COALESCE(cron_job_id, '') = ?")
        params.append(cron_job_id)
    if q:
        like = f"%{q}%"
        clauses.append(
            "("
            "session_id LIKE ? OR "
            "COALESCE(model, '') LIKE ? OR "
            "COALESCE(provider, '') LIKE ? OR "
            "COALESCE(cron_job_id, '') LIKE ? OR "
            "COALESCE(platform, '') LIKE ? OR "
            "COALESCE(status, '') LIKE ?"
            ")"
        )
        params.extend([like, like, like, like, like, like])
    return " AND ".join(clauses), params


def api_runs(
    limit=50,
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    q: str | None = None,
    include_deleted=False,
):
    base_where_sql, base_params = _build_run_filters(
        window_hours,
        day,
        model,
        provider,
        platform,
        status,
        session_id,
        cron_job_id,
        q,
    )
    visible_sql, visible_params = _visible_sessions_clause("session_id", include_deleted)
    where_sql = f"{base_where_sql} AND {visible_sql}"
    params = [*base_params, *visible_params]
    rows = _rows(
        f"""
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
               cost_usd, duration_ms,
               api_calls, tool_calls, estimated_llm_calls
        FROM runs
        WHERE {where_sql}
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    )
    total_row = _one(
        f"SELECT COUNT(*) AS total_runs FROM runs WHERE {where_sql}",
        tuple(params),
    )
    raw_total_row = _one(
        f"SELECT COUNT(*) AS total_runs FROM runs WHERE {base_where_sql}",
        tuple(base_params),
    )
    total_runs = int(total_row.get("total_runs") or 0)
    raw_total_runs = int(raw_total_row.get("total_runs") or 0)
    return {
        "filters": {
            "hours": int(window_hours),
            "day": day,
            "model": model,
            "provider": provider,
            "platform": platform,
            "status": status,
            "session_id": session_id,
            "cron_job_id": cron_job_id,
            "q": q,
            "include_deleted": bool(include_deleted),
        },
        "total_runs": total_runs,
        "hidden_deleted_runs": max(0, raw_total_runs - total_runs),
        "rows": rows,
    }


def api_model_tokens(window_hours=24, limit=100):
    since_clause_ts = _since_clause(window_hours, "ts")
    return _rows(
        f"""
        SELECT
            COALESCE(model, '—') AS model,
            COUNT(*) AS api_calls,
            COALESCE(SUM(tokens_in), 0) AS tokens_in,
            COALESCE(SUM(tokens_out), 0) AS tokens_out,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
            ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
            COALESCE(SUM(tokens_in), 0)
                + COALESCE(SUM(tokens_out), 0)
                + COALESCE(SUM(cache_read_tokens), 0)
                + COALESCE(SUM(cache_write_tokens), 0)
                + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY COALESCE(model, '—')
        ORDER BY total_tokens DESC, api_calls DESC
        LIMIT ?
        """,
        (int(limit),),
    )


def api_daily_tokens(window_hours=24, page=1, per_page=15):
    since_clause_ts = _since_clause(window_hours, "ts")
    page = max(1, int(page))
    per_page = max(1, min(15, int(per_page)))

    total_days_row = _one(
        f"""
        SELECT COUNT(*) AS total_days
        FROM (
            SELECT DATE(ts) AS day
            FROM llm_calls
            WHERE {since_clause_ts}
            GROUP BY DATE(ts)
        ) d
        """
    )
    total_days = int(total_days_row.get("total_days") or 0)
    total_pages = max(1, (total_days + per_page - 1) // per_page) if total_days else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows = _rows(
        f"""
        SELECT
            DATE(ts) AS day,
            COUNT(*) AS api_calls,
            COALESCE(SUM(tokens_in), 0) AS tokens_in,
            COALESCE(SUM(tokens_out), 0) AS tokens_out,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
            ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
            COALESCE(SUM(tokens_in), 0)
                + COALESCE(SUM(tokens_out), 0)
                + COALESCE(SUM(cache_read_tokens), 0)
                + COALESCE(SUM(cache_write_tokens), 0)
                + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY DATE(ts)
        ORDER BY day DESC
        LIMIT ? OFFSET ?
        """,
        (per_page, offset),
    )

    return {
        "page": page,
        "per_page": per_page,
        "total_days": total_days,
        "total_pages": total_pages,
        "rows": rows,
    }


def api_daily_token_chart(window_hours=24, limit_days=90):
    since_clause_ts = _since_clause(window_hours, "ts")
    limit_days = max(1, min(180, int(limit_days)))
    rows = _rows(
        f"""
        SELECT *
        FROM (
            SELECT
                DATE(ts) AS day,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(tokens_in), 0)
                    + COALESCE(SUM(tokens_out), 0)
                    + COALESCE(SUM(cache_read_tokens), 0)
                    + COALESCE(SUM(cache_write_tokens), 0)
                    + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
            FROM llm_calls
            WHERE {since_clause_ts}
            GROUP BY DATE(ts)
            ORDER BY day DESC
            LIMIT ?
        ) d
        ORDER BY day ASC
        """,
        (limit_days,),
    )
    return rows


def api_daily_model_chart(window_hours=24, limit_days=90, top_n=5):
    since_clause_ts = _since_clause(window_hours, "ts")
    limit_days = max(1, min(180, int(limit_days)))
    top_n = max(1, min(8, int(top_n)))

    top_models = _rows(
        f"""
        SELECT COALESCE(model, '—') AS model,
               COALESCE(SUM(tokens_in), 0)
                   + COALESCE(SUM(tokens_out), 0)
                   + COALESCE(SUM(cache_read_tokens), 0)
                   + COALESCE(SUM(cache_write_tokens), 0)
                   + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY COALESCE(model, '—')
        ORDER BY total_tokens DESC
        LIMIT ?
        """,
        (top_n,),
    )
    model_names = [r["model"] for r in top_models]

    daily_rows = _rows(
        f"""
        SELECT *
        FROM (
            SELECT
                DATE(ts) AS day,
                COALESCE(model, '—') AS model,
                COALESCE(SUM(tokens_in), 0)
                    + COALESCE(SUM(tokens_out), 0)
                    + COALESCE(SUM(cache_read_tokens), 0)
                    + COALESCE(SUM(cache_write_tokens), 0)
                    + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
            FROM llm_calls
            WHERE {since_clause_ts}
            GROUP BY DATE(ts), COALESCE(model, '—')
            ORDER BY day DESC
            LIMIT 100000
        ) d
        ORDER BY day ASC
        """
    )

    day_order = []
    day_map = {}
    for row in daily_rows:
        day = row["day"]
        if day not in day_map:
            day_order.append(day)
            day_map[day] = {"day": day, "models": {m: 0 for m in model_names}, "other": 0}
        if row["model"] in day_map[day]["models"]:
            day_map[day]["models"][row["model"]] += row["total_tokens"] or 0
        else:
            day_map[day]["other"] += row["total_tokens"] or 0

    if len(day_order) > limit_days:
        day_order = day_order[-limit_days:]

    return {
        "models": model_names,
        "rows": [day_map[d] for d in day_order],
    }


def api_session_detail(session_id: str):
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
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               AVG(latency_ms) AS avg_latency_ms,
               SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END) AS estimated_calls
        FROM llm_calls
        WHERE session_id = ?
        """,
        (session_id,),
    )

    llm_calls = _rows(
        """
        SELECT ts, model, provider, tokens_in, tokens_out,
               cache_read_tokens, cache_write_tokens, reasoning_tokens,
               cost_usd, latency_ms, estimated
        FROM llm_calls
        WHERE session_id = ?
        ORDER BY ts DESC, id DESC
        LIMIT 100
        """,
        (session_id,),
    )

    provider_models = _rows(
        """
        SELECT COALESCE(provider, '—') AS provider,
               COALESCE(model, '—') AS model,
               COUNT(*) AS api_calls,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               COALESCE(SUM(tokens_in), 0)
                   + COALESCE(SUM(tokens_out), 0)
                   + COALESCE(SUM(cache_read_tokens), 0)
                   + COALESCE(SUM(cache_write_tokens), 0)
                   + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls
        WHERE session_id = ?
        GROUP BY COALESCE(provider, '—'), COALESCE(model, '—')
        ORDER BY total_tokens DESC, cost_usd DESC
        LIMIT 20
        """,
        (session_id,),
    )

    tool_summary = _one(
        """
        SELECT COUNT(*) AS tool_calls,
               SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_calls,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
               AVG(latency_ms) AS avg_latency_ms
        FROM tool_calls
        WHERE session_id = ?
        """,
        (session_id,),
    )

    tool_calls = _rows(
        """
        SELECT ts, tool_name, ok, latency_ms
        FROM tool_calls
        WHERE session_id = ?
        ORDER BY ts DESC, id DESC
        LIMIT 100
        """,
        (session_id,),
    )

    return {
        "run": run,
        "llm_summary": llm_summary,
        "llm_calls": llm_calls,
        "provider_models": provider_models,
        "tool_summary": tool_summary,
        "tool_calls": tool_calls,
    }


def api_cache_efficiency(window_hours=24):
    since_clause_ts = _since_clause(window_hours, "ts")
    overall = _one(
        f"""
        SELECT COUNT(*) AS api_calls,
               COUNT(DISTINCT session_id) AS sessions,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) AS calls_with_cache,
               COUNT(DISTINCT CASE WHEN cache_read_tokens > 0 THEN session_id END) AS sessions_with_cache
        FROM llm_calls
        WHERE {since_clause_ts}
        """
    )
    tokens_in = int(overall.get("tokens_in") or 0)
    cache_read = int(overall.get("cache_read_tokens") or 0)
    cacheable_total = tokens_in + cache_read
    overall["cache_hit_share_pct"] = (
        round((cache_read / cacheable_total) * 100, 2) if cacheable_total else 0.0
    )
    overall["cache_calls_pct"] = (
        round(((overall.get("calls_with_cache") or 0) / (overall.get("api_calls") or 1)) * 100, 2)
        if overall.get("api_calls")
        else 0.0
    )
    overall["estimated_cache_saved_tokens"] = cache_read

    by_model = _rows(
        f"""
        SELECT COALESCE(model, '—') AS model,
               COUNT(*) AS api_calls,
               COUNT(DISTINCT session_id) AS sessions,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) AS calls_with_cache
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY COALESCE(model, '—')
        HAVING COUNT(*) > 0
        ORDER BY cache_read_tokens DESC, tokens_in DESC
        LIMIT 20
        """
    )
    for row in by_model:
        cacheable = (row.get("tokens_in") or 0) + (row.get("cache_read_tokens") or 0)
        row["cache_hit_share_pct"] = (
            round(((row.get("cache_read_tokens") or 0) / cacheable) * 100, 2) if cacheable else 0.0
        )
        row["cache_calls_pct"] = round(
            ((row.get("calls_with_cache") or 0) / (row.get("api_calls") or 1)) * 100, 2
        )

    return {"overall": overall, "by_model": by_model}


def api_budget():
    budget_path = DB_PATH.parent / "budget.yaml"
    if not budget_path.exists():
        return {"enabled": False}

    try:
        import yaml

        cfg = yaml.safe_load(budget_path.read_text())
    except ImportError:
        return {"enabled": True, "raw": budget_path.read_text()}

    budgets = cfg.get("budgets", {})
    thresholds = cfg.get("thresholds", {})
    on_est = cfg.get("on_estimated", {})

    # evaluate each scope
    now = datetime.now(timezone.utc)
    now.strftime("%Y-%m-%d")
    now.strftime("%Y-%m")
    local_now = now.astimezone()
    local_now.strftime("%Y-%m-%d")
    local_now.strftime("%Y-%m")

    scopes = []

    # global daily
    g = budgets.get("global", {})
    for win_key, win_label, since in [
        ("daily", "global/daily", now - timedelta(hours=24)),
        ("monthly", "monthly", now - timedelta(days=30)),
    ]:
        limit = g.get(f"{win_key}_usd")
        if limit is None:
            continue
        spend = _one(
            "SELECT COALESCE(SUM(cost_usd),0.0) AS spent, COALESCE(SUM(estimated_llm_calls),0) AS est, COALESCE(SUM(api_calls),0) AS total FROM runs WHERE started_at >= ?",
            (since.isoformat(),),
        )
        spent = float(spend.get("spent", 0))
        pct = spent / limit if limit > 0 else 0
        soft_pct = thresholds.get("soft_pct", 0.8)
        hard_pct = thresholds.get("hard_pct", 1.0)
        level = "ok"
        if pct >= hard_pct:
            level = "hard"
        elif pct >= soft_pct:
            level = "soft"
        scopes.append(
            {
                "scope": win_label,
                "spent": round(spent, 6),
                "limit": limit,
                "pct": round(pct * 100, 1),
                "level": level,
                "estimated_calls": spend.get("est", 0),
                "total_calls": spend.get("total", 0),
            }
        )

    return {"enabled": True, "budgets": scopes, "on_estimated": on_est.get("mode", "warn_only")}


MAX_BUDGET_PAYLOAD = 1_048_576  # 1 MiB

# Allowed values for budget config — prevents YAML key injection
ALLOWED_SCOPES = {"global", "per_cron_job", "per_sender"}
ALLOWED_WINDOWS = {"daily", "monthly"}


def _validate_threshold(key: str, value, cfg: dict) -> str | None:
    """Validate and store a threshold (soft_pct/hard_pct). Returns error msg or None."""
    try:
        val = float(value)
    except (ValueError, TypeError):
        return f"{key} must be a number"
    if not (0 <= val <= 1):
        return f"{key} must be between 0 and 1"
    cfg.setdefault("thresholds", {})[key] = val
    return None


def api_budget_update(payload):
    """Update budget.yaml from POST payload. Returns updated budget status or error."""
    budget_path = DB_PATH.parent / "budget.yaml"
    if not budget_path.exists():
        return {"enabled": False, "error": "budget.yaml not found"}

    try:
        import yaml
    except ImportError:
        return {"enabled": False, "error": "PyYAML not installed"}

    try:
        cfg = yaml.safe_load(budget_path.read_text()) or {}
    except Exception as e:
        return {"enabled": False, "error": f"Failed to parse budget.yaml: {e}"}

    # Expected payload: {"scope": "global", "window": "daily", "limit_usd": 5.0}
    scope = payload.get("scope", "global")
    window = payload.get("window", "daily")
    limit_usd = payload.get("limit_usd")

    # Validate scope/window against allowed sets (prevents YAML key injection)
    if scope not in ALLOWED_SCOPES:
        return {"enabled": False, "error": f"invalid scope: {scope!r}"}
    if window not in ALLOWED_WINDOWS:
        return {"enabled": False, "error": f"invalid window: {window!r}"}

    if limit_usd is None:
        return {"enabled": False, "error": "limit_usd is required"}

    try:
        limit_usd = float(limit_usd)
    except (ValueError, TypeError):
        return {"enabled": False, "error": "limit_usd must be a number"}

    if limit_usd < 0:
        return {"enabled": False, "error": "limit_usd must be >= 0"}

    # Initialize budgets structure if missing
    if "budgets" not in cfg:
        cfg["budgets"] = {}
    if scope not in cfg["budgets"]:
        cfg["budgets"][scope] = {}

    # Update the limit
    key = f"{window}_usd"
    cfg["budgets"][scope][key] = limit_usd

    # Optional: update thresholds (validated, no silent failures)
    for field, yaml_key in (("soft_pct", "soft_pct"), ("hard_pct", "hard_pct")):
        if field in payload:
            err = _validate_threshold(yaml_key, payload[field], cfg)
            if err:
                return {"enabled": False, "error": err}

    if "on_estimated_mode" in payload:
        mode = payload["on_estimated_mode"]
        if mode not in ("warn_only", "enforce"):
            return {"enabled": False, "error": "on_estimated_mode must be 'warn_only' or 'enforce'"}
        cfg.setdefault("on_estimated", {})["mode"] = mode

    # Write back atomically via Path.replace()
    try:
        import yaml

        tmp = budget_path.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        tmp.replace(budget_path)
    except Exception as e:
        return {"enabled": False, "error": f"Failed to write budget.yaml: {e}"}

    # Return updated status by calling api_budget()
    return api_budget()


def api_budget_detail(scope: str, window: str):
    """Get raw budget config for a specific scope/window for modal pre-fill."""
    if scope not in ALLOWED_SCOPES or window not in ALLOWED_WINDOWS:
        return {"error": "invalid scope or window"}
    budget_path = DB_PATH.parent / "budget.yaml"
    if not budget_path.exists():
        return {"error": "budget.yaml not found"}
    try:
        import yaml
    except ImportError:
        return {"error": "PyYAML not installed"}
    try:
        cfg = yaml.safe_load(budget_path.read_text()) or {}
    except Exception as e:
        return {"error": f"Failed to parse budget.yaml: {e}"}

    budgets = cfg.get("budgets", {})
    thresholds = cfg.get("thresholds", {})
    on_est = cfg.get("on_estimated", {})

    limit = budgets.get(scope, {}).get(f"{window}_usd")

    return {
        "scope": scope,
        "window": window,
        "limit_usd": limit,
        "soft_pct": thresholds.get("soft_pct", 0.8),
        "hard_pct": thresholds.get("hard_pct", 1.0),
        "on_estimated_mode": on_est.get("mode", "warn_only"),
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # API routes
        if path == "/api/summary":
            qs = parse_qs(parsed.query)
            return self._json(api_summary(qs.get("hours", [24])[0]))

        if path == "/api/cron":
            qs = parse_qs(parsed.query)
            return self._json(api_cron(qs.get("hours", [168])[0]))

        if path == "/api/providers":
            qs = parse_qs(parsed.query)
            return self._json(api_providers(int(qs.get("hours", [24])[0])))

        if path == "/api/cache-efficiency":
            qs = parse_qs(parsed.query)
            return self._json(api_cache_efficiency(int(qs.get("hours", [24])[0])))

        if path == "/api/runs":
            qs = parse_qs(parsed.query)
            return self._json(
                api_runs(
                    int(qs.get("limit", [50])[0]),
                    int(qs.get("hours", [0])[0]),
                    qs.get("day", [None])[0],
                    qs.get("model", [None])[0],
                    qs.get("provider", [None])[0],
                    qs.get("platform", [None])[0],
                    qs.get("status", [None])[0],
                    qs.get("session_id", [None])[0],
                    qs.get("cron_job_id", [None])[0],
                    qs.get("q", [None])[0],
                    qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                )
            )

        if path == "/api/session-detail":
            qs = parse_qs(parsed.query)
            return self._json(api_session_detail(qs.get("session_id", [""])[0]))

        if path == "/api/model-tokens":
            qs = parse_qs(parsed.query)
            return self._json(
                api_model_tokens(
                    int(qs.get("hours", [24])[0]),
                    int(qs.get("limit", [100])[0]),
                )
            )

        if path == "/api/daily-tokens":
            qs = parse_qs(parsed.query)
            return self._json(
                api_daily_tokens(
                    int(qs.get("hours", [24])[0]),
                    int(qs.get("page", [1])[0]),
                    int(qs.get("per_page", [15])[0]),
                )
            )

        if path == "/api/daily-token-chart":
            qs = parse_qs(parsed.query)
            return self._json(
                api_daily_token_chart(
                    int(qs.get("hours", [24])[0]),
                    int(qs.get("limit_days", [90])[0]),
                )
            )

        if path == "/api/daily-model-chart":
            qs = parse_qs(parsed.query)
            return self._json(
                api_daily_model_chart(
                    int(qs.get("hours", [24])[0]),
                    int(qs.get("limit_days", [90])[0]),
                    int(qs.get("top_n", [5])[0]),
                )
            )

        if path == "/api/budget":
            return self._json(api_budget())

        if path == "/api/budget/detail":
            qs = parse_qs(parsed.query)
            scope = qs.get("scope", ["global"])[0]
            window = qs.get("window", ["daily"])[0]
            return self._json(api_budget_detail(scope, window))

        if path == "/api/token-breakdown":
            qs = parse_qs(parsed.query)
            return self._json(api_token_breakdown(int(qs.get("hours", [24])[0])))

        # Static: serve index.html for /
        if path == "/" or path == "/index.html":
            html = (SCRIPT_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
            return

        # Try to serve other static files from the same directory
        fpath = SCRIPT_DIR / path.lstrip("/")
        if fpath.is_file():
            super().do_GET()
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/budget":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_BUDGET_PAYLOAD:
                self.send_response(413)
                self.end_headers()
                self.wfile.write(b"Payload too large")
                return
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid JSON")
                return
            result = api_budget_update(payload)
            # Return proper HTTP status: 200 for success, 400 for validation errors
            status = 200 if result.get("enabled") or "error" not in result else 400
            return self._json(result, status)

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    """Parse command-line arguments.

    Back-compat: the original signature was `serve.py [port]` — a single
    positional integer. Preserved so existing scripts and docs keep working.
    """
    parser = argparse.ArgumentParser(
        prog="serve.py",
        description="hermes-telemetry dashboard server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  serve.py                       # bind 127.0.0.1:8765\n"
            "  serve.py --port 9090           # custom port, still loopback\n"
            "  serve.py 9090                  # positional port (back-compat)\n"
            "  serve.py --host 0.0.0.0        # all interfaces (NO AUTH!)\n"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"Interface to bind to (default: {DEFAULT_HOST}). Use 0.0.0.0 to "
            "expose on every interface — the dashboard has NO authentication, "
            "so only do this on a trusted LAN."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to bind to (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "port_positional",
        nargs="?",
        type=int,
        default=None,
        metavar="PORT",
        help="Back-compat: positional port (use --port instead).",
    )
    args = parser.parse_args(argv)

    port = args.port if args.port is not None else args.port_positional
    if port is None:
        port = DEFAULT_PORT
    return args.host, port


def _warn_if_exposed(host: str) -> None:
    """Print a clear warning when binding to anything except loopback."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    msg = (
        f"WARNING: binding dashboard on {host} exposes it to every host "
        "that can reach this interface, and the dashboard has NO "
        "authentication. Anyone who reaches the port will see every "
        "captured token, cost, and tool-call detail. Do not expose to "
        "the public internet or to untrusted networks."
    )
    print(msg, file=sys.stderr)
    logger.warning("Dashboard bound on %s with no authentication.", host)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    host, port = _parse_args(argv)

    if not DB_PATH.exists():
        print(f"ERROR: telemetry DB not found at {DB_PATH}")
        print("Make sure hermes-telemetry plugin has captured data.")
        sys.exit(1)

    _warn_if_exposed(host)

    server = HTTPServer((host, port), Handler)
    display_host = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"hermes-telemetry dashboard at http://{display_host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
