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
    # Telemetry files honor the opt-in canonical home (HERMES_TELEMETRY_HOME), so a
    # multi-profile user who consolidated via that var sees every profile's data here.
    # Replicated inline: plugin_api.py is loaded standalone (self-contained — no import
    # of paths.py), enforced by tests/test_dashboard_plugin_isolation.py.
    base = (
        os.environ.get("HERMES_TELEMETRY_HOME")
        or os.environ.get("HERMES_HOME")
        or str(Path.home() / ".hermes")
    )
    return Path(base) / "telemetry" / "telemetry.db"


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


def _runs_profile_clause(profile: str | None) -> tuple[str, tuple]:
    """AND-clause restricting a `runs` query to one profile (parameterized)."""
    return (" AND profile = ?", (profile,)) if profile else ("", ())


def _calls_profile_clause(
    profile: str | None, session_col: str = "session_id"
) -> tuple[str, tuple]:
    """AND-clause restricting an `llm_calls` query to one profile. Correlates the
    call's session to runs.profile via a subquery — avoids a JOIN and the
    column-name ambiguity between runs and llm_calls (both have provider,
    cost_usd, tokens_in, …). Pass a qualified session_col (e.g. "lc.session_id")
    when the query already aliases llm_calls."""
    if not profile:
        return "", ()
    return (
        f" AND {session_col} IN (SELECT session_id FROM runs WHERE profile = ?)",
        (profile,),
    )


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
def summary(window_hours: int = 24, profile: str = "") -> dict:
    sc = _since_clause(window_hours, "started_at")
    sc_ts = _since_clause(window_hours, "ts")
    rp, rp_params = _runs_profile_clause(profile)
    cp, cp_params = _calls_profile_clause(profile)
    runs = _one(
        f"""
        SELECT COUNT(*) AS total_runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status NOT IN ('ok','running') THEN 1 ELSE 0 END) AS failed_runs,
               COALESCE(SUM(tokens_in), 0) AS tokens_in,
               COALESCE(SUM(tokens_out), 0) AS tokens_out,
               ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
               COALESCE(SUM(moa_calls), 0) AS moa_calls,
               AVG(duration_ms) AS avg_duration_ms
        FROM runs WHERE {sc}{rp}
    """,
        rp_params,
    )
    llm = _one(
        f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE {sc_ts}{cp}
    """,
        cp_params,
    )
    daily = _rows(
        f"""
        SELECT DATE(started_at) AS day,
               ROUND(SUM(cost_usd), 4) AS cost,
               COUNT(*) AS runs
        FROM runs WHERE {sc}{rp}
        GROUP BY DATE(started_at)
        ORDER BY day
    """,
        rp_params,
    )
    return {
        "window_hours": _coerce_window_hours(window_hours),
        "runs": runs,
        "llm": llm,
        "daily_cost": daily,
    }


@router.get("/token-breakdown")
def token_breakdown(window_hours: int = 24, profile: str = "") -> dict:
    sc_ts = _since_clause(window_hours, "ts")
    cp, cp_params = _calls_profile_clause(profile)
    return _one(
        f"""
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
        FROM llm_calls WHERE {sc_ts}{cp}
    """,
        cp_params,
    )


@router.get("/profiles")
def profiles() -> dict:
    """Distinct non-null profiles present in runs — feeds the dashboard filter."""
    rows = _rows(
        "SELECT DISTINCT profile FROM runs "
        "WHERE profile IS NOT NULL AND profile != '' ORDER BY profile"
    )
    return {"profiles": [r["profile"] for r in rows]}


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------
@router.get("/runs")
def runs(limit: int = 50, window_hours: int = 0, profile: str = "") -> dict:
    sc = _since_clause(window_hours, "started_at")
    rp, rp_params = _runs_profile_clause(profile)
    rows = _rows(
        f"""
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
               cost_usd, duration_ms, api_calls, tool_calls, estimated_llm_calls
        FROM runs
        WHERE {sc}{rp}
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (*rp_params, max(1, min(int(limit), 500))),
    )
    total = _one(f"SELECT COUNT(*) AS n FROM runs WHERE {sc}{rp}", rp_params).get("n") or 0
    return {"total_runs": int(total), "rows": rows}


@router.get("/requests")
def requests(limit: int = 100, window_hours: int = 0, profile: str = "") -> dict:
    sc_ts = _since_clause(window_hours, "ts")
    cp, cp_params = _calls_profile_clause(profile, session_col="lc.session_id")
    rows = _rows(
        f"""
        SELECT lc.id, lc.ts, lc.session_id, lc.model, lc.provider,
               lc.tokens_in, lc.tokens_out, lc.cache_read_tokens,
               lc.cache_write_tokens, lc.reasoning_tokens,
               lc.cost_usd, lc.latency_ms, lc.estimated, lc.provider_assumed,
               lc.moa_preset,
               r.platform, r.cron_job_id, r.status
        FROM llm_calls lc
        LEFT JOIN runs r ON r.session_id = lc.session_id
        WHERE {sc_ts}{cp}
        ORDER BY lc.ts DESC, lc.id DESC
        LIMIT ?
        """,
        (*cp_params, max(1, min(int(limit), 1000))),
    )
    cp2, cp2_params = _calls_profile_clause(profile)
    total = (
        _one(f"SELECT COUNT(*) AS n FROM llm_calls WHERE {sc_ts}{cp2}", cp2_params).get("n") or 0
    )
    return {"total_requests": int(total), "rows": rows}


@router.get("/providers")
def providers(window_hours: int = 24, profile: str = "") -> dict:
    sc_ts = _since_clause(window_hours, "ts")
    cp, cp_params = _calls_profile_clause(profile)
    rows = _rows(
        f"""
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
        WHERE {sc_ts}{cp}
        GROUP BY COALESCE(provider, '—')
        ORDER BY cost_usd DESC, total_calls DESC
    """,
        cp_params,
    )
    return {"rows": rows}


@router.get("/cron")
def cron(window_hours: int = 168, profile: str = "") -> dict:
    sc = _since_clause(window_hours, "started_at")
    rp, rp_params = _runs_profile_clause(profile)
    rows = _rows(
        f"""
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
        WHERE cron_job_id IS NOT NULL AND {sc}{rp}
        GROUP BY cron_job_id
        ORDER BY last_run DESC
    """,
        rp_params,
    )
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

    # Per-profile scopes. The budget engine already models per_profile (default
    # + overrides); this is display only — mirror the global loop for each
    # profile present in runs, resolving override-else-default limits.
    pp_cfg = (cfg.get("budgets", {}) or {}).get("per_profile", {}) or {}
    if pp_cfg:
        pp_default = pp_cfg.get("default", {}) or {}
        pp_overrides = pp_cfg.get("overrides", {}) or {}
        profile_rows = _rows(
            "SELECT DISTINCT profile FROM runs "
            "WHERE profile IS NOT NULL AND profile != '' ORDER BY profile"
        )
        for prow in profile_rows:
            name = prow["profile"]
            limits = pp_overrides.get(name, pp_default) or {}
            for window in ("daily", "monthly"):
                limit = limits.get(f"{window}_usd")
                if limit is None:
                    continue
                start_utc = _window_start_utc(window)
                spend = _one(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM runs "
                    "WHERE started_at >= ? AND profile = ?",
                    (start_utc, name),
                )
                spent = float(spend.get("spent") or 0.0)
                pct = spent / limit if limit > 0 else 0.0
                level = "hard" if pct >= hard_pct else ("soft" if pct >= soft_pct else "ok")
                scopes.append(
                    {
                        "scope": f"profile:{name}/{window}",
                        "scope_id": name,
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


@router.get("/tier-transitions")
def tier_transitions(window_hours: int = 72) -> dict:
    """Recent free→paid model transitions, newest first.

    Powers the free→paid widget rendered inside ``TelemetryPage``.
    ``window_hours <= 0`` returns the full history. The table is created by
    the runtime (db.py schema v6); missing-table is treated as "nothing
    flipped yet" so the dashboard stays functional on pre-v6 installs.
    """
    wh = _coerce_window_hours(window_hours)
    sql_base = (
        "SELECT model, provider, detected_at, session_id,"
        " first_paid_cost_usd, first_free_seen_at"
        " FROM free_paid_transitions"
    )
    if wh > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=wh)).isoformat()
        sql = sql_base + " WHERE detected_at >= ? ORDER BY detected_at DESC"
        params: tuple = (cutoff,)
    else:
        sql = sql_base + " ORDER BY detected_at DESC"
        params = ()
    try:
        rows = _rows(sql, params)
    except sqlite3.OperationalError:
        rows = []
    return {"window_hours": wh, "rows": rows}


@router.get("/model-unavailable")
def model_unavailable(window_hours: int = 72) -> dict:
    """Recent model-unavailable (HTTP 404) alerts, newest last_seen_at first.

    Powers the model-unavailable widget rendered inside ``TelemetryPage``,
    sibling to ``/tier-transitions``. ``window_hours <= 0`` returns the full
    history. Table created by db.py schema v8; missing-table is treated as
    "nothing failed yet" so the dashboard stays functional on pre-v8 installs.
    """
    wh = _coerce_window_hours(window_hours)
    sql_base = (
        "SELECT model, provider, error_code, error_message,"
        " first_seen_at, last_seen_at, occurrences"
        " FROM model_unavailable_alerts"
    )
    if wh > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=wh)).isoformat()
        sql = sql_base + " WHERE last_seen_at >= ? ORDER BY last_seen_at DESC"
        params: tuple = (cutoff,)
    else:
        sql = sql_base + " ORDER BY last_seen_at DESC"
        params = ()
    try:
        rows = _rows(sql, params)
    except sqlite3.OperationalError:
        rows = []
    return {"window_hours": wh, "rows": rows}


# ---------------------------------------------------------------------------
# Efficiency score
# ---------------------------------------------------------------------------
def _compute_efficiency(tokens_in: int, tokens_out: int, api_calls: int, status: str) -> float:
    """Compute an efficiency score (0-100) for a single session.

    Kept in sync with ``db.efficiency_runs``. Only 'ok', 'error', and
    'interrupted' are ever stored as run statuses; 'error' is the real
    failure status and carries the heavy penalty.
    """
    output_ratio = tokens_out / tokens_in if tokens_in > 0 else 0.0
    output_contribution = min(60.0, output_ratio * 40.0)
    error_penalty = {"error": 30, "interrupted": 10}.get(status, 0)
    turn_penalty = min(30.0, api_calls * 1.5)
    score = 40.0 + output_contribution - error_penalty - turn_penalty
    return round(max(0.0, min(100.0, score)), 1)


@router.get("/efficiency")
def efficiency(window_hours: int = 24, profile: str = "") -> dict:
    """Return per-session efficiency scores and aggregate stats."""
    sc = _since_clause(window_hours, "started_at")
    pc, pc_params = _runs_profile_clause(profile)
    rows = _rows(
        f"""
        SELECT session_id,
               COALESCE(cron_job_id, '')         AS cron_job_id,
               COALESCE(status, 'running')        AS status,
               COALESCE(tokens_in, 0)             AS tokens_in,
               COALESCE(tokens_out, 0)            AS tokens_out,
               COALESCE(api_calls, 0)             AS api_calls,
               ROUND(COALESCE(cost_usd, 0.0), 6)  AS cost_usd,
               started_at
        FROM runs
        WHERE {sc}
          AND status != 'running'{pc}
        ORDER BY started_at DESC
        LIMIT 100
    """,
        pc_params,
    )
    scored = []
    for r in rows:
        scored.append(
            {
                "session_id": r["session_id"],
                "cron_job_id": r["cron_job_id"],
                "status": r["status"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "api_calls": r["api_calls"],
                "cost_usd": r["cost_usd"],
                "efficiency_score": _compute_efficiency(
                    r["tokens_in"], r["tokens_out"], r["api_calls"], r["status"]
                ),
                "started_at": r["started_at"],
            }
        )

    scored.sort(key=lambda x: x["efficiency_score"], reverse=True)

    avg = sum(s["efficiency_score"] for s in scored) / len(scored) if scored else 0.0

    return {
        "window_hours": _coerce_window_hours(window_hours),
        "sessions_scored": len(scored),
        "average_score": round(avg, 1),
        "sessions": scored,
    }


# ---------------------------------------------------------------------------
# AI smell detection
# ---------------------------------------------------------------------------
@router.get("/smells")
def smells(window_hours: int = 24, profile: str = "") -> dict:
    """AI smell detection over existing telemetry. Inlined per the standalone
    loader constraint; mirrors smell_detector.detect_all."""
    from collections import Counter

    sc = _since_clause(window_hours, "started_at")
    tsc = _since_clause(window_hours, "tc.ts")
    # Profile scoping: `pc` for queries on `runs` directly, `pcr` for queries
    # that JOIN runs as `r` (column name must be qualified there). `pp` is the
    # shared parameter tuple for whichever clause is used.
    pc = " AND profile = ?" if profile else ""
    pcr = " AND r.profile = ?" if profile else ""
    pp: tuple = (profile,) if profile else ()
    found: list[dict] = []

    for r in _rows(
        f"""SELECT session_id, COALESCE(cron_job_id,'') AS cron_job_id,
                   COALESCE(tokens_in,0) AS tokens_in, COALESCE(tokens_out,0) AS tokens_out,
                   COALESCE(status,'running') AS status, started_at
            FROM runs WHERE {sc} AND status != 'running'{pc}
              AND tokens_in > 1000
              AND CAST(tokens_out AS REAL) / CAST(tokens_in AS REAL) < 0.10
            ORDER BY tokens_in DESC LIMIT 50""",
        pp,
    ):
        ratio = r["tokens_out"] / max(r["tokens_in"], 1) * 100
        found.append(
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

    for r in _rows(
        f"""SELECT tc.session_id, r.cron_job_id, r.status, r.started_at,
                   COUNT(*) AS total_tools,
                   SUM(CASE WHEN tc.ok = 0 THEN 1 ELSE 0 END) AS failed_tools
            FROM tool_calls tc JOIN runs r ON tc.session_id = r.session_id
            WHERE {tsc} AND r.status != 'running'{pcr}
            GROUP BY tc.session_id
            HAVING total_tools > 20
               AND CAST(failed_tools AS REAL) / CAST(total_tools AS REAL) > 0.30
            ORDER BY total_tools DESC LIMIT 50""",
        pp,
    ):
        rate = r["failed_tools"] / max(r["total_tools"], 1) * 100
        found.append(
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

    tool_rows = _rows(
        f"""SELECT tc.session_id, tc.tool_name, r.cron_job_id, r.status, r.started_at
            FROM tool_calls tc JOIN runs r ON tc.session_id = r.session_id
            WHERE {tsc} AND r.status != 'running'{pcr}""",
        pp,
    )
    by_session: dict = {}
    meta: dict = {}
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
    found.extend(loop_traps[:50])

    agg = _one(
        f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
            FROM runs WHERE {sc} AND status != 'running'{pc}""",
        pp,
    )
    total_runs = agg.get("total") or 0
    error_runs = agg.get("errors") or 0
    if error_runs:
        overall = "high" if (total_runs and error_runs / total_runs > 0.30) else "warning"
        for r in _rows(
            f"""SELECT session_id, COALESCE(cron_job_id,'') AS cron_job_id, status,
                       COALESCE(api_calls,0) AS api_calls, ROUND(COALESCE(cost_usd,0.0),6) AS cost_usd, started_at
                FROM runs WHERE {sc} AND status = 'error'{pc}
                ORDER BY started_at DESC LIMIT 50""",
            pp,
        ):
            found.append(
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

    for r in _rows(
        f"""SELECT session_id, COALESCE(cron_job_id,'') AS cron_job_id, COALESCE(status,'running') AS status,
                   COALESCE(tokens_in,0) AS tokens_in, COALESCE(tokens_out,0) AS tokens_out,
                   COALESCE(api_calls,0) AS api_calls, ROUND(COALESCE(cost_usd,0.0),6) AS cost_usd, started_at
            FROM runs WHERE {sc} AND status != 'running'{pc}
              AND ((tokens_in + tokens_out) > 100000 OR api_calls > 50)
            ORDER BY (tokens_in + tokens_out) DESC LIMIT 50""",
        pp,
    ):
        found.append(
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

    rank = {"high": 0, "medium": 1, "warning": 2}
    found.sort(key=lambda s: (rank.get(s["severity"], 99), s["smell"]))
    return {
        "window_hours": _coerce_window_hours(window_hours),
        "count": len(found),
        "smells": found,
    }


# ---------------------------------------------------------------------------
# Burn-rate forecast (global scope)
# ---------------------------------------------------------------------------
@router.get("/forecast")
def forecast(window: str = "monthly") -> dict:
    """Global burn-rate projection toward the configured limit. Inlined per the
    standalone loader constraint; mirrors budget.burn_rate_projection."""
    if window not in ("daily", "monthly"):
        return {"enabled": False, "scope": "global", "window": window, "error": "invalid window"}
    path = _budget_path()
    if not path.exists():
        return {"enabled": False, "scope": "global", "window": window}
    try:
        import yaml

        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"enabled": False, "scope": "global", "window": window}
    limit = ((cfg.get("budgets", {}) or {}).get("global", {}) or {}).get(f"{window}_usd")
    if not limit or limit <= 0:
        return {"enabled": False, "scope": "global", "window": window}

    lookback_days = 14
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=lookback_days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    series = _rows(
        "SELECT substr(started_at, 1, 10) AS day, COALESCE(SUM(cost_usd), 0.0) AS cost_usd "
        "FROM runs WHERE started_at >= ? GROUP BY day",
        (start.isoformat(),),
    )
    by_day = {r["day"]: float(r["cost_usd"] or 0.0) for r in series}
    total = sum(
        by_day.get((start + timedelta(days=i)).strftime("%Y-%m-%d"), 0.0)
        for i in range(lookback_days)
    )
    avg_daily = total / lookback_days

    start_utc = _window_start_utc(window)
    spent = float(
        _one(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS spent FROM runs WHERE started_at >= ?",
            (start_utc,),
        ).get("spent")
        or 0.0
    )

    local_now = datetime.now().astimezone()
    if window == "monthly":
        import calendar

        days_in_window = calendar.monthrange(local_now.year, local_now.month)[1]
        win_start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        days_in_window = 1
        win_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (local_now - win_start_local).total_seconds()
    remaining_days = max(0.0, days_in_window * 86400.0 - elapsed) / 86400.0

    projected_total = spent + avg_daily * remaining_days
    pct = projected_total / limit if limit > 0 else 0.0
    status = "hard" if pct >= 1.00 else ("soft" if pct >= 0.80 else "ok")
    usd_left = limit - spent
    if avg_daily > 0:
        est_days_to_breach = round(usd_left / avg_daily, 2) if usd_left > 0 else 0.0
    else:
        est_days_to_breach = None

    return {
        "enabled": True,
        "scope": "global",
        "window": window,
        "limit_usd": float(limit),
        "spent_so_far_usd": round(spent, 6),
        "avg_daily_usd": round(avg_daily, 6),
        "projected_total_usd": round(projected_total, 6),
        "projected_pct": round(pct, 4),
        "status": status,
        "lookback_days": lookback_days,
        "est_days_to_breach": est_days_to_breach,
    }
