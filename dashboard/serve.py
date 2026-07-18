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

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

logger = logging.getLogger("hermes_telemetry.dashboard")

# ---------------------------------------------------------------------------
# DB / Hermes paths
# ---------------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
# Telemetry files honor the opt-in canonical home (HERMES_TELEMETRY_HOME), so a
# multi-profile user can point every profile's dashboard at one shared DB. This
# mirrors paths.get_telemetry_home(); serve.py replicates it inline because the
# standalone dashboard shares no code with the package (see
# tests/test_dashboard_plugin_isolation.py). state.db / cron stay on HERMES_HOME.
_TELEMETRY_HOME = (
    Path(
        os.environ.get("HERMES_TELEMETRY_HOME")
        or os.environ.get("HERMES_HOME")
        or str(Path.home() / ".hermes")
    )
    / "telemetry"
)
DB_PATH = _TELEMETRY_HOME / "telemetry.db"
STATE_DB_PATH = HERMES_HOME / "state.db"
CRON_JOBS_PATH = HERMES_HOME / "cron" / "jobs.json"
CRON_OUTPUT_DIR = HERMES_HOME / "cron" / "output"

_REPO_ROOT = Path(__file__).resolve().parents[1]
if "hermes_telemetry" not in sys.modules:
    _pkg = types.ModuleType("hermes_telemetry")
    _pkg.__path__ = [str(_REPO_ROOT)]
    _pkg.__package__ = "hermes_telemetry"
    _pkg.__file__ = str(_REPO_ROOT / "__init__.py")
    sys.modules["hermes_telemetry"] = _pkg

_local = threading.local()


def _register_sqlite_functions(conn: sqlite3.Connection):
    conn.create_function("dashboard_period", 3, _sqlite_dashboard_period)
    conn.create_function("dashboard_period_start", 3, _sqlite_dashboard_period_start)


def _conn():
    if not getattr(_local, "c", None):
        _local.c = sqlite3.connect(str(DB_PATH), isolation_level=None)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA busy_timeout=5000")
        _register_sqlite_functions(_local.c)
    return _local.c


def _rows(sql, params=()):
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _one(sql, params=()):
    r = _conn().execute(sql, params).fetchone()
    return dict(r) if r else {}


def _state_rows(sql, params=()):
    if not STATE_DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(STATE_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    _register_sqlite_functions(conn)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _normalize_dashboard_tz_name(tz_name: str | None):
    text = (tz_name or "").strip()
    if not text or text.lower() == "local":
        return None
    if text.upper() == "UTC":
        return "UTC"
    return text


def _coerce_utc_dt(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if text == "UTC":
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sqlite_dashboard_period(value, granularity, tz_name):
    dt_utc = _coerce_utc_dt(value)
    if dt_utc is None:
        return None
    tzinfo, _ = _dashboard_viewer_tz(_normalize_dashboard_tz_name(tz_name))
    dt_local = dt_utc.astimezone(tzinfo)
    granularity = _normalize_granularity(granularity)
    if granularity == "minute":
        return dt_local.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    if granularity == "hour":
        return dt_local.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
    if granularity == "day":
        return dt_local.date().isoformat()
    if granularity == "week":
        monday = (dt_local - timedelta(days=dt_local.weekday())).date()
        return monday.isoformat()
    return dt_local.strftime("%Y-%m")


def _sqlite_dashboard_period_start(value, granularity, tz_name):
    label = _sqlite_dashboard_period(value, granularity, tz_name)
    if not label:
        return None
    granularity = _normalize_granularity(granularity)
    if granularity == "month":
        return f"{label}-01"
    return label


def _read_cron_jobs():
    if not CRON_JOBS_PATH.exists():
        return []
    try:
        data = json.loads(CRON_JOBS_PATH.read_text())
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, list) else []


def _since_cutoff_epoch(window_hours):
    wh = _coerce_window_hours(window_hours)
    if wh <= 0:
        return None
    return datetime.now(timezone.utc).timestamp() - (wh * 3600)


def _parse_cron_output_ts(path: Path) -> datetime | None:
    try:
        naive = datetime.strptime(path.stem, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return naive.replace(tzinfo=local_tz).astimezone(timezone.utc)


def _cron_scheduler_runs(window_hours=0):
    cutoff = None
    wh = _coerce_window_hours(window_hours)
    if wh > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=wh)
    runs = []
    for job in _read_cron_jobs():
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        job_dir = CRON_OUTPUT_DIR / job_id
        if not job_dir.exists():
            continue
        for path in sorted(job_dir.glob("*.md")):
            ts = _parse_cron_output_ts(path)
            if ts is None:
                continue
            if cutoff is not None and ts < cutoff:
                continue
            runs.append({"cron_job_id": job_id, "ts": ts})
    return runs


def _cron_scheduler_totals(window_hours=0):
    runs = _cron_scheduler_runs(window_hours)
    counts = {}
    last_seen = {}
    for row in runs:
        job_id = row["cron_job_id"]
        counts[job_id] = counts.get(job_id, 0) + 1
        prev = last_seen.get(job_id)
        if prev is None or row["ts"] > prev:
            last_seen[job_id] = row["ts"]

    job_meta = {str(job.get("id") or ""): job for job in _read_cron_jobs() if job.get("id")}
    rows = []
    for job_id, job in job_meta.items():
        runs_count = counts.get(job_id, 0)
        if _coerce_window_hours(window_hours) <= 0 and isinstance(job.get("repeat"), dict):
            completed = job["repeat"].get("completed")
            if isinstance(completed, int):
                runs_count = max(runs_count, completed)
        last_run = job.get("last_run_at")
        if not last_run and last_seen.get(job_id):
            last_run = last_seen[job_id].isoformat()
        rows.append(
            {
                "cron_job_id": job_id,
                "job_name": job.get("name") or job_id,
                "schedule_display": job.get("schedule_display") or "",
                "runs": runs_count,
                "last_run": last_run,
                "last_status": job.get("last_status") or "—",
                "enabled": bool(job.get("enabled", True)),
            }
        )
    return rows


def _since_cutoff_iso(window_hours):
    """UTC ISO cutoff matching the stored timestamp format in telemetry.db.

    runs.started_at and llm_calls.ts are stored as UTC ISO-8601 strings with a
    +00:00 offset. Using SQLite datetime('now', ...) creates a different string
    format and breaks lexicographic comparisons, so generate the cutoff in the
    same format as the stored data.
    """
    wh = _coerce_window_hours(window_hours)
    if wh <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=wh)).isoformat()


def _since_clause(window_hours, col="started_at"):
    """Return SQL WHERE clause for time window. 0 = all time (no filter)."""
    cutoff = _since_cutoff_iso(window_hours)
    if cutoff is None:
        return "1=1"
    return f"{col} >= '{cutoff}'"


def _normalize_granularity(granularity: str | None) -> str:
    g = (granularity or "day").strip().lower()
    if g not in {"minute", "hour", "day", "week", "month"}:
        raise ValueError(f"invalid granularity: {granularity!r}")
    return g


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _period_label_expr(col: str, granularity: str, tz_name: str | None = None) -> str:
    granularity = _normalize_granularity(granularity)
    tz_value = _normalize_dashboard_tz_name(tz_name) or "local"
    return f"dashboard_period({col}, {_sql_string_literal(granularity)}, {_sql_string_literal(tz_value)})"


def _period_start_expr(col: str, granularity: str, tz_name: str | None = None) -> str:
    granularity = _normalize_granularity(granularity)
    tz_value = _normalize_dashboard_tz_name(tz_name) or "local"
    return f"dashboard_period_start({col}, {_sql_string_literal(granularity)}, {_sql_string_literal(tz_value)})"


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
            SUM(estimated_llm_calls) AS estimated_llm_calls,
            SUM(moa_calls) AS moa_calls
        FROM runs WHERE {since_clause}
    """)

    llm = _one(f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE {since_clause_ts}
    """)

    tool_summary = _one(
        f"""
        SELECT COUNT(*) AS tool_calls
        FROM tool_calls tc
        LEFT JOIN runs r ON tc.session_id = r.session_id
        WHERE {_since_clause(window_hours, "r.started_at")}
        """
    )
    runs["tool_calls"] = int(tool_summary.get("tool_calls") or 0)

    top_tools = _rows(f"""
        SELECT tool_name, COUNT(*) AS calls,
               SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures,
               AVG(latency_ms) AS avg_ms
        FROM tool_calls tc
        LEFT JOIN runs r ON tc.session_id = r.session_id
        WHERE {_since_clause(window_hours, "r.started_at")}
        GROUP BY tool_name ORDER BY calls DESC LIMIT 10
    """)

    # daily cost chart data
    daily_window = _coerce_window_hours(window_hours)
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
        cutoff = _since_cutoff_iso(daily_window)
        daily_cost = _rows(
            """
            SELECT DATE(started_at) AS day,
                   ROUND(SUM(cost_usd), 4) AS cost,
                   COUNT(*) AS runs
            FROM runs
            WHERE started_at >= ?
            GROUP BY DATE(started_at)
            ORDER BY day
        """,
            (cutoff,),
        )

    return {
        "window_hours": daily_window,
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
    telemetry_rows = _rows(f"""
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
    """)
    telemetry_by_job = {row["cron_job_id"]: row for row in telemetry_rows if row.get("cron_job_id")}
    scheduler_rows = _cron_scheduler_totals(window_hours)
    merged = []
    seen = set()

    for row in scheduler_rows:
        job_id = row["cron_job_id"]
        tele = telemetry_by_job.get(job_id, {})
        merged.append(
            {
                "cron_job_id": job_id,
                "job_name": row.get("job_name") or job_id,
                "schedule_display": row.get("schedule_display") or "",
                "runs": int(row.get("runs") or 0),
                "ok_runs": int(tele.get("ok_runs") or 0),
                "failed_runs": int(tele.get("failed_runs") or 0),
                "tokens_in": int(tele.get("tokens_in") or 0),
                "tokens_out": int(tele.get("tokens_out") or 0),
                "cache_read_tokens": int(tele.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(tele.get("cache_write_tokens") or 0),
                "cost_usd": float(tele.get("cost_usd") or 0.0),
                "avg_duration_ms": tele.get("avg_duration_ms"),
                "last_run": row.get("last_run") or tele.get("last_run"),
                "last_status": row.get("last_status") or "—",
                "enabled": bool(row.get("enabled", True)),
            }
        )
        seen.add(job_id)

    for row in telemetry_rows:
        job_id = row.get("cron_job_id")
        if not job_id or job_id in seen:
            continue
        merged.append(
            {
                "cron_job_id": job_id,
                "job_name": job_id,
                "schedule_display": "",
                "runs": int(row.get("runs") or 0),
                "ok_runs": int(row.get("ok_runs") or 0),
                "failed_runs": int(row.get("failed_runs") or 0),
                "tokens_in": int(row.get("tokens_in") or 0),
                "tokens_out": int(row.get("tokens_out") or 0),
                "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
                "cost_usd": float(row.get("cost_usd") or 0.0),
                "avg_duration_ms": row.get("avg_duration_ms"),
                "last_run": row.get("last_run"),
                "last_status": "—",
                "enabled": True,
            }
        )

    return sorted(
        merged,
        key=lambda row: (
            row.get("last_run") or "",
            int(row.get("runs") or 0),
        ),
        reverse=True,
    )


def _compute_providers_rows(window_hours=24):
    since_clause_ts = _since_clause(window_hours, "ts")
    return _rows(f"""
        SELECT provider,
               COUNT(*) AS total_calls,
               SUM(CASE WHEN estimated=0 THEN 1 ELSE 0 END) AS real_calls,
               SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END) AS estimated_calls,
               SUM(CASE WHEN provider_assumed=1 THEN 1 ELSE 0 END) AS provider_assumed_calls,
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


def api_providers(window_hours=24):
    return ProvidersCache.instance().get_rows(window_hours=window_hours)


def _compute_provider_health(window_hours=24):
    requested_window = _coerce_window_hours(window_hours)
    effective_window = requested_window if requested_window > 0 else 24
    now = datetime.now(timezone.utc)
    current_start = (now - timedelta(hours=effective_window)).isoformat()
    previous_start = (now - timedelta(hours=effective_window * 2)).isoformat()

    llm_rows = _rows(
        """
        SELECT COALESCE(provider, '—') AS provider,
               SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS calls_current,
               SUM(CASE WHEN ts >= ? AND ts < ? THEN 1 ELSE 0 END) AS calls_previous,
               SUM(CASE WHEN ts >= ? AND estimated = 1 THEN 1 ELSE 0 END) AS estimated_current,
               ROUND(AVG(CASE WHEN ts >= ? THEN latency_ms END), 1) AS avg_latency_current,
               ROUND(AVG(CASE WHEN ts >= ? AND ts < ? THEN latency_ms END), 1) AS avg_latency_previous,
               ROUND(COALESCE(SUM(CASE WHEN ts >= ? THEN cost_usd ELSE 0 END), 0), 6) AS cost_current
        FROM llm_calls
        WHERE ts >= ?
        GROUP BY COALESCE(provider, '—')
        ORDER BY calls_current DESC, cost_current DESC
        """,
        (
            current_start,
            previous_start,
            current_start,
            current_start,
            current_start,
            previous_start,
            current_start,
            current_start,
            previous_start,
        ),
    )
    run_rows = _rows(
        """
        SELECT COALESCE(provider, '—') AS provider,
               SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS runs_current,
               SUM(CASE WHEN started_at >= ? THEN CASE WHEN status NOT IN ('ok', 'running') THEN 1 ELSE 0 END ELSE 0 END) AS failed_runs_current
        FROM runs
        WHERE started_at >= ?
        GROUP BY COALESCE(provider, '—')
        """,
        (current_start, current_start, previous_start),
    )
    run_map = {row["provider"]: row for row in run_rows}

    rows = []
    for row in llm_rows:
        provider = row["provider"] or "—"
        calls_current = int(row.get("calls_current") or 0)
        calls_previous = int(row.get("calls_previous") or 0)
        est_current = int(row.get("estimated_current") or 0)
        latency_current = float(row.get("avg_latency_current") or 0)
        latency_previous = float(row.get("avg_latency_previous") or 0)
        run_meta = run_map.get(provider, {})
        failed_runs_current = int(run_meta.get("failed_runs_current") or 0)
        runs_current = int(run_meta.get("runs_current") or 0)
        estimated_pct = round((est_current / calls_current) * 100, 2) if calls_current else 0.0
        failure_pct = round((failed_runs_current / runs_current) * 100, 2) if runs_current else 0.0
        traffic_delta_pct = (
            0.0
            if calls_previous == 0
            else round(((calls_current - calls_previous) / calls_previous) * 100, 2)
        )
        latency_delta_pct = (
            0.0
            if latency_previous == 0
            else round(((latency_current - latency_previous) / latency_previous) * 100, 2)
        )
        anomalies = []
        health = "ok"
        if estimated_pct >= 50:
            anomalies.append("estimated-heavy")
            health = "warn"
        if failure_pct >= 20:
            anomalies.append("run-failures")
            health = "error"
        elif latency_delta_pct >= 50 and calls_current >= 5:
            anomalies.append("latency-spike")
            if health == "ok":
                health = "warn"
        if traffic_delta_pct >= 150 and calls_current >= 10:
            anomalies.append("traffic-spike")
            if health == "ok":
                health = "warn"
        if calls_previous >= 10 and calls_current == 0:
            anomalies.append("traffic-drop")
            if health == "ok":
                health = "warn"
        rows.append(
            {
                "provider": provider,
                "calls_current": calls_current,
                "calls_previous": calls_previous,
                "estimated_pct": estimated_pct,
                "avg_latency_current": latency_current,
                "avg_latency_previous": latency_previous,
                "latency_delta_pct": latency_delta_pct,
                "cost_current": row.get("cost_current") or 0,
                "runs_current": runs_current,
                "failed_runs_current": failed_runs_current,
                "failure_pct": failure_pct,
                "traffic_delta_pct": traffic_delta_pct,
                "health": health,
                "anomalies": anomalies,
            }
        )
    rows.sort(
        key=lambda r: (
            {"error": 0, "warn": 1, "ok": 2}[r["health"]],
            -r["calls_current"],
            -r["cost_current"],
        )
    )
    return {"window_hours": effective_window, "rows": rows}


def api_provider_health(window_hours=24):
    return ProviderHealthCache.instance().get_rows(window_hours=window_hours)


def _build_run_filters(
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    tool_name: str | None = None,
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
    if tool_name:
        clauses.append(
            "EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.session_id = runs.session_id AND tc.tool_name = ?)"
        )
        params.append(tool_name)
    if q:
        like = f"%{q}%"
        clauses.append(
            "("
            "session_id LIKE ? OR "
            "COALESCE(model, '') LIKE ? OR "
            "COALESCE(provider, '') LIKE ? OR "
            "COALESCE(cron_job_id, '') LIKE ? OR "
            "COALESCE(platform, '') LIKE ? OR "
            "COALESCE(status, '') LIKE ? OR "
            "EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.session_id = runs.session_id AND tc.tool_name LIKE ?)"
            ")"
        )
        params.extend([like, like, like, like, like, like, like])
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
    tool_name: str | None = None,
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
        tool_name,
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
            "hours": _coerce_window_hours(window_hours),
            "day": day,
            "model": model,
            "provider": provider,
            "platform": platform,
            "status": status,
            "session_id": session_id,
            "cron_job_id": cron_job_id,
            "tool_name": tool_name,
            "q": q,
            "include_deleted": bool(include_deleted),
        },
        "total_runs": total_runs,
        "hidden_deleted_runs": max(0, raw_total_runs - total_runs),
        "rows": rows,
    }


def _build_request_filters(
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    tool_name: str | None = None,
    q: str | None = None,
):
    clauses = [_since_clause(window_hours, "lc.ts")]
    params = []
    if day:
        clauses.append("DATE(lc.ts) = ?")
        params.append(day)
    if model:
        clauses.append("COALESCE(lc.model, '—') = ?")
        params.append(model)
    if provider:
        clauses.append("COALESCE(lc.provider, '—') = ?")
        params.append(provider)
    if platform:
        clauses.append("COALESCE(r.platform, 'cli') = ?")
        params.append(platform)
    if status:
        clauses.append("COALESCE(r.status, 'running') = ?")
        params.append(status)
    if session_id:
        clauses.append("lc.session_id = ?")
        params.append(session_id)
    if cron_job_id:
        clauses.append("COALESCE(r.cron_job_id, '') = ?")
        params.append(cron_job_id)
    if tool_name:
        clauses.append(
            "EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.session_id = lc.session_id AND tc.tool_name = ?)"
        )
        params.append(tool_name)
    if q:
        like = f"%{q}%"
        clauses.append(
            "("
            "CAST(lc.id AS TEXT) LIKE ? OR "
            "lc.session_id LIKE ? OR "
            "COALESCE(lc.model, '') LIKE ? OR "
            "COALESCE(lc.provider, '') LIKE ? OR "
            "COALESCE(r.cron_job_id, '') LIKE ? OR "
            "COALESCE(r.platform, '') LIKE ? OR "
            "COALESCE(r.status, '') LIKE ? OR "
            "EXISTS (SELECT 1 FROM tool_calls tc WHERE tc.session_id = lc.session_id AND tc.tool_name LIKE ?)"
            ")"
        )
        params.extend([like, like, like, like, like, like, like, like])
    return " AND ".join(clauses), params


def api_requests(
    limit=100,
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    tool_name: str | None = None,
    q: str | None = None,
    include_deleted=False,
):
    base_where_sql, base_params = _build_request_filters(
        window_hours,
        day,
        model,
        provider,
        platform,
        status,
        session_id,
        cron_job_id,
        tool_name,
        q,
    )
    visible_sql, visible_params = _visible_sessions_clause("r.session_id", include_deleted)
    where_sql = f"{base_where_sql} AND {visible_sql}"
    params = [*base_params, *visible_params]
    rows = _rows(
        f"""
        SELECT lc.id, lc.ts, lc.session_id, lc.model, lc.provider,
               lc.tokens_in, lc.tokens_out, lc.cache_read_tokens, lc.cache_write_tokens,
               lc.reasoning_tokens, lc.cost_usd, lc.latency_ms, lc.estimated,
               lc.provider_assumed,
               r.platform, r.cron_job_id, r.status, r.tool_calls
        FROM llm_calls lc
        LEFT JOIN runs r ON r.session_id = lc.session_id
        WHERE {where_sql}
        ORDER BY lc.ts DESC, lc.id DESC
        LIMIT ?
        """,
        (*params, int(limit)),
    )
    total_row = _one(
        f"SELECT COUNT(*) AS total_requests FROM llm_calls lc LEFT JOIN runs r ON r.session_id = lc.session_id WHERE {where_sql}",
        tuple(params),
    )
    raw_total_row = _one(
        f"SELECT COUNT(*) AS total_requests FROM llm_calls lc LEFT JOIN runs r ON r.session_id = lc.session_id WHERE {base_where_sql}",
        tuple(base_params),
    )
    total_requests = int(total_row.get("total_requests") or 0)
    raw_total_requests = int(raw_total_row.get("total_requests") or 0)
    return {
        "filters": {
            "hours": _coerce_window_hours(window_hours),
            "day": day,
            "model": model,
            "provider": provider,
            "platform": platform,
            "status": status,
            "session_id": session_id,
            "cron_job_id": cron_job_id,
            "tool_name": tool_name,
            "q": q,
            "include_deleted": bool(include_deleted),
        },
        "total_requests": total_requests,
        "hidden_deleted_requests": max(0, raw_total_requests - total_requests),
        "rows": rows,
    }


def api_request_detail(request_id: int):
    if not request_id:
        return {"error": "request id is required"}

    request = _one(
        """
        SELECT lc.id, lc.ts, lc.session_id, lc.model, lc.provider,
               lc.tokens_in, lc.tokens_out, lc.cache_read_tokens, lc.cache_write_tokens,
               lc.reasoning_tokens, lc.cost_usd, lc.latency_ms, lc.estimated,
               lc.provider_assumed,
               r.platform, r.cron_job_id, r.status, r.started_at, r.ended_at,
               r.duration_ms, r.api_calls, r.tool_calls, r.cost_usd AS session_cost_usd
        FROM llm_calls lc
        LEFT JOIN runs r ON r.session_id = lc.session_id
        WHERE lc.id = ?
        """,
        (int(request_id),),
    )
    if not request:
        return {"error": "request not found", "request_id": request_id}

    session_totals = _one(
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
        (request["session_id"],),
    )

    sibling_requests = _rows(
        """
        SELECT id, ts, model, provider, tokens_in, tokens_out,
               cache_read_tokens, reasoning_tokens, cost_usd, latency_ms, estimated
        FROM llm_calls
        WHERE session_id = ?
        ORDER BY ts DESC, id DESC
        LIMIT 20
        """,
        (request["session_id"],),
    )

    session_tools = _rows(
        """
        SELECT ts, tool_name, ok, latency_ms
        FROM tool_calls
        WHERE session_id = ?
        ORDER BY ts DESC, id DESC
        LIMIT 20
        """,
        (request["session_id"],),
    )

    return {
        "request": request,
        "session_totals": session_totals,
        "sibling_requests": sibling_requests,
        "session_tools": session_tools,
    }


def api_tool_analytics(
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    tool_name: str | None = None,
    q: str | None = None,
    include_deleted=False,
):
    clauses = [_since_clause(window_hours, "tc.ts")]
    params = []
    if day:
        clauses.append("DATE(tc.ts) = ?")
        params.append(day)
    if model:
        clauses.append("COALESCE(r.model, '—') = ?")
        params.append(model)
    if provider:
        clauses.append("COALESCE(r.provider, '—') = ?")
        params.append(provider)
    if platform:
        clauses.append("COALESCE(r.platform, 'cli') = ?")
        params.append(platform)
    if status:
        clauses.append("COALESCE(r.status, 'running') = ?")
        params.append(status)
    if session_id:
        clauses.append("tc.session_id = ?")
        params.append(session_id)
    if cron_job_id:
        clauses.append("COALESCE(r.cron_job_id, '') = ?")
        params.append(cron_job_id)
    if tool_name:
        clauses.append("tc.tool_name = ?")
        params.append(tool_name)
    if q:
        like = f"%{q}%"
        clauses.append(
            "("
            "tc.tool_name LIKE ? OR "
            "tc.session_id LIKE ? OR "
            "COALESCE(r.model, '') LIKE ? OR "
            "COALESCE(r.provider, '') LIKE ? OR "
            "COALESCE(r.cron_job_id, '') LIKE ? OR "
            "COALESCE(r.platform, '') LIKE ? OR "
            "COALESCE(r.status, '') LIKE ?"
            ")"
        )
        params.extend([like, like, like, like, like, like, like])

    visible_sql, visible_params = _visible_sessions_clause("r.session_id", include_deleted)
    where_sql = f"{' AND '.join(clauses)} AND {visible_sql}"
    params.extend(visible_params)

    overall = _one(
        f"""
        SELECT COUNT(*) AS tool_calls,
               COUNT(DISTINCT tc.tool_name) AS unique_tools,
               COUNT(DISTINCT tc.session_id) AS sessions,
               SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
               AVG(tc.latency_ms) AS avg_latency_ms
        FROM tool_calls tc
        LEFT JOIN runs r ON r.session_id = tc.session_id
        WHERE {where_sql}
        """,
        tuple(params),
    )
    by_tool = _rows(
        f"""
        SELECT tc.tool_name,
               COUNT(*) AS calls,
               COUNT(DISTINCT tc.session_id) AS sessions,
               SUM(CASE WHEN tc.ok = 1 THEN 1 ELSE 0 END) AS ok_calls,
               SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
               ROUND(AVG(tc.latency_ms), 1) AS avg_latency_ms,
               MAX(tc.latency_ms) AS max_latency_ms,
               MAX(tc.ts) AS last_seen
        FROM tool_calls tc
        LEFT JOIN runs r ON r.session_id = tc.session_id
        WHERE {where_sql}
        GROUP BY tc.tool_name
        ORDER BY failed_calls DESC, calls DESC, avg_latency_ms DESC
        LIMIT 50
        """,
        tuple(params),
    )
    for row in by_tool:
        calls = int(row.get("calls") or 0)
        failed = int(row.get("failed_calls") or 0)
        row["failure_pct"] = round((failed / calls) * 100, 2) if calls else 0.0
    return {"overall": overall, "by_tool": by_tool}


def api_error_center(
    window_hours=0,
    day: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    cron_job_id: str | None = None,
    tool_name: str | None = None,
    q: str | None = None,
    include_deleted=False,
):
    run_where_sql, run_params = _build_run_filters(
        window_hours,
        day,
        model,
        provider,
        platform,
        status,
        session_id,
        cron_job_id,
        tool_name,
        q,
    )
    visible_sql, visible_params = _visible_sessions_clause("runs.session_id", include_deleted)
    run_where_sql = f"{run_where_sql} AND {visible_sql}"
    run_params = [*run_params, *visible_params]

    tool_clauses = [_since_clause(window_hours, "tc.ts")]
    tool_params = []
    if day:
        tool_clauses.append("DATE(tc.ts) = ?")
        tool_params.append(day)
    if model:
        tool_clauses.append("COALESCE(r.model, '—') = ?")
        tool_params.append(model)
    if provider:
        tool_clauses.append("COALESCE(r.provider, '—') = ?")
        tool_params.append(provider)
    if platform:
        tool_clauses.append("COALESCE(r.platform, 'cli') = ?")
        tool_params.append(platform)
    if status:
        tool_clauses.append("COALESCE(r.status, 'running') = ?")
        tool_params.append(status)
    if session_id:
        tool_clauses.append("tc.session_id = ?")
        tool_params.append(session_id)
    if cron_job_id:
        tool_clauses.append("COALESCE(r.cron_job_id, '') = ?")
        tool_params.append(cron_job_id)
    if tool_name:
        tool_clauses.append("tc.tool_name = ?")
        tool_params.append(tool_name)
    if q:
        like = f"%{q}%"
        tool_clauses.append(
            "("
            "tc.tool_name LIKE ? OR "
            "tc.session_id LIKE ? OR "
            "COALESCE(r.model, '') LIKE ? OR "
            "COALESCE(r.provider, '') LIKE ? OR "
            "COALESCE(r.cron_job_id, '') LIKE ? OR "
            "COALESCE(r.platform, '') LIKE ? OR "
            "COALESCE(r.status, '') LIKE ?"
            ")"
        )
        tool_params.extend([like, like, like, like, like, like, like])
    tool_visible_sql, tool_visible_params = _visible_sessions_clause(
        "r.session_id", include_deleted
    )
    tool_where_sql = f"{' AND '.join(tool_clauses)} AND {tool_visible_sql}"
    tool_params.extend(tool_visible_params)

    summary = {
        "runs": _one(
            f"""
            SELECT SUM(CASE WHEN status NOT IN ('ok', 'running') THEN 1 ELSE 0 END) AS failed_runs,
                   SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted_runs,
                   SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) AS timeout_runs,
                   COUNT(*) AS total_runs
            FROM runs
            WHERE {run_where_sql}
            """,
            tuple(run_params),
        ),
        "tools": _one(
            f"""
            SELECT SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END) AS failed_tool_calls,
                   COUNT(DISTINCT CASE WHEN tc.ok = 0 THEN tc.session_id END) AS sessions_with_failed_tools,
                   COUNT(*) AS tool_calls
            FROM tool_calls tc
            LEFT JOIN runs r ON r.session_id = tc.session_id
            WHERE {tool_where_sql}
            """,
            tuple(tool_params),
        ),
    }

    status_groups = _rows(
        f"""
        SELECT status,
               COUNT(*) AS runs,
               COUNT(DISTINCT session_id) AS sessions,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               MAX(started_at) AS last_seen
        FROM runs
        WHERE {run_where_sql}
          AND status NOT IN ('ok', 'running')
        GROUP BY status
        ORDER BY runs DESC, last_seen DESC
        """,
        tuple(run_params),
    )

    failed_tools = _rows(
        f"""
        SELECT tc.tool_name,
               COUNT(*) AS failed_calls,
               COUNT(DISTINCT tc.session_id) AS sessions,
               ROUND(AVG(tc.latency_ms), 1) AS avg_latency_ms,
               MAX(tc.ts) AS last_seen
        FROM tool_calls tc
        LEFT JOIN runs r ON r.session_id = tc.session_id
        WHERE {tool_where_sql}
          AND tc.ok = 0
        GROUP BY tc.tool_name
        ORDER BY failed_calls DESC, last_seen DESC
        LIMIT 20
        """,
        tuple(tool_params),
    )

    incidents = _rows(
        f"""
        SELECT *
        FROM (
            SELECT started_at AS ts,
                   'run_status' AS kind,
                   session_id,
                   platform,
                   provider,
                   model,
                   status,
                   '' AS tool_name,
                   duration_ms AS latency_ms,
                   cost_usd
            FROM runs
            WHERE {run_where_sql}
              AND status NOT IN ('ok', 'running')
            UNION ALL
            SELECT tc.ts AS ts,
                   'tool_failure' AS kind,
                   tc.session_id,
                   r.platform,
                   r.provider,
                   r.model,
                   r.status,
                   tc.tool_name,
                   tc.latency_ms,
                   r.cost_usd
            FROM tool_calls tc
            LEFT JOIN runs r ON r.session_id = tc.session_id
            WHERE {tool_where_sql}
              AND tc.ok = 0
        ) incidents
        ORDER BY ts DESC
        LIMIT 50
        """,
        tuple(run_params + tool_params),
    )

    return {
        "summary": summary,
        "status_groups": status_groups,
        "failed_tools": failed_tools,
        "incidents": incidents,
    }


def _model_efficiency_cache_key(window_hours, include_deleted, limit):
    return (int(_coerce_window_hours(window_hours)), int(bool(include_deleted)), int(limit))


def _endpoint_payload_cache_key(*parts):
    return json.dumps(parts, separators=(",", ":"), default=str)


def _telemetry_db_module():
    import hermes_telemetry.db as telemetry_db

    return telemetry_db


def _ensure_dashboard_cache_schema() -> None:
    _telemetry_db_module()._get_conn()
    _telemetry_db_module().close_thread_conn()
    return None


def _payload_rows_count(payload) -> int:
    """Count logical rows for cache metadata.

    List payloads count elements. Dict payloads prefer an embedded ``rows``
    list (e.g. provider-health); otherwise they contribute 0 so we don't
    mistake object-key count for row count.
    """
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return len(rows)
        return 0
    return 0


def _parse_cache_built_at(built_at):
    if not built_at:
        return None
    try:
        dt = datetime.fromisoformat(str(built_at))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class _BackgroundPayloadCache:
    """SQLite-backed payload cache with background priming + TTL freshness.

    ``get_rows`` is a pure presence check no longer: a hit is returned only
    while ``built_at`` is within ``DEFAULT_REFRESH_SECONDS``. Stale hits are
    served immediately and an async recompute is kicked for that exact key
    (so UI presets that are not in ``DEFAULT_KEYS`` still refresh when used).
    Misses still compute synchronously and seed the cache.

    Shared-connection safety: every read/write on ``self._conn`` is guarded by
    ``_cond``. The connection uses ``check_same_thread=False`` so request
    threads and the background refresher can share it; the condition is the
    serialization that makes that safe. WAL + ``busy_timeout`` only cover
    *other* connections (plugin writers), not concurrent use of this one.
    """

    _instance = None
    _lock = threading.Lock()
    CACHE_NAME = "base"
    DEFAULT_REFRESH_SECONDS = 300
    DEFAULT_KEYS = ()
    MAX_ENTRIES = 64
    # Drop entries older than this even if under MAX_ENTRIES.
    MAX_ENTRY_AGE_SECONDS = 6 * 3600

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_dashboard_cache_schema()
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        # Serialize all use of self._conn across request + background threads.
        self._cond = threading.Condition(threading.Lock())
        self._stop = threading.Event()
        self._refreshing = False
        self._inflight_keys = set()
        self._last_refresh_at = None
        self._last_refresh_seconds = 0.0
        self._thread = threading.Thread(
            target=self._run, name=f"{self.CACHE_NAME}-cache", daemon=True
        )

    @classmethod
    def instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(DB_PATH)
                cls._instance.start()
            return cls._instance

    @classmethod
    def reset_for_tests(cls):
        with cls._lock:
            if cls._instance is not None:
                cls._instance.stop()
            cls._instance = None

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def _run(self):
        try:
            self._refresh_all()
        except Exception:
            logger.exception("initial %s cache refresh failed", self.CACHE_NAME)
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait(timeout=self.DEFAULT_REFRESH_SECONDS)
            if self._stop.is_set():
                break
            try:
                self._refresh_all()
            except Exception:
                logger.exception("scheduled %s cache refresh failed", self.CACHE_NAME)

    def _is_fresh(self, built_at) -> bool:
        dt = _parse_cache_built_at(built_at)
        if dt is None:
            return False
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age <= float(self.DEFAULT_REFRESH_SECONDS)

    def _cache_row(self, key):
        with self._cond:
            row = self._conn.execute(
                "SELECT payload_json, rows_count, built_at FROM endpoint_payload_cache WHERE cache_name = ? AND cache_key = ?",
                (self.CACHE_NAME, key),
            ).fetchone()
            if not row:
                return None
            return {
                "payload": json.loads(row["payload_json"]),
                "rows_count": row["rows_count"],
                "built_at": row["built_at"],
            }

    def _evict_locked(self):
        """Bound growth: drop expired entries, then keep the newest MAX_ENTRIES."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.MAX_ENTRY_AGE_SECONDS)
        ).isoformat()
        self._conn.execute(
            "DELETE FROM endpoint_payload_cache WHERE cache_name = ? AND built_at < ?",
            (self.CACHE_NAME, cutoff),
        )
        rows = self._conn.execute(
            """
            SELECT cache_key FROM endpoint_payload_cache
            WHERE cache_name = ?
            ORDER BY built_at DESC
            """,
            (self.CACHE_NAME,),
        ).fetchall()
        if len(rows) <= self.MAX_ENTRIES:
            return
        for row in rows[self.MAX_ENTRIES :]:
            self._conn.execute(
                "DELETE FROM endpoint_payload_cache WHERE cache_name = ? AND cache_key = ?",
                (self.CACHE_NAME, row["cache_key"]),
            )

    def _write_cache(self, key, payload, **_meta):
        with self._cond:
            self._conn.execute(
                """
                INSERT INTO endpoint_payload_cache (cache_name, cache_key, payload_json, rows_count, built_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_name, cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    rows_count = excluded.rows_count,
                    built_at = excluded.built_at
                """,
                (
                    self.CACHE_NAME,
                    key,
                    json.dumps(payload, default=str),
                    _payload_rows_count(payload),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._evict_locked()

    def get_rows(self, **kwargs):
        key = self.cache_key(**kwargs)
        cached = self._cache_row(key)
        if cached is not None and self._is_fresh(cached["built_at"]):
            return cached["payload"]
        if cached is not None:
            # Stale but present: never freeze a dynamic UI window forever.
            self._kick_async_refresh(kwargs)
            return cached["payload"]
        rows = self.compute_rows(**kwargs)
        try:
            self._write_cache(key, rows, **kwargs)
        except Exception:
            logger.exception("failed to seed %s cache", self.CACHE_NAME)
        return rows

    def _kick_async_refresh(self, kwargs):
        key = self.cache_key(**kwargs)
        with self._cond:
            if key in self._inflight_keys:
                return
            self._inflight_keys.add(key)

        def _worker():
            try:
                self.refresh(**kwargs)
            except Exception:
                logger.exception("async stale refresh failed for %s key=%s", self.CACHE_NAME, key)
            finally:
                with self._cond:
                    self._inflight_keys.discard(key)

        threading.Thread(
            target=_worker, name=f"{self.CACHE_NAME}-stale-refresh", daemon=True
        ).start()

    def refresh(self, **kwargs):
        rows = self.compute_rows(**kwargs)
        self._write_cache(self.cache_key(**kwargs), rows, **kwargs)
        return rows

    def _refresh_all(self):
        if self._refreshing:
            return {"skipped": "already refreshing"}
        self._refreshing = True
        started = time.monotonic()
        try:
            for kwargs in self.default_refresh_kwargs():
                try:
                    self.refresh(**kwargs)
                except Exception:
                    logger.exception("%s refresh failed for %s", self.CACHE_NAME, kwargs)
            # Sweep leftovers from drifting calendar keys / one-off windows.
            with self._cond:
                self._evict_locked()
            self._last_refresh_seconds = time.monotonic() - started
            self._last_refresh_at = datetime.now(timezone.utc).isoformat()
            return {
                "refreshed_at": self._last_refresh_at,
                "elapsed_seconds": round(self._last_refresh_seconds, 3),
            }
        finally:
            self._refreshing = False

    def cache_key(self, **kwargs):
        raise NotImplementedError

    def compute_rows(self, **kwargs):
        raise NotImplementedError

    def default_refresh_kwargs(self):
        return [dict(item) for item in self.DEFAULT_KEYS]


class ProvidersCache(_BackgroundPayloadCache):
    CACHE_NAME = "providers"
    DEFAULT_KEYS = (
        {"window_hours": 24},
        {"window_hours": 168},
        {"window_hours": 720},
        {"window_hours": 0},
    )

    def cache_key(self, **kwargs):
        return _endpoint_payload_cache_key(
            int(_coerce_window_hours(kwargs.get("window_hours", 24)))
        )

    def compute_rows(self, **kwargs):
        return _compute_providers_rows(kwargs.get("window_hours", 24))


class ProviderHealthCache(_BackgroundPayloadCache):
    CACHE_NAME = "provider-health"
    DEFAULT_KEYS = (
        {"window_hours": 24},
        {"window_hours": 168},
        {"window_hours": 720},
        {"window_hours": 0},
    )

    def cache_key(self, **kwargs):
        return _endpoint_payload_cache_key(
            int(_coerce_window_hours(kwargs.get("window_hours", 24)))
        )

    def compute_rows(self, **kwargs):
        return _compute_provider_health(kwargs.get("window_hours", 24))


class DailyTokenChartCache(_BackgroundPayloadCache):
    CACHE_NAME = "daily-token-chart"
    DEFAULT_KEYS = (
        {
            "window_hours": 24,
            "limit_days": 26,
            "include_deleted": True,
            "granularity": "hour",
            "tz_name": None,
        },
        {
            "window_hours": 168,
            "limit_days": 14,
            "include_deleted": True,
            "granularity": "day",
            "tz_name": None,
        },
        {
            "window_hours": 720,
            "limit_days": 32,
            "include_deleted": True,
            "granularity": "day",
            "tz_name": None,
        },
        {
            "window_hours": 0,
            "limit_days": 3650,
            "include_deleted": True,
            "granularity": "day",
            "tz_name": None,
        },
    )

    def cache_key(self, **kwargs):
        return _endpoint_payload_cache_key(
            int(_coerce_window_hours(kwargs.get("window_hours", 24))),
            max(1, min(3650, int(kwargs.get("limit_days", 90)))),
            int(bool(kwargs.get("include_deleted", False))),
            _normalize_granularity(kwargs.get("granularity", "day")),
            _normalize_dashboard_tz_name(kwargs.get("tz_name")),
        )

    def compute_rows(self, **kwargs):
        return _compute_daily_token_chart_rows(
            window_hours=kwargs.get("window_hours", 24),
            limit_days=kwargs.get("limit_days", 90),
            include_deleted=kwargs.get("include_deleted", False),
            granularity=kwargs.get("granularity", "day"),
            tz_name=kwargs.get("tz_name"),
        )


class DailyModelChartCache(_BackgroundPayloadCache):
    CACHE_NAME = "daily-model-chart"
    DEFAULT_KEYS = (
        {
            "window_hours": 24,
            "limit_days": 26,
            "top_n": 5,
            "include_deleted": True,
            "tz_name": None,
        },
        {
            "window_hours": 168,
            "limit_days": 14,
            "top_n": 5,
            "include_deleted": True,
            "tz_name": None,
        },
        {
            "window_hours": 720,
            "limit_days": 32,
            "top_n": 5,
            "include_deleted": True,
            "tz_name": None,
        },
        {
            "window_hours": 0,
            "limit_days": 3650,
            "top_n": 5,
            "include_deleted": True,
            "tz_name": None,
        },
    )

    def cache_key(self, **kwargs):
        return _endpoint_payload_cache_key(
            int(_coerce_window_hours(kwargs.get("window_hours", 24))),
            max(1, min(3650, int(kwargs.get("limit_days", 90)))),
            max(1, min(8, int(kwargs.get("top_n", 5)))),
            int(bool(kwargs.get("include_deleted", False))),
            _normalize_dashboard_tz_name(kwargs.get("tz_name")),
        )

    def compute_rows(self, **kwargs):
        return _compute_daily_model_chart(
            window_hours=kwargs.get("window_hours", 24),
            limit_days=kwargs.get("limit_days", 90),
            top_n=kwargs.get("top_n", 5),
            include_deleted=kwargs.get("include_deleted", False),
            tz_name=kwargs.get("tz_name"),
        )


def api_model_efficiency(window_hours=0, limit=50, include_deleted=False):
    """Return per-model efficiency rows.

    Reads from the in-process SQLite cache when fresh; otherwise computes
    live and seeds the cache. The cache is refreshed in a background thread
    every few minutes by `ModelEfficiencyCache`, so dashboard first-paint
    never waits on the heavy `tool_calls ⨝ llm_calls` join.
    """
    return ModelEfficiencyCache.instance().get_rows(
        window_hours=window_hours, limit=limit, include_deleted=include_deleted
    )


def api_model_efficiency_cache_status():
    return ModelEfficiencyCache.instance().status()


def api_model_efficiency_refresh(window_hours=None, include_deleted=False):
    """Force an immediate refresh of the cache for the given window.

    ``window_hours=None`` refreshes the default window set. This endpoint is
    intentionally unauthenticated like the rest of the standalone dashboard;
    only expose the server on trusted networks (see ``_warn_if_exposed``).
    """
    return ModelEfficiencyCache.instance().refresh(
        window_hours=window_hours, include_deleted=include_deleted
    )


def _compute_model_efficiency_rows(window_hours, limit, include_deleted):
    """Live (uncached) computation of model efficiency.

    Kept separate so the cache layer can call it without going through itself.
    """
    limit = max(1, min(int(limit), 500))
    since_clause = _since_clause(window_hours, "lc.ts")
    visible_sql, visible_params = _visible_sessions_clause("r.session_id", include_deleted)
    rows = _rows(
        f"""
        SELECT COALESCE(lc.model, '—') AS model,
               COALESCE(lc.provider, '—') AS provider,
               COUNT(*) AS api_calls,
               COUNT(DISTINCT lc.session_id) AS sessions,
               COALESCE(SUM(lc.tokens_in), 0) AS tokens_in,
               COALESCE(SUM(lc.tokens_out), 0) AS tokens_out,
               COALESCE(SUM(lc.cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(lc.cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(lc.reasoning_tokens), 0) AS reasoning_tokens,
               ROUND(COALESCE(SUM(lc.cost_usd), 0), 6) AS cost_usd,
               ROUND(AVG(lc.latency_ms), 1) AS avg_latency_ms,
               SUM(CASE WHEN lc.estimated = 1 THEN 1 ELSE 0 END) AS estimated_calls,
               SUM(CASE WHEN COALESCE(r.status, 'running') NOT IN ('ok', 'running') THEN 1 ELSE 0 END) AS failed_run_calls,
               COUNT(DISTINCT CASE WHEN COALESCE(r.status, 'running') NOT IN ('ok', 'running') THEN lc.session_id END) AS failed_sessions,
               0 AS tool_calls
        FROM llm_calls lc
        LEFT JOIN runs r ON r.session_id = lc.session_id
        WHERE {since_clause} AND {visible_sql}
        GROUP BY COALESCE(lc.model, '—'), COALESCE(lc.provider, '—')
        ORDER BY (COALESCE(SUM(lc.tokens_in), 0) + COALESCE(SUM(lc.tokens_out), 0) + COALESCE(SUM(lc.cache_read_tokens), 0) + COALESCE(SUM(lc.cache_write_tokens), 0) + COALESCE(SUM(lc.reasoning_tokens), 0)) DESC
        LIMIT ?
        """,
        (*visible_params, limit),
    )
    if rows:
        session_models = _rows(
            f"""
            SELECT DISTINCT tc.session_id, lc.model, lc.provider
            FROM tool_calls tc
            JOIN llm_calls lc ON lc.session_id = tc.session_id
            WHERE lc.ts >= {since_clause}
            """,
            visible_params,
        )
        tool_count: dict[tuple, int] = {}
        for sm in session_models:
            key = (sm["model"] or "—", sm["provider"] or "—")
            tool_count[key] = tool_count.get(key, 0) + 1
        for row in rows:
            key = (row["model"], row["provider"])
            row["tool_calls"] = tool_count.get(key, 0)
    for row in rows:
        api_calls = int(row.get("api_calls") or 0)
        tokens_in = int(row.get("tokens_in") or 0)
        tokens_out = int(row.get("tokens_out") or 0)
        cache_read = int(row.get("cache_read_tokens") or 0)
        cache_write = int(row.get("cache_write_tokens") or 0)
        reasoning = int(row.get("reasoning_tokens") or 0)
        total_tokens = tokens_in + tokens_out + cache_read + cache_write + reasoning
        cost = float(row.get("cost_usd") or 0.0)
        tool_calls = int(row.get("tool_calls") or 0)
        estimated = int(row.get("estimated_calls") or 0)
        failed_sessions = int(row.get("failed_sessions") or 0)
        sessions = int(row.get("sessions") or 0)
        row["total_tokens"] = total_tokens
        row["cost_per_1m"] = round((cost / total_tokens) * 1_000_000, 6) if total_tokens else 0.0
        row["output_input_ratio"] = round(tokens_out / tokens_in, 4) if tokens_in else 0.0
        row["cache_hit_share_pct"] = (
            round((cache_read / (tokens_in + cache_read)) * 100, 2)
            if (tokens_in + cache_read)
            else 0.0
        )
        row["tool_calls_per_api"] = round(tool_calls / api_calls, 2) if api_calls else 0.0
        row["estimated_pct"] = round((estimated / api_calls) * 100, 2) if api_calls else 0.0
        row["failure_pct"] = round((failed_sessions / sessions) * 100, 2) if sessions else 0.0
        score = 100
        score -= min(35, row["failure_pct"] * 2)
        score -= min(25, max(0, (float(row.get("avg_latency_ms") or 0) - 5000) / 500))
        score -= min(20, row["estimated_pct"] / 5)
        score += min(10, row["cache_hit_share_pct"] / 10)
        row["efficiency_score"] = round(max(0, min(100, score)), 1)
    return rows


def api_tool_failure_heatmap(window_hours=0, limit=80, include_deleted=False):
    limit = max(1, min(int(limit), 500))
    since_clause = _since_clause(window_hours, "tc.ts")
    visible_sql, visible_params = _visible_sessions_clause("r.session_id", include_deleted)
    rows = _rows(
        f"""
        SELECT tc.tool_name,
               COALESCE(r.model, '—') AS model,
               COALESCE(r.platform, 'cli') AS platform,
               COALESCE(r.cron_job_id, '') AS cron_job_id,
               COUNT(*) AS calls,
               SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END) AS failed_calls,
               COUNT(DISTINCT tc.session_id) AS sessions,
               ROUND(AVG(tc.latency_ms), 1) AS avg_latency_ms,
               MAX(tc.ts) AS last_seen
        FROM tool_calls tc
        LEFT JOIN runs r ON r.session_id = tc.session_id
        WHERE {since_clause} AND {visible_sql}
        GROUP BY tc.tool_name, COALESCE(r.model, '—'), COALESCE(r.platform, 'cli'), COALESCE(r.cron_job_id, '')
        HAVING calls > 0
        ORDER BY failed_calls DESC, calls DESC, avg_latency_ms DESC
        LIMIT ?
        """,
        (*visible_params, limit),
    )
    for row in rows:
        calls = int(row.get("calls") or 0)
        failed = int(row.get("failed_calls") or 0)
        row["failure_pct"] = round((failed / calls) * 100, 2) if calls else 0.0
    return rows


def api_cron_failure_waste(window_hours=0, limit=50, include_deleted=False):
    limit = max(1, min(int(limit), 500))
    run_where, run_params = _build_run_filters(window_hours, platform="cron")
    visible_sql, visible_params = _visible_sessions_clause("runs.session_id", include_deleted)
    where_sql = f"{run_where} AND {visible_sql} AND COALESCE(runs.cron_job_id, '') != ''"
    params = [*run_params, *visible_params]
    sub_run_where = run_where.replace("started_at", "r2.started_at")
    sub_visible_sql = visible_sql.replace("runs.session_id", "r2.session_id")
    rows = _rows(
        f"""
        SELECT runs.cron_job_id,
               COUNT(*) AS runs,
               SUM(CASE WHEN runs.status = 'ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN COALESCE(runs.status, 'running') NOT IN ('ok', 'running') THEN 1 ELSE 0 END) AS failed_runs,
               SUM(CASE WHEN runs.status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted_runs,
               SUM(CASE WHEN runs.status = 'timeout' THEN 1 ELSE 0 END) AS timeout_runs,
               SUM(CASE WHEN COALESCE(runs.status, 'running') = 'running' THEN 1 ELSE 0 END) AS running_runs,
               COALESCE(SUM(runs.tokens_in), 0) AS tokens_in,
               COALESCE(SUM(runs.tokens_out), 0) AS tokens_out,
               COALESCE(SUM(runs.cache_read_tokens), 0) AS cache_read_tokens,
               ROUND(COALESCE(SUM(runs.cost_usd), 0), 6) AS cost_usd,
               ROUND(COALESCE(AVG(runs.duration_ms), 0), 1) AS avg_duration_ms,
               MAX(runs.started_at) AS last_run,
               MAX(CASE WHEN runs.status = 'ok' THEN runs.started_at ELSE NULL END) AS last_success,
               COALESCE((
                   SELECT COUNT(*)
                   FROM tool_calls tc
                   LEFT JOIN runs r2 ON r2.session_id = tc.session_id
                   WHERE r2.cron_job_id = runs.cron_job_id
                     AND tc.ok = 0
                     AND {sub_run_where}
                     AND {sub_visible_sql}
               ), 0) AS failed_tool_calls
        FROM runs
        WHERE {where_sql}
        GROUP BY runs.cron_job_id
        ORDER BY failed_runs DESC, failed_tool_calls DESC, cost_usd DESC, runs DESC
        LIMIT ?
        """,
        (*run_params, *visible_params, *params, limit),
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        runs = int(row.get("runs") or 0)
        failed = int(row.get("failed_runs") or 0)
        row["failure_pct"] = round((failed / runs) * 100, 2) if runs else 0.0
        wasted_tokens = 0
        if failed:
            wasted = _one(
                f"""
                SELECT COALESCE(SUM(tokens_in + tokens_out + COALESCE(cache_read_tokens, 0)), 0) AS wasted_tokens,
                       ROUND(COALESCE(SUM(cost_usd), 0), 6) AS wasted_cost
                FROM runs
                WHERE cron_job_id = ? AND COALESCE(status, 'running') NOT IN ('ok', 'running') AND {run_where}
                """,
                (row["cron_job_id"], *run_params),
            )
            wasted_tokens = int(wasted.get("wasted_tokens") or 0)
            row["wasted_cost_usd"] = wasted.get("wasted_cost") or 0
        else:
            row["wasted_cost_usd"] = 0
        row["wasted_tokens"] = wasted_tokens
        last_success = row.get("last_success")
        if last_success:
            try:
                last_dt = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                row["last_success_age_hours"] = round((now - last_dt).total_seconds() / 3600, 1)
            except ValueError:
                row["last_success_age_hours"] = None
        else:
            row["last_success_age_hours"] = None
        risks = []
        if row["timeout_runs"]:
            risks.append("timeout")
        if row["failure_pct"] >= 20:
            risks.append("failure-rate")
        if row["failed_tool_calls"]:
            risks.append("tool-failures")
        if row["last_success_age_hours"] is None:
            risks.append("never-success")
        elif row["last_success_age_hours"] > 48:
            risks.append("stale-success")
        row["risks"] = risks
    return rows


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


def api_daily_tokens(window_hours=24, page=1, per_page=15, tz_name: str | None = None):
    since_clause_ts = _since_clause(window_hours, "ts")
    period_expr = _period_label_expr("ts", "day", tz_name)
    page = max(1, int(page))
    per_page = max(1, min(15, int(per_page)))

    total_days_row = _one(
        f"""
        SELECT COUNT(*) AS total_days
        FROM (
            SELECT {period_expr} AS day
            FROM llm_calls
            WHERE {since_clause_ts}
            GROUP BY {period_expr}
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
            {period_expr} AS day,
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
        GROUP BY {period_expr}
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


def _compute_daily_token_chart_rows(
    window_hours=24,
    limit_days=90,
    include_deleted=False,
    granularity="day",
    tz_name: str | None = None,
):
    since_clause_ts = _since_clause(window_hours, "lc.ts")
    since_clause_runs = _since_clause(window_hours, "started_at")
    since_epoch = _since_cutoff_epoch(window_hours)
    granularity = _normalize_granularity(granularity)
    llm_period_expr = _period_label_expr("lc.ts", granularity, tz_name)
    runs_period_expr = _period_label_expr("started_at", granularity, tz_name)
    state_period_expr = _period_label_expr("datetime(timestamp, 'unixepoch')", granularity, tz_name)
    limit_days = max(1, min(3650, int(limit_days)))
    visible_llm_sql, visible_llm_params = _visible_sessions_clause(
        "r.session_id", include_deleted=include_deleted
    )
    visible_runs_sql, visible_runs_params = _visible_sessions_clause(
        "session_id", include_deleted=include_deleted
    )

    llm_rows = _rows(
        f"""
        SELECT *
        FROM (
            SELECT
                {llm_period_expr} AS day,
                COUNT(*) AS api_calls,
                ROUND(COALESCE(SUM(lc.cost_usd), 0), 6) AS cost_usd,
                COALESCE(SUM(lc.tokens_in), 0) AS tokens_in,
                COALESCE(SUM(lc.tokens_out), 0) AS tokens_out,
                COALESCE(SUM(lc.cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(lc.cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(lc.reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(lc.tokens_in), 0)
                    + COALESCE(SUM(lc.tokens_out), 0)
                    + COALESCE(SUM(lc.cache_read_tokens), 0)
                    + COALESCE(SUM(lc.cache_write_tokens), 0)
                    + COALESCE(SUM(lc.reasoning_tokens), 0) AS total_tokens
            FROM llm_calls lc
            LEFT JOIN runs r ON r.session_id = lc.session_id
            WHERE {since_clause_ts} AND {visible_llm_sql}
            GROUP BY {llm_period_expr}
            ORDER BY day DESC
            LIMIT ?
        ) d
        ORDER BY day ASC
        """,
        (*visible_llm_params, limit_days),
    )
    run_rows = _rows(
        f"""
        SELECT *
        FROM (
            SELECT
                {runs_period_expr} AS day,
                COUNT(*) AS request_runs
            FROM runs
            WHERE {since_clause_runs} AND {visible_runs_sql}
            GROUP BY {runs_period_expr}
            ORDER BY day DESC
            LIMIT ?
        ) d
        ORDER BY day ASC
        """,
        (*visible_runs_params, limit_days),
    )
    tool_rows = _rows(
        f"""
        SELECT *
        FROM (
            SELECT
                {_period_label_expr("tc.ts", granularity, tz_name)} AS day,
                COUNT(*) AS tool_calls
            FROM tool_calls tc
            LEFT JOIN runs r ON r.session_id = tc.session_id
            WHERE {_since_clause(window_hours, "tc.ts")} AND {visible_llm_sql}
            GROUP BY {_period_label_expr("tc.ts", granularity, tz_name)}
            ORDER BY day DESC
            LIMIT ?
        ) d
        ORDER BY day ASC
        """,
        (*visible_llm_params, limit_days),
    )
    cron_rows = _cron_scheduler_runs(window_hours)
    cron_counts = {}
    cron_tzinfo, _ = _dashboard_viewer_tz(_normalize_dashboard_tz_name(tz_name))
    for row in cron_rows:
        ts = row["ts"].astimezone(cron_tzinfo)
        if granularity == "month":
            day = ts.strftime("%Y-%m")
        elif granularity == "week":
            monday = (ts - timedelta(days=ts.weekday())).date()
            day = monday.isoformat()
        elif granularity == "minute":
            day = ts.replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
        elif granularity == "hour":
            day = ts.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        else:
            day = ts.date().isoformat()
        cron_counts[day] = cron_counts.get(day, 0) + 1
    state_where = "role IN ('user','assistant')"
    state_params = []
    if since_epoch is not None:
        state_where += " AND timestamp >= ?"
        state_params.append(since_epoch)
    message_rows = _state_rows(
        f"""
        SELECT
            {state_period_expr} AS day,
            SUM(CASE WHEN role = 'user' AND TRIM(COALESCE(content, '')) != '' THEN 1 ELSE 0 END) AS user_messages,
            SUM(CASE WHEN role = 'assistant' AND TRIM(COALESCE(content, '')) != '' THEN 1 ELSE 0 END) AS assistant_messages,
            SUM(CASE WHEN role = 'user' AND TRIM(COALESCE(content, '')) != '' THEN 1 ELSE 0 END)
                + SUM(CASE WHEN role = 'assistant' AND TRIM(COALESCE(content, '')) != '' THEN 1 ELSE 0 END) AS message_runs
        FROM messages
        WHERE {state_where}
        GROUP BY {state_period_expr}
        ORDER BY day ASC
        """,
        tuple(state_params),
    )

    merged = {}
    for row in llm_rows:
        day = row["day"]
        merged[day] = dict(row)
    for row in run_rows:
        day = row["day"]
        target = merged.setdefault(
            day,
            {
                "day": day,
                "api_calls": 0,
                "cost_usd": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        )
        target["request_runs"] = int(row.get("request_runs") or 0)
    for row in tool_rows:
        day = row["day"]
        target = merged.setdefault(
            day,
            {
                "day": day,
                "api_calls": 0,
                "cost_usd": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        )
        target["tool_calls"] = int(row.get("tool_calls") or 0)
    for day, count in cron_counts.items():
        target = merged.setdefault(
            day,
            {
                "day": day,
                "api_calls": 0,
                "cost_usd": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        )
        target["cron_job_runs"] = int(count)
    for row in message_rows:
        day = row["day"]
        target = merged.setdefault(
            day,
            {
                "day": day,
                "api_calls": 0,
                "cost_usd": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        )
        target["message_runs"] = int(row.get("message_runs") or 0)
        target["user_messages"] = int(row.get("user_messages") or 0)
        target["assistant_messages"] = int(row.get("assistant_messages") or 0)

    rows = [merged[day] for day in sorted(merged.keys())][-limit_days:]
    for row in rows:
        row.setdefault("request_runs", 0)
        row.setdefault("tool_calls", 0)
        row.setdefault("cron_job_runs", 0)
        row.setdefault("message_runs", 0)
        row.setdefault("user_messages", 0)
        row.setdefault("assistant_messages", 0)
        billable_tokens = (
            int(row.get("tokens_in") or 0)
            + int(row.get("tokens_out") or 0)
            + int(row.get("cache_write_tokens") or 0)
            + int(row.get("reasoning_tokens") or 0)
        )
        cache_read_tokens = int(row.get("cache_read_tokens") or 0)
        cost_usd = float(row.get("cost_usd") or 0.0)
        effective_cost_per_token = (cost_usd / billable_tokens) if billable_tokens > 0 else 0.0
        estimated_savings_usd = cache_read_tokens * effective_cost_per_token
        row["estimated_savings_usd"] = round(estimated_savings_usd, 6)
        total_effective_spend = cost_usd + estimated_savings_usd
        row["savings_pct"] = (
            round((estimated_savings_usd / total_effective_spend) * 100, 2)
            if total_effective_spend > 0
            else 0.0
        )
    return rows


def api_daily_token_chart(
    window_hours=24,
    limit_days=90,
    include_deleted=False,
    granularity="day",
    tz_name: str | None = None,
):
    return DailyTokenChartCache.instance().get_rows(
        window_hours=window_hours,
        limit_days=limit_days,
        include_deleted=include_deleted,
        granularity=granularity,
        tz_name=tz_name,
    )


def _compute_daily_model_chart(
    window_hours=24, limit_days=90, top_n=5, include_deleted=False, tz_name: str | None = None
):
    since_clause_ts = _since_clause(window_hours, "ts")
    day_expr = _period_label_expr("lc.ts", "day", tz_name)
    limit_days = max(1, min(3650, int(limit_days)))
    top_n = max(1, min(8, int(top_n)))
    visible_sql, visible_params = _visible_sessions_clause(
        "r.session_id", include_deleted=include_deleted
    )

    top_models = _rows(
        f"""
        SELECT COALESCE(lc.model, '—') AS model,
               COALESCE(SUM(lc.tokens_in), 0)
                   + COALESCE(SUM(lc.tokens_out), 0)
                   + COALESCE(SUM(lc.cache_read_tokens), 0)
                   + COALESCE(SUM(lc.cache_write_tokens), 0)
                   + COALESCE(SUM(lc.reasoning_tokens), 0) AS total_tokens
        FROM llm_calls lc
        LEFT JOIN runs r ON r.session_id = lc.session_id
        WHERE {since_clause_ts} AND {visible_sql}
        GROUP BY COALESCE(lc.model, '—')
        ORDER BY total_tokens DESC
        LIMIT ?
        """,
        (*visible_params, top_n),
    )
    model_names = [r["model"] for r in top_models]

    daily_rows = _rows(
        f"""
        SELECT *
        FROM (
            SELECT
                {day_expr} AS day,
                COALESCE(lc.model, '—') AS model,
                COALESCE(SUM(lc.tokens_in), 0)
                    + COALESCE(SUM(lc.tokens_out), 0)
                    + COALESCE(SUM(lc.cache_read_tokens), 0)
                    + COALESCE(SUM(lc.cache_write_tokens), 0)
                    + COALESCE(SUM(lc.reasoning_tokens), 0) AS total_tokens
            FROM llm_calls lc
            LEFT JOIN runs r ON r.session_id = lc.session_id
            WHERE {since_clause_ts} AND {visible_sql}
            GROUP BY {day_expr}, COALESCE(lc.model, '—')
            ORDER BY day DESC
            LIMIT 100000
        ) d
        ORDER BY day ASC
        """,
        tuple(visible_params),
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


def api_daily_model_chart(
    window_hours=24, limit_days=90, top_n=5, include_deleted=False, tz_name: str | None = None
):
    return DailyModelChartCache.instance().get_rows(
        window_hours=window_hours,
        limit_days=limit_days,
        top_n=top_n,
        include_deleted=include_deleted,
        tz_name=tz_name,
    )


def api_model_period_trends(
    window_hours=24,
    granularity="day",
    metric="tokens",
    top_n=6,
    limit_periods=24,
    tz_name: str | None = None,
):
    granularity = _normalize_granularity(granularity)
    metric = (metric or "tokens").strip().lower()
    if metric not in {"tokens", "cost", "requests", "share"}:
        raise ValueError(f"invalid metric: {metric!r}")
    top_n = max(1, min(8, int(top_n)))
    limit_periods = max(1, min(36, int(limit_periods)))
    since_clause_ts = _since_clause(window_hours, "ts")
    period_expr = _period_label_expr("ts", granularity, tz_name)

    metric_expr = {
        "tokens": "COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) + COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_write_tokens), 0) + COALESCE(SUM(reasoning_tokens), 0)",
        "cost": "COALESCE(SUM(cost_usd), 0)",
        "requests": "COUNT(*)",
        "share": "COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0) + COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_write_tokens), 0) + COALESCE(SUM(reasoning_tokens), 0)",
    }[metric]

    top_models = _rows(
        f"""
        SELECT COALESCE(model, '—') AS model,
               {metric_expr} AS metric_total
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY COALESCE(model, '—')
        ORDER BY metric_total DESC, model ASC
        LIMIT ?
        """,
        (top_n,),
    )
    model_names = [r["model"] for r in top_models]

    period_rows = _rows(
        f"""
        SELECT {period_expr} AS period,
               MIN({_period_start_expr("ts", granularity, tz_name)}) AS period_start,
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
        GROUP BY {period_expr}, COALESCE(model, '—')
        ORDER BY period_start ASC, model ASC
        """
    )

    period_order = []
    period_map = {}
    for row in period_rows:
        period = row["period"]
        if period not in period_map:
            period_order.append(period)
            period_map[period] = {
                "period": period,
                "period_start": row["period_start"],
                "models": {
                    m: {"api_calls": 0, "total_tokens": 0, "cost_usd": 0.0} for m in model_names
                },
                "other": {"api_calls": 0, "total_tokens": 0, "cost_usd": 0.0},
                "totals": {"api_calls": 0, "total_tokens": 0, "cost_usd": 0.0},
            }
        bucket = period_map[period]["models"].get(row["model"]) or period_map[period]["other"]
        bucket["api_calls"] += int(row.get("api_calls") or 0)
        bucket["total_tokens"] += int(row.get("total_tokens") or 0)
        bucket["cost_usd"] = round(
            float(bucket["cost_usd"] or 0) + float(row.get("cost_usd") or 0), 6
        )
        period_map[period]["totals"]["api_calls"] += int(row.get("api_calls") or 0)
        period_map[period]["totals"]["total_tokens"] += int(row.get("total_tokens") or 0)
        period_map[period]["totals"]["cost_usd"] = round(
            float(period_map[period]["totals"]["cost_usd"] or 0) + float(row.get("cost_usd") or 0),
            6,
        )

    if len(period_order) > limit_periods:
        period_order = period_order[-limit_periods:]

    return {
        "granularity": granularity,
        "metric": metric,
        "models": model_names,
        "rows": [period_map[p] for p in period_order],
    }


def api_model_share_comparison(
    window_hours=24, granularity="day", limit=12, tz_name: str | None = None
):
    granularity = _normalize_granularity(granularity)
    limit = max(1, min(20, int(limit)))
    since_clause_ts = _since_clause(window_hours, "ts")
    period_expr = _period_label_expr("ts", granularity, tz_name)
    rows = _rows(
        f"""
        SELECT {period_expr} AS period,
               MIN({_period_start_expr("ts", granularity, tz_name)}) AS period_start,
               COALESCE(model, '—') AS model,
               COUNT(*) AS api_calls,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               COALESCE(SUM(tokens_in), 0)
                   + COALESCE(SUM(tokens_out), 0)
                   + COALESCE(SUM(cache_read_tokens), 0)
                   + COALESCE(SUM(cache_write_tokens), 0)
                   + COALESCE(SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY {period_expr}, COALESCE(model, '—')
        ORDER BY period_start ASC, model ASC
        """
    )
    if not rows:
        return {
            "granularity": granularity,
            "current_period": None,
            "previous_period": None,
            "rows": [],
        }

    periods = []
    period_models = {}
    for row in rows:
        period = row["period"]
        if period not in period_models:
            periods.append(period)
            period_models[period] = {
                "models": {},
                "totals": {"total_tokens": 0, "cost_usd": 0.0, "api_calls": 0},
            }
        period_models[period]["models"][row["model"]] = {
            "total_tokens": int(row.get("total_tokens") or 0),
            "cost_usd": float(row.get("cost_usd") or 0),
            "api_calls": int(row.get("api_calls") or 0),
        }
        period_models[period]["totals"]["total_tokens"] += int(row.get("total_tokens") or 0)
        period_models[period]["totals"]["cost_usd"] = round(
            float(period_models[period]["totals"]["cost_usd"] or 0)
            + float(row.get("cost_usd") or 0),
            6,
        )
        period_models[period]["totals"]["api_calls"] += int(row.get("api_calls") or 0)

    current_period = periods[-1]
    if len(periods) < 2:
        return {
            "granularity": granularity,
            "current_period": current_period,
            "previous_period": None,
            "rows": [],
        }
    previous_period = periods[-2]
    current = period_models[current_period]
    previous = period_models[previous_period]
    all_models = set(current["models"]) | set(previous["models"])
    out_rows = []
    current_total_tokens = max(1, current["totals"]["total_tokens"])
    prev_total_tokens = max(1, previous["totals"]["total_tokens"])
    current_total_cost = max(0.000001, float(current["totals"]["cost_usd"] or 0.0))
    prev_total_cost = max(0.000001, float(previous["totals"]["cost_usd"] or 0.0))
    for model in all_models:
        cur = current["models"].get(model, {"total_tokens": 0, "cost_usd": 0.0, "api_calls": 0})
        prev = previous["models"].get(model, {"total_tokens": 0, "cost_usd": 0.0, "api_calls": 0})
        current_token_share = (
            round((cur["total_tokens"] / current_total_tokens) * 100, 2)
            if current["totals"]["total_tokens"]
            else 0.0
        )
        previous_token_share = (
            round((prev["total_tokens"] / prev_total_tokens) * 100, 2)
            if previous["totals"]["total_tokens"]
            else 0.0
        )
        current_cost_share = (
            round((float(cur["cost_usd"] or 0.0) / current_total_cost) * 100, 2)
            if current["totals"]["cost_usd"]
            else 0.0
        )
        previous_cost_share = (
            round((float(prev["cost_usd"] or 0.0) / prev_total_cost) * 100, 2)
            if previous["totals"]["cost_usd"]
            else 0.0
        )
        out_rows.append(
            {
                "model": model,
                "current_tokens": cur["total_tokens"],
                "previous_tokens": prev["total_tokens"],
                "current_token_share_pct": current_token_share,
                "previous_token_share_pct": previous_token_share,
                "token_share_delta_pct": round(current_token_share - previous_token_share, 2),
                "current_cost_usd": round(float(cur["cost_usd"] or 0.0), 6),
                "previous_cost_usd": round(float(prev["cost_usd"] or 0.0), 6),
                "current_cost_share_pct": current_cost_share,
                "previous_cost_share_pct": previous_cost_share,
                "cost_share_delta_pct": round(current_cost_share - previous_cost_share, 2),
                "current_api_calls": cur["api_calls"],
                "previous_api_calls": prev["api_calls"],
                "api_calls_delta": cur["api_calls"] - prev["api_calls"],
            }
        )
    out_rows.sort(
        key=lambda r: (r["current_tokens"], r["current_cost_usd"], r["current_api_calls"]),
        reverse=True,
    )
    return {
        "granularity": granularity,
        "current_period": current_period,
        "previous_period": previous_period,
        "rows": out_rows[:limit],
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


def _dashboard_viewer_tz(tz_name: str | None):
    if tz_name and ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name), tz_name
        except Exception:
            pass
    # UTC needs no timezone database, so honor it even when zoneinfo is
    # unavailable (Python 3.8) rather than falling back to the server's local tz.
    if tz_name and tz_name.upper() == "UTC":
        return timezone.utc, "UTC"
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    label = getattr(local_tz, "key", None) or str(local_tz)
    return local_tz, label


def _budget_window_bounds_utc(
    window: str, tz_name: str | None = None, now_utc: datetime | None = None
):
    tzinfo, tz_label = _dashboard_viewer_tz(tz_name)
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tzinfo)
    if window == "monthly":
        start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    else:
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
    return {
        "viewer_timezone": tz_label,
        "window_start_utc": start_local.astimezone(timezone.utc).isoformat(),
        "window_end_utc": end_local.astimezone(timezone.utc).isoformat(),
    }


def _budget_window_start_utc(window: str, tz_name: str | None = None) -> str:
    return _budget_window_bounds_utc(window, tz_name)["window_start_utc"]


def api_budget(tz_name: str | None = None):
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

    scopes = []

    g = budgets.get("global", {})
    for win_key in ("daily", "monthly"):
        limit = g.get(f"{win_key}_usd")
        if limit is None:
            continue
        bounds = _budget_window_bounds_utc(win_key, tz_name)
        spend = _one(
            "SELECT COALESCE(SUM(cost_usd),0.0) AS spent, COALESCE(SUM(estimated_llm_calls),0) AS est, COALESCE(SUM(api_calls),0) AS total FROM runs WHERE started_at >= ?",
            (bounds["window_start_utc"],),
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
                "scope": f"global/{win_key}",
                "window": win_key,
                "spent": round(spent, 6),
                "limit": limit,
                "pct": round(pct * 100, 1),
                "level": level,
                "estimated_calls": spend.get("est", 0),
                "total_calls": spend.get("total", 0),
                **bounds,
            }
        )

    return {"enabled": True, "budgets": scopes, "on_estimated": on_est.get("mode", "warn_only")}


MAX_BUDGET_PAYLOAD = 1_048_576  # 1 MiB


def _parse_int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name}: {value!r}") from None


def _coerce_window_hours(window_hours) -> float:
    try:
        return float(window_hours)
    except (TypeError, ValueError):
        raise ValueError(f"invalid hours: {window_hours!r}") from None


def _parse_window_hours(value, name: str = "hours") -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name}: {value!r}") from None


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


def api_budget_update(payload, tz_name: str | None = None):
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
    return api_budget(tz_name)


def api_budget_detail(scope: str, window: str, tz_name: str | None = None):
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
        **_budget_window_bounds_utc(window, tz_name),
    }


# Scope column used to filter `runs` for a scoped forecast. Keys are the budget
# config-key vocabulary the rest of the standalone dashboard uses (global,
# per_cron_job, per_sender, per_profile) — NOT the budget engine's internal
# scope names. None = no filter (global).
_FORECAST_SCOPE_COLUMN = {
    "global": None,
    "per_cron_job": "cron_job_id",
    "per_sender": "sender_id",
    "per_profile": "profile",
}


def _forecast_limit(scope: str, scope_id: str, window: str):
    """Resolve the configured USD limit for a scope/window from budget.yaml.

    Mirrors budget._resolve_limits but reads the file directly so serve.py stays
    self-contained (it must not import the package — see the isolation contract).
    """
    budget_path = DB_PATH.parent / "budget.yaml"
    if not budget_path.exists():
        return None
    try:
        import yaml

        cfg = yaml.safe_load(budget_path.read_text()) or {}
    except Exception:
        return None
    budgets = cfg.get("budgets", {})
    key = f"{window}_usd"
    if scope == "global":
        return (budgets.get("global") or {}).get(key)
    node = budgets.get(scope) or {}
    overrides = node.get("overrides") or {}
    chosen = overrides.get(scope_id) or node.get("default") or {}
    return chosen.get(key) if isinstance(chosen, dict) else None


def api_budget_forecast(
    scope: str = "global", window: str = "monthly", scope_id: str = "", tz_name: str | None = None
):
    """Project burn rate toward the configured limit.

    Self-contained: reads the limit from budget.yaml and learns a daily spend
    rate from the ``runs`` table, mirroring ``budget.burn_rate_projection``.
    serve.py must not import the package (isolation contract), so the projection
    is reimplemented here rather than delegated.
    """
    if scope not in _FORECAST_SCOPE_COLUMN or window not in ALLOWED_WINDOWS:
        return {"error": "invalid scope or window"}

    limit = _forecast_limit(scope, scope_id, window)
    if not limit or limit <= 0:
        return {"enabled": False, "scope": scope, "scope_id": scope_id, "window": window}

    col = _FORECAST_SCOPE_COLUMN[scope]
    scope_sql = f" AND {col} = ?" if col else ""
    scope_params: tuple = (scope_id,) if col else ()

    lookback_days = 14
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=lookback_days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    series_rows = _rows(
        f"SELECT substr(started_at, 1, 10) AS day, COALESCE(SUM(cost_usd), 0.0) AS cost_usd "
        f"FROM runs WHERE started_at >= ?{scope_sql} GROUP BY day",
        (start.isoformat(), *scope_params),
    )
    by_day = {r["day"]: float(r["cost_usd"] or 0.0) for r in series_rows}
    total = sum(
        by_day.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), 0.0)
        for i in range(lookback_days)
    )
    avg_daily = total / lookback_days

    bounds = _budget_window_bounds_utc(window, tz_name, now_utc)
    spend = _one(
        f"SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM runs WHERE started_at >= ?{scope_sql}",
        (bounds["window_start_utc"], *scope_params),
    )
    spent_so_far = float(spend.get("spent", 0.0))

    window_start = datetime.fromisoformat(bounds["window_start_utc"])
    window_end = datetime.fromisoformat(bounds["window_end_utc"])
    window_seconds = (window_end - window_start).total_seconds()
    elapsed = (now_utc - window_start).total_seconds()
    remaining_days = max(0.0, window_seconds - elapsed) / 86400.0

    projected_remaining = avg_daily * remaining_days
    projected_total = spent_so_far + projected_remaining
    pct = projected_total / limit if limit > 0 else 0.0
    status = "hard" if pct >= 1.00 else ("soft" if pct >= 0.80 else "ok")
    usd_left = limit - spent_so_far
    if avg_daily > 0:
        est_days_to_breach = round(usd_left / avg_daily, 2) if usd_left > 0 else 0.0
    else:
        est_days_to_breach = None

    return {
        "enabled": True,
        "scope": scope,
        "scope_id": scope_id,
        "window": window,
        "limit_usd": float(limit),
        "spent_so_far_usd": round(spent_so_far, 6),
        "avg_daily_usd": round(avg_daily, 6),
        "projected_remaining_usd": round(projected_remaining, 6),
        "projected_total_usd": round(projected_total, 6),
        "projected_pct": round(pct, 4),
        "status": status,
        "lookback_days": lookback_days,
        "est_days_to_breach": est_days_to_breach,
        **bounds,
    }


def _efficiency_score(tokens_in: int, tokens_out: int, api_calls: int, status: str) -> float:
    """Efficiency score (0-100). Mirrors db.efficiency_runs; kept inline because
    serve.py must not import the package. Only 'error'/'interrupted' are real
    failure statuses (see ONBOARDING § Agent Intelligence)."""
    output_ratio = tokens_out / tokens_in if tokens_in > 0 else 0.0
    output_contribution = min(60.0, output_ratio * 40.0)
    error_penalty = {"error": 30, "interrupted": 10}.get(status, 0)
    turn_penalty = min(30.0, api_calls * 1.5)
    return round(max(0.0, min(100.0, 40.0 + output_contribution - error_penalty - turn_penalty)), 1)


def api_efficiency(window_hours=24, include_deleted=False):
    """Per-session efficiency scores + aggregate. Self-contained (see isolation
    contract); mirrors db.efficiency_runs."""
    since = _since_clause(window_hours, "started_at")
    visible_sql, visible_params = _visible_sessions_clause("session_id", include_deleted)
    rows = _rows(
        f"""
        SELECT session_id,
               COALESCE(cron_job_id, '')          AS cron_job_id,
               COALESCE(status, 'running')        AS status,
               COALESCE(tokens_in, 0)             AS tokens_in,
               COALESCE(tokens_out, 0)            AS tokens_out,
               COALESCE(api_calls, 0)             AS api_calls,
               ROUND(COALESCE(cost_usd, 0.0), 6)  AS cost_usd,
               started_at
        FROM runs
        WHERE {since} AND status != 'running' AND {visible_sql}
        ORDER BY started_at DESC
        LIMIT 100
        """,
        visible_params,
    )
    for r in rows:
        r["efficiency_score"] = _efficiency_score(
            r["tokens_in"], r["tokens_out"], r["api_calls"], r["status"]
        )
    rows.sort(key=lambda x: x["efficiency_score"], reverse=True)
    avg = round(sum(r["efficiency_score"] for r in rows) / len(rows), 1) if rows else 0.0
    return {
        "window_hours": _coerce_window_hours(window_hours),
        "sessions_scored": len(rows),
        "average_score": avg,
        "sessions": rows,
    }


def api_smells(window_hours=24, include_deleted=False):
    """AI smell detection: anti-patterns over existing telemetry. Self-contained
    (see isolation contract); mirrors smell_detector.detect_all."""
    from collections import Counter

    since = _since_clause(window_hours, "started_at")
    tools_since = _since_clause(window_hours, "tc.ts")
    visible_sql, visible_params = _visible_sessions_clause("session_id", include_deleted)
    tools_visible_sql, tools_visible_params = _visible_sessions_clause(
        "tc.session_id", include_deleted
    )
    smells: list[dict] = []

    # context_rotation (high): input tokens dwarf output.
    for r in _rows(
        f"""SELECT session_id, COALESCE(cron_job_id,'') AS cron_job_id,
                   COALESCE(tokens_in,0) AS tokens_in, COALESCE(tokens_out,0) AS tokens_out,
                   COALESCE(api_calls,0) AS api_calls, COALESCE(status,'running') AS status, started_at
            FROM runs
            WHERE {since} AND status != 'running' AND {visible_sql}
              AND tokens_in > 1000
              AND CAST(tokens_out AS REAL) / CAST(tokens_in AS REAL) < 0.10
            ORDER BY tokens_in DESC LIMIT 50""",
        visible_params,
    ):
        ratio = r["tokens_out"] / max(r["tokens_in"], 1) * 100
        smells.append(
            {
                "smell": "context_rotation",
                "severity": "high",
                "session_id": r["session_id"],
                "cron_job_id": r["cron_job_id"],
                "detail": f"{r['tokens_in']:,} tokens in vs {r['tokens_out']:,} out ({ratio:.1f}% output)",
                "status": r["status"],
                "started_at": r["started_at"],
            }
        )

    # tool_thrashing (high): many tool calls with a high failure rate.
    for r in _rows(
        f"""SELECT tc.session_id, r.cron_job_id, r.status, r.started_at,
                   COUNT(*) AS total_tools,
                   SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END) AS failed_tools
            FROM tool_calls tc JOIN runs r ON tc.session_id = r.session_id
            WHERE {tools_since} AND r.status != 'running' AND {tools_visible_sql}
            GROUP BY tc.session_id
            HAVING total_tools > 20
               AND CAST(failed_tools AS REAL) / CAST(total_tools AS REAL) > 0.30
            ORDER BY total_tools DESC LIMIT 50""",
        tools_visible_params,
    ):
        rate = r["failed_tools"] / max(r["total_tools"], 1) * 100
        smells.append(
            {
                "smell": "tool_thrashing",
                "severity": "high",
                "session_id": r["session_id"],
                "cron_job_id": r["cron_job_id"] or "",
                "detail": f"{r['failed_tools']}/{r['total_tools']} tool calls failed ({rate:.1f}%)",
                "status": r["status"],
                "started_at": r["started_at"],
            }
        )

    # loop_trap (medium): a single tool dominates a busy session.
    tool_rows = _rows(
        f"""SELECT tc.session_id, tc.tool_name, r.cron_job_id, r.status, r.started_at
            FROM tool_calls tc JOIN runs r ON tc.session_id = r.session_id
            WHERE {tools_since} AND r.status != 'running' AND {tools_visible_sql}""",
        tools_visible_params,
    )
    by_session: dict[str, Counter] = {}
    meta: dict[str, dict] = {}
    for r in tool_rows:
        sid = r["session_id"]
        by_session.setdefault(sid, Counter())[r["tool_name"]] += 1
        meta.setdefault(
            sid,
            {
                "cron_job_id": r["cron_job_id"] or "",
                "status": r["status"],
                "started_at": r["started_at"],
            },
        )
    loop_traps = []
    for sid, counter in by_session.items():
        total = sum(counter.values())
        if total <= 10:
            continue
        top_name, top_count = counter.most_common(1)[0]
        if top_count / total > 0.80:
            loop_traps.append(
                {
                    "smell": "loop_trap",
                    "severity": "medium",
                    "session_id": sid,
                    "cron_job_id": meta[sid]["cron_job_id"],
                    "detail": f"{top_count}/{total} tool calls were '{top_name}' ({top_count / total * 100:.0f}%)",
                    "top_tool": top_name,
                    "total_tools": total,
                    "status": meta[sid]["status"],
                    "started_at": meta[sid]["started_at"],
                }
            )
    loop_traps.sort(key=lambda x: x["total_tools"], reverse=True)
    smells.extend(loop_traps[:50])

    # high_error_rate (warning|high): sessions that ended in 'error'.
    agg = _one(
        f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
            FROM runs WHERE {since} AND status != 'running' AND {visible_sql}""",
        visible_params,
    )
    total_runs = agg.get("total") or 0
    error_runs = agg.get("errors") or 0
    if error_runs:
        overall = "high" if (total_runs and error_runs / total_runs > 0.30) else "warning"
        for r in _rows(
            f"""SELECT session_id, COALESCE(cron_job_id,'') AS cron_job_id, status,
                       COALESCE(api_calls,0) AS api_calls, ROUND(COALESCE(cost_usd,0.0),6) AS cost_usd, started_at
                FROM runs WHERE {since} AND status = 'error' AND {visible_sql}
                ORDER BY started_at DESC LIMIT 50""",
            visible_params,
        ):
            smells.append(
                {
                    "smell": "high_error_rate",
                    "severity": overall,
                    "session_id": r["session_id"],
                    "cron_job_id": r["cron_job_id"],
                    "detail": f"Session ended with status 'error' — {r['api_calls']} API calls, ${r['cost_usd']:.6f}",
                    "status": r["status"],
                    "started_at": r["started_at"],
                }
            )

    # massive_session (warning): extreme token or API-call volume.
    for r in _rows(
        f"""SELECT session_id, COALESCE(cron_job_id,'') AS cron_job_id, COALESCE(status,'running') AS status,
                   COALESCE(tokens_in,0) AS tokens_in, COALESCE(tokens_out,0) AS tokens_out,
                   COALESCE(api_calls,0) AS api_calls, ROUND(COALESCE(cost_usd,0.0),6) AS cost_usd, started_at
            FROM runs
            WHERE {since} AND status != 'running' AND {visible_sql}
              AND ((tokens_in + tokens_out) > 100000 OR api_calls > 50)
            ORDER BY (tokens_in + tokens_out) DESC LIMIT 50""",
        visible_params,
    ):
        smells.append(
            {
                "smell": "massive_session",
                "severity": "warning",
                "session_id": r["session_id"],
                "cron_job_id": r["cron_job_id"],
                "detail": f"{(r['tokens_in'] + r['tokens_out']):,} total tokens, {r['api_calls']} API calls, ${r['cost_usd']:.4f}",
                "status": r["status"],
                "started_at": r["started_at"],
            }
        )

    severity_rank = {"high": 0, "medium": 1, "warning": 2}
    smells.sort(key=lambda s: (severity_rank.get(s["severity"], 99), s["smell"]))
    return {
        "window_hours": _coerce_window_hours(window_hours),
        "count": len(smells),
        "smells": smells,
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        try:
            # API routes
            if path == "/api/summary":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_summary(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )

            if path == "/api/cron":
                qs = parse_qs(parsed.query)
                return self._json(api_cron(_parse_window_hours(qs.get("hours", [168])[0], "hours")))

            if path == "/api/providers":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_providers(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )

            if path == "/api/provider-health":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_provider_health(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )

            if path == "/api/cache-efficiency":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_cache_efficiency(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )

            if path == "/api/runs":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_runs(
                        _parse_int(qs.get("limit", [50])[0], "limit"),
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        qs.get("day", [None])[0],
                        qs.get("model", [None])[0],
                        qs.get("provider", [None])[0],
                        qs.get("platform", [None])[0],
                        qs.get("status", [None])[0],
                        qs.get("session_id", [None])[0],
                        qs.get("cron_job_id", [None])[0],
                        qs.get("tool_name", [None])[0],
                        qs.get("q", [None])[0],
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/requests":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_requests(
                        _parse_int(qs.get("limit", [100])[0], "limit"),
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        qs.get("day", [None])[0],
                        qs.get("model", [None])[0],
                        qs.get("provider", [None])[0],
                        qs.get("platform", [None])[0],
                        qs.get("status", [None])[0],
                        qs.get("session_id", [None])[0],
                        qs.get("cron_job_id", [None])[0],
                        qs.get("tool_name", [None])[0],
                        qs.get("q", [None])[0],
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/session-detail":
                qs = parse_qs(parsed.query)
                return self._json(api_session_detail(qs.get("session_id", [""])[0]))

            if path == "/api/request-detail":
                qs = parse_qs(parsed.query)
                return self._json(api_request_detail(_parse_int(qs.get("id", [0])[0], "id")))

            if path == "/api/tool-analytics":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_tool_analytics(
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        qs.get("day", [None])[0],
                        qs.get("model", [None])[0],
                        qs.get("provider", [None])[0],
                        qs.get("platform", [None])[0],
                        qs.get("status", [None])[0],
                        qs.get("session_id", [None])[0],
                        qs.get("cron_job_id", [None])[0],
                        qs.get("tool_name", [None])[0],
                        qs.get("q", [None])[0],
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/error-center":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_error_center(
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        qs.get("day", [None])[0],
                        qs.get("model", [None])[0],
                        qs.get("provider", [None])[0],
                        qs.get("platform", [None])[0],
                        qs.get("status", [None])[0],
                        qs.get("session_id", [None])[0],
                        qs.get("cron_job_id", [None])[0],
                        qs.get("tool_name", [None])[0],
                        qs.get("q", [None])[0],
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/model-tokens":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_model_tokens(
                        _parse_window_hours(qs.get("hours", [24])[0], "hours"),
                        _parse_int(qs.get("limit", [100])[0], "limit"),
                    )
                )

            if path == "/api/model-efficiency":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_model_efficiency(
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        _parse_int(qs.get("limit", [50])[0], "limit"),
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/model-efficiency/cache/status":
                return self._json(api_model_efficiency_cache_status())

            if path == "/api/tool-failure-heatmap":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_tool_failure_heatmap(
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        _parse_int(qs.get("limit", [80])[0], "limit"),
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/cron-failure-waste":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_cron_failure_waste(
                        _parse_window_hours(qs.get("hours", [0])[0], "hours"),
                        _parse_int(qs.get("limit", [50])[0], "limit"),
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                    )
                )

            if path == "/api/daily-tokens":
                qs = parse_qs(parsed.query)
                tz_name = qs.get("tz", [None])[0]
                return self._json(
                    api_daily_tokens(
                        _parse_window_hours(qs.get("hours", [24])[0], "hours"),
                        _parse_int(qs.get("page", [1])[0], "page"),
                        _parse_int(qs.get("per_page", [15])[0], "per_page"),
                        tz_name,
                    )
                )

            if path == "/api/daily-token-chart":
                qs = parse_qs(parsed.query)
                tz_name = qs.get("tz", [None])[0]
                return self._json(
                    api_daily_token_chart(
                        _parse_window_hours(qs.get("hours", [24])[0], "hours"),
                        _parse_int(qs.get("limit_days", [90])[0], "limit_days"),
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                        qs.get("granularity", ["day"])[0],
                        tz_name,
                    )
                )

            if path == "/api/daily-model-chart":
                qs = parse_qs(parsed.query)
                tz_name = qs.get("tz", [None])[0]
                return self._json(
                    api_daily_model_chart(
                        _parse_window_hours(qs.get("hours", [24])[0], "hours"),
                        _parse_int(qs.get("limit_days", [90])[0], "limit_days"),
                        _parse_int(qs.get("top_n", [5])[0], "top_n"),
                        qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"},
                        tz_name,
                    )
                )

            if path == "/api/model-period-trends":
                qs = parse_qs(parsed.query)
                tz_name = qs.get("tz", [None])[0]
                return self._json(
                    api_model_period_trends(
                        _parse_window_hours(qs.get("hours", [24])[0], "hours"),
                        qs.get("granularity", ["day"])[0],
                        qs.get("metric", ["tokens"])[0],
                        _parse_int(qs.get("top_n", [6])[0], "top_n"),
                        _parse_int(qs.get("limit_periods", [24])[0], "limit_periods"),
                        tz_name,
                    )
                )

            if path == "/api/model-share-comparison":
                qs = parse_qs(parsed.query)
                tz_name = qs.get("tz", [None])[0]
                return self._json(
                    api_model_share_comparison(
                        _parse_window_hours(qs.get("hours", [24])[0], "hours"),
                        qs.get("granularity", ["day"])[0],
                        _parse_int(qs.get("limit", [12])[0], "limit"),
                        tz_name,
                    )
                )

            if path == "/api/budget":
                qs = parse_qs(parsed.query)
                tz_name = qs.get("tz", [None])[0]
                return self._json(api_budget(tz_name))

            if path == "/api/budget/detail":
                qs = parse_qs(parsed.query)
                scope = qs.get("scope", ["global"])[0]
                window = qs.get("window", ["daily"])[0]
                tz_name = qs.get("tz", [None])[0]
                return self._json(api_budget_detail(scope, window, tz_name))

            if path == "/api/budget/forecast":
                qs = parse_qs(parsed.query)
                scope = qs.get("scope", ["global"])[0]
                window = qs.get("window", ["monthly"])[0]
                scope_id = qs.get("scope_id", [""])[0]
                tz_name = qs.get("tz", [None])[0]
                return self._json(api_budget_forecast(scope, window, scope_id, tz_name))

            if path == "/api/efficiency":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_efficiency(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )

            if path == "/api/smells":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_smells(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )

            if path == "/api/token-breakdown":
                qs = parse_qs(parsed.query)
                return self._json(
                    api_token_breakdown(_parse_window_hours(qs.get("hours", [24])[0], "hours"))
                )
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

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
            return super().do_GET()

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/budget":
            parsed_qs = parse_qs(parsed.query)
            parsed_tz = parsed_qs.get("tz", [None])[0]
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
            result = api_budget_update(payload, parsed_tz)
            # Return proper HTTP status: 200 for success, 400 for validation errors
            status = 200 if result.get("enabled") or "error" not in result else 400
            return self._json(result, status)

        if path == "/api/model-efficiency/refresh":
            parsed_qs = parse_qs(parsed.query)
            wh_raw = parsed_qs.get("hours", [None])[0]
            include_deleted = parsed_qs.get("include_deleted", ["0"])[0] in {"1", "true", "yes"}
            if wh_raw in (None, "", "all"):
                return self._json(api_model_efficiency_refresh(include_deleted=include_deleted))
            return self._json(
                api_model_efficiency_refresh(
                    window_hours=int(_parse_window_hours(wh_raw, "hours")),
                    include_deleted=include_deleted,
                )
            )

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
        "captured token, cost, and tool-call detail, and can also trigger "
        "expensive POST /api/model-efficiency/refresh recomputes. Do not "
        "expose to the public internet or to untrusted networks."
    )
    print(msg, file=sys.stderr)
    logger.warning("Dashboard bound on %s with no authentication.", host)


# ---------------------------------------------------------------------------
# ModelEfficiencyCache
#
# Thin subclass of `_BackgroundPayloadCache`. The underlying query joins
# tool_calls × llm_calls over the configured window, which can take seconds
# once telemetry grows. Storage stays on `model_efficiency_cache` (schema v16)
# so status/refresh keep their existing shape; TTL / serve-stale / eviction
# logic is inherited from the base class.
# ---------------------------------------------------------------------------
class ModelEfficiencyCache(_BackgroundPayloadCache):
    CACHE_NAME = "model-efficiency"
    DEFAULT_WINDOWS = (24, 168, 720, 0)  # 1d, 1w, 30d, all
    DEFAULT_LIMITS = (50,)
    DEFAULT_KEYS = tuple(
        {
            "window_hours": wh,
            "include_deleted": include_deleted,
            "limit": limit,
        }
        for wh in (24, 168, 720, 0)
        for include_deleted in (False, True)
        for limit in (50,)
    )

    def cache_key(self, **kwargs):
        window_hours = kwargs.get("window_hours", 24)
        include_deleted = kwargs.get("include_deleted", False)
        limit = max(1, min(int(kwargs.get("limit", 50)), 500))
        return str(_model_efficiency_cache_key(window_hours, include_deleted, limit))

    def compute_rows(self, **kwargs):
        window_hours = kwargs.get("window_hours", 24)
        include_deleted = kwargs.get("include_deleted", False)
        limit = max(1, min(int(kwargs.get("limit", 50)), 500))
        return _compute_model_efficiency_rows(window_hours, limit, include_deleted)

    def _cache_row(self, key):
        with self._cond:
            row = self._conn.execute(
                "SELECT payload_json, rows_count, built_at FROM model_efficiency_cache WHERE cache_key = ?",
                (str(key),),
            ).fetchone()
            if not row:
                return None
            return {
                "payload": json.loads(row["payload_json"]),
                "rows_count": row["rows_count"],
                "built_at": row["built_at"],
            }

    def _evict_locked(self):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.MAX_ENTRY_AGE_SECONDS)
        ).isoformat()
        self._conn.execute(
            "DELETE FROM model_efficiency_cache WHERE built_at < ?",
            (cutoff,),
        )
        rows = self._conn.execute(
            "SELECT cache_key FROM model_efficiency_cache ORDER BY built_at DESC"
        ).fetchall()
        if len(rows) <= self.MAX_ENTRIES:
            return
        for row in rows[self.MAX_ENTRIES :]:
            self._conn.execute(
                "DELETE FROM model_efficiency_cache WHERE cache_key = ?",
                (row["cache_key"],),
            )

    def _write_cache(self, key, payload, **meta):
        """Persist payload with explicit metadata columns.

        Callers must pass window_hours/include_deleted/limit. We intentionally do
        not recover those fields from the string form of ``cache_key`` — that
        would couple row metadata to key encoding and silently mislabel rows if
        the key shape ever changes.
        """
        try:
            window_hours = meta["window_hours"]
            include_deleted = meta["include_deleted"]
            limit = meta["limit"]
        except KeyError as exc:
            raise ValueError(
                "ModelEfficiencyCache._write_cache requires window_hours, "
                "include_deleted, and limit kwargs from the seed/refresh path"
            ) from exc
        limit = max(1, min(int(limit), 500))
        with self._cond:
            now_iso = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                """
                INSERT INTO model_efficiency_cache (cache_key, window_hours, include_deleted, limit_n, payload_json, rows_count, built_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    rows_count = excluded.rows_count,
                    built_at = excluded.built_at,
                    window_hours = excluded.window_hours,
                    include_deleted = excluded.include_deleted,
                    limit_n = excluded.limit_n
                """,
                (
                    str(key),
                    int(_coerce_window_hours(window_hours)),
                    int(bool(include_deleted)),
                    int(limit),
                    json.dumps(payload, default=str),
                    _payload_rows_count(payload),
                    now_iso,
                ),
            )
            self._evict_locked()

    def get_rows(self, window_hours=24, limit=50, include_deleted=False, **kwargs):
        limit = max(1, min(int(limit), 500))
        return super().get_rows(
            window_hours=window_hours, limit=limit, include_deleted=include_deleted
        )

    def refresh(self, window_hours=None, include_deleted=False, limit=50, **kwargs):
        """Force an immediate refresh of one window (or all default windows)."""
        if self._refreshing:
            return {"skipped": "already refreshing"}
        self._refreshing = True
        try:
            if window_hours is None:
                return self._refresh_all()
            started = time.monotonic()
            # Keep historical behavior: a window refresh primes every default limit.
            for lim in self.DEFAULT_LIMITS:
                rows = self.compute_rows(
                    window_hours=window_hours, include_deleted=include_deleted, limit=lim
                )
                key = self.cache_key(
                    window_hours=window_hours, include_deleted=include_deleted, limit=lim
                )
                self._write_cache(
                    key,
                    rows,
                    window_hours=window_hours,
                    include_deleted=include_deleted,
                    limit=lim,
                )
            elapsed = time.monotonic() - started
            with self._cond:
                self._last_refresh_seconds = elapsed
                self._last_refresh_at = datetime.now(timezone.utc).isoformat()
                return {
                    "window_hours": int(_coerce_window_hours(window_hours)),
                    "include_deleted": bool(include_deleted),
                    "elapsed_seconds": round(elapsed, 3),
                    "refreshed_at": self._last_refresh_at,
                }
        finally:
            self._refreshing = False

    def _refresh_all(self):
        result = super()._refresh_all()
        if isinstance(result, dict) and "skipped" not in result:
            result["windows"] = list(self.DEFAULT_WINDOWS)
            result["limits"] = list(self.DEFAULT_LIMITS)
        return result

    def status(self):
        keys = []
        with self._cond:
            for row in self._conn.execute(
                "SELECT cache_key, window_hours, include_deleted, limit_n, rows_count, built_at FROM model_efficiency_cache ORDER BY window_hours, include_deleted, limit_n"
            ).fetchall():
                keys.append(
                    {
                        "cache_key": row["cache_key"],
                        "window_hours": row["window_hours"],
                        "include_deleted": bool(row["include_deleted"]),
                        "limit": row["limit_n"],
                        "rows_count": row["rows_count"],
                        "built_at": row["built_at"],
                    }
                )
        return {
            "refresh_interval_seconds": self.DEFAULT_REFRESH_SECONDS,
            "last_refresh_at": self._last_refresh_at,
            "last_refresh_seconds": (
                round(self._last_refresh_seconds, 3) if self._last_refresh_seconds else None
            ),
            "is_refreshing": self._refreshing,
            "entries": keys,
        }


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

    # Start background caches before binding the server so first-paint
    # never blocks on heavy historical aggregations.
    ProvidersCache.instance()
    ProviderHealthCache.instance()
    DailyTokenChartCache.instance()
    DailyModelChartCache.instance()
    ModelEfficiencyCache.instance()

    server = ThreadingHTTPServer((host, port), Handler)
    display_host = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"hermes-telemetry dashboard at http://{display_host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
