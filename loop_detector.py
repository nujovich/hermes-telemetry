"""
Loop detection engine for hermes-telemetry.

Detects recurring execution patterns (loops) from Hermes session data:

- Cron-based loops: sessions where platform='cron' and a cron_job_id
  appears across multiple sessions (periodic execution).
- Self-perpetuating loops: sessions that create new cron jobs via the
  cronjob tool, spawning future runs.
- Tool-count loops: sessions containing scheduling tool calls (cronjob)
  that indicate the agent scheduled its own continuation.

All detection is based on tool-call patterns (specifically cronjob tool
calls), not text markers. This mirrors the approach in TraceToken telemetry
but adapted to Hermes' unified cronjob tool surface.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from . import db
except ImportError:
    import db

logger = logging.getLogger(__name__)

# Tool names that indicate loop-creating behavior in Hermes.
_LOOP_TOOL_NAMES: set[str] = {"cronjob"}


def _get_conn():
    """Internal helper — delegates to db's per-thread connection."""
    return db._get_conn()


def detect_session_loop(session_id: str) -> dict[str, Any] | None:
    """Detect whether *session_id* is part of or creates a loop.

    Returns a loop-fact dict on success, or None if no loop pattern is
    detected. The dict shape::

        {
            "session_id": str,
            "loop_type": "cron" | "self_perpetuating",
            "detected_from": "cron_platform" | "cronjob_tool",
            "cron_job_id": str | None,
            "tool_call_count": int,       # cronjob calls in this session
            "tool_call_names": list[str], # scheduling tool names found
        }

    None means no loop pattern was detected for this session.
    """
    conn = _get_conn()

    # Check 1: platform='cron' — the session IS a cron job execution.
    run_row = conn.execute(
        "SELECT platform, cron_job_id FROM runs WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if run_row and run_row["platform"] == "cron" and run_row["cron_job_id"]:
        return {
            "session_id": session_id,
            "loop_type": "cron",
            "detected_from": "cron_platform",
            "cron_job_id": run_row["cron_job_id"],
            "tool_call_count": 0,
            "tool_call_names": [],
        }

    # Check 2: session used cronjob tool — it scheduled its own continuation.
    tool_rows = conn.execute(
        """SELECT tool_name FROM tool_calls
           WHERE session_id = ? AND tool_name IN ({})
           ORDER BY ts""".format(",".join("?" for _ in _LOOP_TOOL_NAMES)),
        (session_id, *sorted(_LOOP_TOOL_NAMES)),
    ).fetchall()

    if tool_rows:
        names = [r["tool_name"] for r in tool_rows]
        # Determine job_id: if the session itself is a cron run, use its
        # cron_job_id; otherwise use parent context or None.
        job_id = run_row["cron_job_id"] if run_row else None
        return {
            "session_id": session_id,
            "loop_type": "self_perpetuating",
            "detected_from": "cronjob_tool",
            "cron_job_id": job_id,
            "tool_call_count": len(tool_rows),
            "tool_call_names": names,
        }

    return None


def list_loop_sessions() -> list[dict[str, Any]]:
    """Return all sessions that are part of or create a loop.

    Scans all runs and tool_calls. Returns a list of loop-fact dicts
    (same shape as detect_session_loop), one per loop session.
    """
    conn = _get_conn()
    results: list[dict[str, Any]] = []

    # Pass 1: cron-platform sessions with a cron_job_id.
    cron_rows = conn.execute(
        """SELECT session_id, cron_job_id FROM runs
           WHERE platform = 'cron' AND cron_job_id IS NOT NULL
           ORDER BY started_at"""
    ).fetchall()
    for row in cron_rows:
        results.append(
            {
                "session_id": row["session_id"],
                "loop_type": "cron",
                "detected_from": "cron_platform",
                "cron_job_id": row["cron_job_id"],
                "tool_call_count": 0,
                "tool_call_names": [],
            }
        )

    # Pass 2: sessions that used cronjob tool but are NOT already in results.
    already_seen = {r["session_id"] for r in results}
    tool_rows = conn.execute(
        """SELECT DISTINCT tc.session_id, r.cron_job_id
           FROM tool_calls tc
           LEFT JOIN runs r ON tc.session_id = r.session_id
           WHERE tc.tool_name IN ({})
           ORDER BY tc.ts""".format(",".join("?" for _ in _LOOP_TOOL_NAMES)),
        tuple(sorted(_LOOP_TOOL_NAMES)),
    ).fetchall()
    for row in tool_rows:
        sid = row["session_id"]
        if sid in already_seen:
            continue
        already_seen.add(sid)
        # Count the actual tool calls for this session.
        count_row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM tool_calls
               WHERE session_id = ? AND tool_name IN ({})""".format(
                ",".join("?" for _ in _LOOP_TOOL_NAMES)
            ),
            (sid, *sorted(_LOOP_TOOL_NAMES)),
        ).fetchone()
        results.append(
            {
                "session_id": sid,
                "loop_type": "self_perpetuating",
                "detected_from": "cronjob_tool",
                "cron_job_id": row["cron_job_id"],
                "tool_call_count": count_row["cnt"] if count_row else 0,
                "tool_call_names": ["cronjob"],
            }
        )

    return results


def list_loop_jobs() -> list[dict[str, Any]]:
    """Return distinct loop jobs with per-job aggregates.

    Each entry represents a distinct cron job that has fired at least once.
    Shape::

        {
            "cron_job_id": str,
            "loop_type": "cron" | "self_perpetuating",
            "session_count": int,       # how many sessions exist for this job
            "total_cost_usd": float,    # aggregate cost across all sessions
            "first_seen": str,          # ISO timestamp of first session
            "last_seen": str,           # ISO timestamp of most recent session
            "status": "active" | "expired" | "unknown",
        }
    """
    conn = _get_conn()

    # Lifecycle heuristic: active if last seen within 7 days, expired if
    # last seen > 30 days ago, unknown otherwise.
    results: list[dict[str, Any]] = []
    job_rows = conn.execute(
        """SELECT
             cron_job_id,
             COUNT(*) AS session_count,
             COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd,
             MIN(started_at) AS first_seen,
             MAX(started_at) AS last_seen
           FROM runs
           WHERE platform = 'cron' AND cron_job_id IS NOT NULL
           GROUP BY cron_job_id
           ORDER BY first_seen"""
    ).fetchall()

    now = db._utcnow()
    for row in job_rows:
        last = row["last_seen"]
        # Simple lifecycle: compare dates without heavy date parsing.
        if last >= now[:10] or last < now[:10] and last >= _days_ago(30, now):
            status = "active"
        elif last < _days_ago(30, now):
            status = "expired"
        else:
            status = "unknown"

        results.append(
            {
                "cron_job_id": row["cron_job_id"],
                "loop_type": "cron",
                "session_count": row["session_count"],
                "total_cost_usd": row["total_cost_usd"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "status": status,
            }
        )

    return results


def _days_ago(n: int, now: str) -> str:
    """Return ISO date string for *n* days before *now*."""
    from datetime import datetime, timedelta, timezone

    try:
        dt = datetime.fromisoformat(now)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    return (dt - timedelta(days=n)).isoformat()[:10]
