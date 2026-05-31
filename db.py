"""SQLite persistence layer for hermes-telemetry.

Per-thread connections (threading.local) + WAL mode for safe concurrent writes
from parallel cron jobs. See NOTES.md for the concurrency rationale.

Public API used by __init__.py (hooks) and stats.py (/stats command):
  start_run(session_id, model, platform, cron_job_id=None)
  end_run(session_id, status, ended_at=None)
  record_llm_call(session_id, ts, model, provider, tokens_in, tokens_out,
                  cost_usd, latency_ms)
  record_tool_call(session_id, ts, tool_name, ok, latency_ms)
  stats_summary(window_hours)  -> dict
  cost_by_job(window_hours)    -> list[dict]
  recent_runs(limit)           -> list[dict]
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_local = threading.local()


def _get_db_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    tele_dir = hermes_home / "telemetry"
    tele_dir.mkdir(parents=True, exist_ok=True)
    return tele_dir / "telemetry.db"


def _get_conn() -> sqlite3.Connection:
    """Return the per-thread SQLite connection, creating it on first access."""
    if not getattr(_local, "conn", None):
        db_path = _get_db_path()
        conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
        _ensure_schema(conn)
    return _local.conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version  INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            session_id   TEXT PRIMARY KEY,
            platform     TEXT,
            cron_job_id  TEXT,
            model        TEXT,
            provider     TEXT,
            started_at   TEXT NOT NULL,
            ended_at     TEXT,
            status       TEXT DEFAULT 'running',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            cost_usd     REAL DEFAULT 0.0,
            duration_ms  INTEGER,
            api_calls    INTEGER DEFAULT 0,
            tool_calls   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS llm_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            ts          TEXT NOT NULL,
            model       TEXT,
            provider    TEXT,
            tokens_in   INTEGER DEFAULT 0,
            tokens_out  INTEGER DEFAULT 0,
            cost_usd    REAL DEFAULT 0.0,
            latency_ms  INTEGER
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            ts          TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            ok          INTEGER NOT NULL DEFAULT 1,
            latency_ms  INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_runs_started    ON runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_runs_platform   ON runs(platform);
        CREATE INDEX IF NOT EXISTS idx_runs_cron_job   ON runs(cron_job_id);
        CREATE INDEX IF NOT EXISTS idx_llm_session     ON llm_calls(session_id);
        CREATE INDEX IF NOT EXISTS idx_llm_ts          ON llm_calls(ts);
        CREATE INDEX IF NOT EXISTS idx_tool_session    ON tool_calls(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_name       ON tool_calls(tool_name);
    """)

    cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (_SCHEMA_VERSION, _utcnow()),
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_hours_ago_expr(hours: int) -> str:
    return f"datetime('now', '-{hours} hours')"


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------

def start_run(
    session_id: str,
    model: str,
    platform: str,
    cron_job_id: Optional[str] = None,
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO runs
            (session_id, model, platform, cron_job_id, started_at, status)
        VALUES (?, ?, ?, ?, ?, 'running')
        """,
        (session_id, model, platform, cron_job_id, _utcnow()),
    )


def end_run(session_id: str, status: str, ended_at: Optional[str] = None) -> None:
    now = ended_at or _utcnow()
    conn = _get_conn()
    conn.execute(
        """
        UPDATE runs
        SET ended_at   = ?,
            status     = ?,
            duration_ms = CAST(
                (julianday(?) - julianday(started_at)) * 86400000 AS INTEGER
            )
        WHERE session_id = ?
        """,
        (now, status, now, session_id),
    )


def record_llm_call(
    session_id: str,
    ts: str,
    model: str,
    provider: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO llm_calls
            (session_id, ts, model, provider, tokens_in, tokens_out, cost_usd, latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, ts, model, provider, tokens_in, tokens_out, cost_usd, latency_ms),
    )
    conn.execute(
        """
        UPDATE runs
        SET tokens_in  = tokens_in  + ?,
            tokens_out = tokens_out + ?,
            cost_usd   = cost_usd   + ?,
            api_calls  = api_calls  + 1,
            model      = COALESCE(model, ?),
            provider   = COALESCE(provider, ?)
        WHERE session_id = ?
        """,
        (tokens_in, tokens_out, cost_usd, model, provider, session_id),
    )


def record_tool_call(
    session_id: str,
    ts: str,
    tool_name: str,
    ok: bool,
    latency_ms: Optional[int],
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO tool_calls (session_id, ts, tool_name, ok, latency_ms)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, ts, tool_name, 1 if ok else 0, latency_ms),
    )
    conn.execute(
        "UPDATE runs SET tool_calls = tool_calls + 1 WHERE session_id = ?",
        (session_id,),
    )


# ---------------------------------------------------------------------------
# Read API (used by stats.py)
# ---------------------------------------------------------------------------

def stats_summary(window_hours: int = 24) -> dict[str, Any]:
    conn = _get_conn()
    since = _run_hours_ago_expr(window_hours)

    runs_row = conn.execute(
        f"""
        SELECT
            COUNT(*)                                           AS total_runs,
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)   AS ok_runs,
            SUM(CASE WHEN status != 'ok'
                      AND status != 'running' THEN 1 ELSE 0 END) AS failed_runs,
            SUM(tokens_in)                                    AS tokens_in,
            SUM(tokens_out)                                   AS tokens_out,
            ROUND(SUM(cost_usd), 6)                           AS cost_usd,
            AVG(duration_ms)                                  AS avg_duration_ms,
            SUM(tool_calls)                                   AS tool_calls
        FROM runs
        WHERE started_at >= {since}
        """
    ).fetchone()

    llm_row = conn.execute(
        f"""
        SELECT
            COUNT(*)       AS api_calls,
            AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls
        WHERE ts >= {since}
        """
    ).fetchone()

    top_tools = conn.execute(
        f"""
        SELECT tool_name,
               COUNT(*) AS calls,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,
               AVG(latency_ms) AS avg_ms
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE r.started_at >= {since}
        GROUP BY tool_name
        ORDER BY calls DESC
        LIMIT 10
        """
    ).fetchall()

    result = dict(runs_row)
    result.update(dict(llm_row))
    result["top_tools"] = [dict(t) for t in top_tools]
    result["window_hours"] = window_hours
    return result


def cost_by_job(window_hours: int = 168) -> list[dict[str, Any]]:
    conn = _get_conn()
    since = _run_hours_ago_expr(window_hours)
    rows = conn.execute(
        f"""
        SELECT
            cron_job_id,
            COUNT(*)                                           AS runs,
            SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)   AS ok_runs,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS failed_runs,
            SUM(tokens_in)                                    AS tokens_in,
            SUM(tokens_out)                                   AS tokens_out,
            ROUND(SUM(cost_usd), 6)                           AS cost_usd,
            AVG(duration_ms)                                  AS avg_duration_ms,
            MAX(started_at)                                   AS last_run
        FROM runs
        WHERE cron_job_id IS NOT NULL
          AND started_at >= {since}
        GROUP BY cron_job_id
        ORDER BY cost_usd DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def recent_runs(limit: int = 20) -> list[dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cost_usd, duration_ms,
               api_calls, tool_calls
        FROM runs
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def close_thread_conn() -> None:
    """Close this thread's connection — call on clean thread exit if needed."""
    conn = getattr(_local, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
