"""AI Smell Detection for hermes-telemetry.

Detects anti-patterns in agent sessions by analysing the telemetry data already
captured in SQLite. No new telemetry collection is required — all detection heuristics
operate on existing runs, llm_calls, and tool_calls tables.

Detected smells:

  context_rotation  — sessions where input tokens dwarf output (inefficient context use)
  loop_trap         — sessions dominated by a single repeated tool call
  tool_thrashing    — sessions with many tool calls but a high failure rate
  high_error_rate   — cluster of failed sessions in the time window
  massive_session   — sessions with extreme token counts or API call volumes

Each smell carries a severity level (warning, medium, high) and a human-readable
description that the /stats smells subcommand surfaces.

Public API:
  detect_all(window_hours, date_from, date_to) -> list[dict]
  detect_by_session(window_hours, date_from, date_to) -> list[dict]
"""

from __future__ import annotations

import logging
from typing import Any

from . import db

logger = logging.getLogger("hermes_telemetry")


# ---------------------------------------------------------------------------
# Per-smell detection functions
# ---------------------------------------------------------------------------


def _detect_context_rotation(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions where input tokens vastly outnumber output tokens.

    Threshold: tokens_in > 1,000 AND (tokens_out / tokens_in) < 0.10.
    Indicates the agent is receiving large amounts of context but producing
    very little output — possibly due to excessive system prompts, unnecessary
    file inclusion, or repetitive context injection.
    """
    conn = db._get_conn()
    where_sql, params = db._build_where_clause(window_hours, date_from, date_to)

    rows = conn.execute(
        f"""\
        SELECT session_id,
               COALESCE(cron_job_id, '')          AS cron_job_id,
               COALESCE(tokens_in, 0)             AS tokens_in,
               COALESCE(tokens_out, 0)            AS tokens_out,
               COALESCE(api_calls, 0)             AS api_calls,
               COALESCE(status, 'running')         AS status,
               started_at
        FROM runs
        WHERE {where_sql}
          AND status != 'running'
          AND tokens_in > 1000
          AND CAST(tokens_out AS REAL) / CAST(tokens_in AS REAL) < 0.10
        ORDER BY tokens_in DESC
        LIMIT 50
        """,
        params,
    ).fetchall()

    return [
        {
            "smell": "context_rotation",
            "severity": "high",
            "session_id": r["session_id"],
            "cron_job_id": r["cron_job_id"],
            "detail": (
                f"{r['tokens_in']:,} tokens in vs {r['tokens_out']:,} tokens out "
                f"({r['tokens_out'] / max(r['tokens_in'], 1) * 100:.1f}% output ratio) "
                f"across {r['api_calls']} API calls"
            ),
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "api_calls": r["api_calls"],
            "status": r["status"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]


def _detect_loop_traps(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions where a single tool dominates all tool calls.

    Threshold: > 10 tool calls AND one tool name accounts for > 80% of them.
    Indicates the agent may be stuck in a repetitive pattern — calling the same
    tool repeatedly without making meaningful progress.
    """
    conn = db._get_conn()
    tools_where, tools_params = db._build_tools_where(window_hours, date_from, date_to)

    # Fetch all tool calls for sessions within the window, then aggregate
    # in Python (SQLite does not support referencing SELECT aliases in
    # the same query, so we cannot do the top-tool ratio check in SQL alone).
    all_rows = conn.execute(
        f"""\
        SELECT tc.session_id,
               tc.tool_name,
               r.cron_job_id,
               r.status,
               r.started_at
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE {tools_where}
          AND r.status != 'running'
        ORDER BY tc.session_id
        """,
        tools_params,
    ).fetchall()

    # Aggregate per session in Python
    from collections import Counter

    session_tools: dict[str, Counter[str]] = {}
    session_meta: dict[str, dict[str, Any]] = {}
    for r in all_rows:
        sid = r["session_id"]
        if sid not in session_tools:
            session_tools[sid] = Counter()
            session_meta[sid] = {
                "cron_job_id": r["cron_job_id"] or "",
                "status": r["status"],
                "started_at": r["started_at"],
            }
        session_tools[sid][r["tool_name"]] += 1

    results: list[dict[str, Any]] = []
    for sid, counter in session_tools.items():
        total = sum(counter.values())
        if total <= 10:
            continue
        top_name, top_count = counter.most_common(1)[0]
        if top_count / total > 0.80:
            results.append(
                {
                    "smell": "loop_trap",
                    "severity": "medium",
                    "session_id": sid,
                    "cron_job_id": session_meta[sid]["cron_job_id"],
                    "detail": (
                        f"{top_count}/{total} tool calls were '{top_name}' "
                        f"({top_count / total * 100:.0f}%) — possible loop"
                    ),
                    "top_tool": top_name,
                    "top_count": top_count,
                    "total_tools": total,
                    "status": session_meta[sid]["status"],
                    "started_at": session_meta[sid]["started_at"],
                }
            )

    # Sort by total tools descending, limit to 50
    results.sort(key=lambda x: x["total_tools"], reverse=True)
    return results[:50]


def _detect_tool_thrashing(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions with many tool calls and a high failure rate.

    Threshold: > 20 tool calls AND failure rate > 30%.
    Indicates the agent is trying many tools that fail — possibly due to
    incorrect tool parameters, unavailable services, or model hallucination.
    """
    conn = db._get_conn()
    tools_where, tools_params = db._build_tools_where(window_hours, date_from, date_to)

    rows = conn.execute(
        f"""\
        SELECT tc.session_id,
               r.cron_job_id,
               r.status,
               r.started_at,
               COUNT(*)                                          AS total_tools,
               SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END)       AS failed_tools
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE {tools_where}
          AND r.status != 'running'
        GROUP BY tc.session_id
        HAVING total_tools > 20
           AND CAST(failed_tools AS REAL) / CAST(total_tools AS REAL) > 0.30
        ORDER BY total_tools DESC
        LIMIT 50
        """,
        tools_params,
    ).fetchall()

    return [
        {
            "smell": "tool_thrashing",
            "severity": "high",
            "session_id": r["session_id"],
            "cron_job_id": r["cron_job_id"] or "",
            "detail": (
                f"{r['failed_tools']}/{r['total_tools']} tool calls failed "
                f"({r['failed_tools'] / max(r['total_tools'], 1) * 100:.1f}% failure rate)"
            ),
            "total_tools": r["total_tools"],
            "failed_tools": r["failed_tools"],
            "status": r["status"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]


def _detect_high_error_rate(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions that ended with an error status.

    Returns all sessions with status='error' in the window ('error' is the only
    failure status the plugin writes for a run — see ONBOARDING § Agent
    Intelligence). Severity is scaled: if > 30% of all sessions in the window
    errored, the smell is 'high'; otherwise 'warning'.
    """
    conn = db._get_conn()
    where_sql, params = db._build_where_clause(window_hours, date_from, date_to)

    # First get the error rate for severity scaling
    agg = conn.execute(
        f"""\
        SELECT
            COUNT(*)                                          AS total,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
        FROM runs
        WHERE {where_sql}
          AND status != 'running'
        """,
        params,
    ).fetchone()

    total = agg["total"] or 0
    errors = agg["errors"] or 0
    overall_severity = "high" if (total > 0 and errors / total > 0.30) else "warning"

    if errors == 0:
        return []

    rows = conn.execute(
        f"""\
        SELECT session_id,
               COALESCE(cron_job_id, '')  AS cron_job_id,
               status,
               COALESCE(tokens_in, 0)     AS tokens_in,
               COALESCE(tokens_out, 0)    AS tokens_out,
               COALESCE(api_calls, 0)     AS api_calls,
               COALESCE(tool_calls, 0)    AS tool_calls,
               ROUND(COALESCE(cost_usd, 0.0), 6) AS cost_usd,
               started_at
        FROM runs
        WHERE {where_sql}
          AND status = 'error'
        ORDER BY started_at DESC
        LIMIT 50
        """,
        params,
    ).fetchall()

    return [
        {
            "smell": "high_error_rate",
            "severity": overall_severity,
            "session_id": r["session_id"],
            "cron_job_id": r["cron_job_id"],
            "detail": (
                f"Session ended with status '{r['status']}' — "
                f"{r['tokens_in']:,} tokens in, "
                f"{r['tokens_out']:,} tokens out, "
                f"{r['api_calls']} API calls, "
                f"{r['tool_calls']} tool calls, "
                f"cost ${r['cost_usd']:.6f}"
            ),
            "overall_error_rate": f"{errors / max(total, 1) * 100:.1f}% ({errors}/{total} sessions)",
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "api_calls": r["api_calls"],
            "tool_calls": r["tool_calls"],
            "cost_usd": r["cost_usd"],
            "status": r["status"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]


def _detect_massive_sessions(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions with extreme token counts or API call volumes.

    Threshold: total tokens > 100,000 OR api_calls > 50.
    Indicates sessions that may be consuming disproportionate resources —
    possibly due to unbounded agent loops or overly ambitious tasks.
    """
    conn = db._get_conn()
    where_sql, params = db._build_where_clause(window_hours, date_from, date_to)

    rows = conn.execute(
        f"""\
        SELECT session_id,
               COALESCE(cron_job_id, '')          AS cron_job_id,
               COALESCE(status, 'running')         AS status,
               COALESCE(tokens_in, 0)             AS tokens_in,
               COALESCE(tokens_out, 0)            AS tokens_out,
               COALESCE(api_calls, 0)             AS api_calls,
               COALESCE(tool_calls, 0)            AS tool_calls,
               ROUND(COALESCE(cost_usd, 0.0), 6)   AS cost_usd,
               COALESCE(duration_ms, 0)            AS duration_ms,
               started_at
        FROM runs
        WHERE {where_sql}
          AND status != 'running'
          AND (
              (tokens_in + tokens_out) > 100000
              OR api_calls > 50
          )
        ORDER BY (tokens_in + tokens_out) DESC
        LIMIT 50
        """,
        params,
    ).fetchall()

    return [
        {
            "smell": "massive_session",
            "severity": "warning",
            "session_id": r["session_id"],
            "cron_job_id": r["cron_job_id"],
            "detail": (
                f"{(r['tokens_in'] + r['tokens_out']):,} total tokens "
                f"({r['tokens_in']:,} in, {r['tokens_out']:,} out), "
                f"{r['api_calls']} API calls, "
                f"{r['tool_calls']} tool calls, "
                f"{r['duration_ms'] / 1000:.0f}s duration, "
                f"${r['cost_usd']:.4f}"
            ),
            "total_tokens": r["tokens_in"] + r["tokens_out"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "api_calls": r["api_calls"],
            "tool_calls": r["tool_calls"],
            "cost_usd": r["cost_usd"],
            "duration_ms": r["duration_ms"],
            "status": r["status"],
            "started_at": r["started_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SMELL_DETECTORS = [
    ("context_rotation", _detect_context_rotation),
    ("loop_trap", _detect_loop_traps),
    ("tool_thrashing", _detect_tool_thrashing),
    ("high_error_rate", _detect_high_error_rate),
    ("massive_session", _detect_massive_sessions),
]


def detect_all(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Run all smell detectors and return a combined, sorted list.

    Results are sorted by severity (high > medium > warning), then by
    smell type. Each entry has keys:
      smell, severity, session_id, cron_job_id, detail,
      plus smell-specific fields.
    """
    all_smells: list[dict[str, Any]] = []
    severity_rank = {"high": 0, "medium": 1, "warning": 2}

    for smell_name, detector_fn in _SMELL_DETECTORS:
        try:
            results = detector_fn(window_hours=window_hours, date_from=date_from, date_to=date_to)
            all_smells.extend(results)
        except Exception as exc:
            # Smell detection is best-effort; a single broken query should not
            # prevent other detectors from running. Log at debug so a broken
            # detector stays visible instead of silently disappearing.
            logger.debug("smell detector %r failed: %s", smell_name, exc)
            continue

    all_smells.sort(key=lambda s: (severity_rank.get(s["severity"], 99), s["smell"]))
    return all_smells


def detect_by_session(
    window_hours: int | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Return smells grouped by session_id for per-session views.

    Each entry has keys:
      session_id, cron_job_id, status, started_at, smells (list of smell dicts),
      total_smells
    """
    all_smells = detect_all(window_hours=window_hours, date_from=date_from, date_to=date_to)

    sessions: dict[str, dict[str, Any]] = {}
    for s in all_smells:
        sid = s["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "cron_job_id": s.get("cron_job_id", ""),
                "status": s.get("status", "?"),
                "started_at": s.get("started_at", ""),
                "smells": [],
                "total_smells": 0,
            }
        sessions[sid]["smells"].append(s)
        sessions[sid]["total_smells"] += 1

    result = list(sessions.values())
    result.sort(key=lambda x: x["total_smells"], reverse=True)
    return result
