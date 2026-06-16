"""SQLite persistence layer for hermes-telemetry.

Per-thread connections (threading.local) + WAL mode for safe concurrent writes
from parallel cron jobs. See ONBOARDING.md for the concurrency rationale.

Public API used by __init__.py (hooks) and stats.py (/stats command):
  start_run(session_id, model, platform, cron_job_id=None, parent_session_id=None)
  end_run(session_id, status, ended_at=None)
  record_llm_call(session_id, ts, model, provider, tokens_in, tokens_out,
                  cost_usd, latency_ms,
                  cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0,
                  estimated=False)
  record_tool_call(session_id, ts, tool_name, ok, latency_ms)
  set_sender(session_id, sender_id)
  get_run(session_id)          -> dict | None
  stats_summary(window_hours)  -> dict
  cost_by_job(window_hours)    -> list[dict]
  recent_runs(limit)           -> list[dict]

Budget support (used by budget.py):
  spend_by_scope(scope, scope_id, since_iso) -> dict
  try_budget_alert(scope, scope_id, window, period_key, level, spent, limit) -> bool
  list_cron_job_ids(since_iso) -> list[str]
  list_sender_ids(since_iso)   -> list[str]
  stats_by_provider(window_hours) -> list[dict]
  stats_by_model(window_hours)    -> list[dict]
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 5
_local = threading.local()

# Serializes first-time schema setup across threads. Each thread opens its own
# connection (per-thread design), but DDL (CREATE/ALTER) run concurrently can
# raise SQLITE_LOCKED — which busy_timeout does NOT retry. Holding this lock
# while migrating is cheap (only the first connect per thread) and removes the
# race when many cron jobs start at once.
_schema_lock = threading.Lock()


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
        # busy_timeout MUST be set before journal_mode=WAL: switching a fresh,
        # contested DB to WAL needs a brief lock, and with the default timeout
        # of 0 a concurrent switch fails immediately with "database is locked".
        # 30s gives enough headroom for CI environments with slow or
        # network-backed filesystems where concurrent writes contend.
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
        with _schema_lock:
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

    # Insert v1 marker if not already present
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (1, ?)",
        (_utcnow(),),
    )

    # Apply migrations in order
    _migrate_v2(conn)
    _migrate_v3(conn)
    _migrate_v4(conn)
    _migrate_v5(conn)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add v2 columns: cache tokens + estimated flag on llm_calls;
    parent_session_id + estimated_llm_calls on runs."""
    # Check if migration already applied
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 2")
    if cur.fetchone() is not None:
        return

    new_cols_llm = [
        ("cache_read_tokens", "INTEGER DEFAULT 0"),
        ("cache_write_tokens", "INTEGER DEFAULT 0"),
        ("reasoning_tokens", "INTEGER DEFAULT 0"),
        ("estimated", "INTEGER DEFAULT 0"),
    ]
    for col, typedef in new_cols_llm:
        try:
            conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # already exists

    new_cols_runs = [
        ("parent_session_id", "TEXT"),
        ("estimated_llm_calls", "INTEGER DEFAULT 0"),
    ]
    for col, typedef in new_cols_runs:
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass  # already exists

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (2, ?)",
        (_utcnow(),),
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Add v3 schema: sender_id on runs (for per-sender budgets) and the
    budget_alerts table (anti-spam ledger for budget notifications)."""
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 3")
    if cur.fetchone() is not None:
        return

    try:
        conn.execute("ALTER TABLE runs ADD COLUMN sender_id TEXT")
    except sqlite3.OperationalError:
        pass  # already exists

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS budget_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT NOT NULL,
            scope_id    TEXT NOT NULL DEFAULT '',
            window      TEXT NOT NULL,
            period_key  TEXT NOT NULL,
            level       TEXT NOT NULL,
            fired_at    TEXT NOT NULL,
            spent_usd   REAL,
            limit_usd   REAL,
            UNIQUE(scope, scope_id, window, period_key, level)
        );

        CREATE INDEX IF NOT EXISTS idx_runs_sender ON runs(sender_id);
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (3, ?)",
        (_utcnow(),),
    )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Add v4 schema: cache tokens on runs table for per-session/cron breakdown."""
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 4")
    if cur.fetchone() is not None:
        return

    for col in ("cache_read_tokens", "cache_write_tokens"):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already exists

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (4, ?)",
        (_utcnow(),),
    )


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Add v5 schema: known_free_models table for free→paid transition alerts."""
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 5")
    if cur.fetchone() is not None:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS known_free_models (
            model         TEXT NOT NULL,
            provider      TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            PRIMARY KEY (model, provider)
        )
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (5, ?)",
        (_utcnow(),),
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_hours_ago_expr(hours: int) -> str:
    return f"datetime('now', '-{hours} hours')"


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


def _ensure_run_row(session_id: str, ts: str) -> None:
    """Idempotently ensure a `runs` row exists for ``session_id``.

    `INSERT OR IGNORE` so any existing row (created by `start_run` on the
    happy path) is preserved untouched. The row is opened in `running`
    status with `ts` as `started_at`; subsequent UPDATEs in
    `record_llm_call` / `end_run` populate the rest.

    Called by `record_llm_call` and `end_run` so the `UPDATE WHERE
    session_id = ?` paths never silently no-op for sessions whose
    `on_session_start` hook didn't reach the plugin — see
    hermes-agent `agent/conversation_loop.py:296` (the start hook fires
    "once when a brand-new session is created (not on continuation)").
    """
    _get_conn().execute(
        "INSERT OR IGNORE INTO runs (session_id, started_at, status) VALUES (?, ?, 'running')",
        (session_id, ts),
    )


def start_run(
    session_id: str,
    model: str,
    platform: str,
    cron_job_id: str | None = None,
    parent_session_id: str | None = None,
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO runs
            (session_id, model, platform, cron_job_id, parent_session_id, started_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'running')
        """,
        (session_id, model, platform, cron_job_id, parent_session_id, _utcnow()),
    )


def end_run(session_id: str, status: str, ended_at: str | None = None) -> None:
    now = ended_at or _utcnow()
    _ensure_run_row(session_id, now)
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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    estimated: bool = False,
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO llm_calls
            (session_id, ts, model, provider, tokens_in, tokens_out, cost_usd, latency_ms,
             cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            ts,
            model,
            provider,
            tokens_in,
            tokens_out,
            cost_usd,
            latency_ms,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            1 if estimated else 0,
        ),
    )
    _ensure_run_row(session_id, ts)
    conn.execute(
        """
        UPDATE runs
        SET tokens_in         = tokens_in  + ?,
            tokens_out        = tokens_out + ?,
            cache_read_tokens = cache_read_tokens + ?,
            cache_write_tokens = cache_write_tokens + ?,
            cost_usd          = cost_usd   + ?,
            api_calls         = api_calls  + 1,
            model             = COALESCE(model, ?),
            provider          = COALESCE(provider, ?)
        WHERE session_id = ?
        """,
        (
            tokens_in,
            tokens_out,
            cache_read_tokens,
            cache_write_tokens,
            cost_usd,
            model,
            provider,
            session_id,
        ),
    )
    if estimated:
        conn.execute(
            "UPDATE runs SET estimated_llm_calls = estimated_llm_calls + 1 WHERE session_id = ?",
            (session_id,),
        )


def set_sender(session_id: str, sender_id: str) -> None:
    """Attach a sender_id to a run (first non-null wins). Used for per-sender
    budgets — sender_id is only exposed to the pre_llm_call hook, not at
    session start."""
    if not sender_id:
        return
    conn = _get_conn()
    conn.execute(
        "UPDATE runs SET sender_id = COALESCE(sender_id, ?) WHERE session_id = ?",
        (sender_id, session_id),
    )


def get_run(session_id: str) -> dict[str, Any] | None:
    """Return the run row for a session, or None if not recorded."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM runs WHERE session_id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def record_tool_call(
    session_id: str,
    ts: str,
    tool_name: str,
    ok: bool,
    latency_ms: int | None,
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
            SUM(tool_calls)                                   AS tool_calls,
            SUM(estimated_llm_calls)                          AS estimated_llm_calls
        FROM runs
        WHERE started_at >= {since}
        """
    ).fetchone()

    llm_row = conn.execute(
        f"""
        SELECT
            COUNT(*)        AS api_calls,
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
    # Parent-child attribution is not yet populated (see ONBOARDING.md)
    result["parent_links_available"] = False
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
        SELECT session_id, platform, cron_job_id, sender_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cost_usd, duration_ms,
               api_calls, tool_calls,
               parent_session_id, estimated_llm_calls
        FROM runs
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Budget support (used by budget.py)
# ---------------------------------------------------------------------------


def spend_by_scope(scope: str, scope_id: str, since_iso: str) -> dict[str, Any]:
    """Aggregate spend for a budget scope since an ISO-8601 UTC timestamp.

    scope ∈ {"global", "cron_job", "sender"}. For "global", scope_id is
    ignored. Returns spent_usd plus an estimated_pct so callers can tell when
    a verdict rests on estimated (usage=None) rows.
    """
    conn = _get_conn()
    where = ["started_at >= ?"]
    params: list[Any] = [since_iso]
    if scope == "cron_job":
        where.append("cron_job_id = ?")
        params.append(scope_id)
    elif scope == "sender":
        where.append("sender_id = ?")
        params.append(scope_id)
    # "global": no extra filter

    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(cost_usd), 0.0)            AS spent_usd,
               COALESCE(SUM(estimated_llm_calls), 0)   AS estimated_calls,
               COALESCE(SUM(api_calls), 0)             AS total_calls
        FROM runs
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone()

    spent = float(row["spent_usd"] or 0.0)
    est = int(row["estimated_calls"] or 0)
    tot = int(row["total_calls"] or 0)
    return {
        "spent_usd": spent,
        "estimated_calls": est,
        "total_calls": tot,
        "estimated_pct": (est / tot) if tot else 0.0,
    }


def try_budget_alert(
    scope: str,
    scope_id: str,
    window: str,
    period_key: str,
    level: str,
    spent_usd: float,
    limit_usd: float,
) -> bool:
    """Record a budget alert idempotently. Returns True only the FIRST time a
    given (scope, scope_id, window, period_key, level) tuple is seen — this is
    the anti-spam guarantee so soft/hard/pause notices fire once per window."""
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO budget_alerts
            (scope, scope_id, window, period_key, level, fired_at, spent_usd, limit_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (scope, scope_id or "", window, period_key, level, _utcnow(), spent_usd, limit_usd),
    )
    return cur.rowcount > 0


def list_cron_job_ids(since_iso: str) -> list[str]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT cron_job_id FROM runs WHERE cron_job_id IS NOT NULL AND started_at >= ?",
        (since_iso,),
    ).fetchall()
    return [r["cron_job_id"] for r in rows]


def list_sender_ids(since_iso: str) -> list[str]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT sender_id FROM runs "
        "WHERE sender_id IS NOT NULL AND sender_id != '' AND started_at >= ?",
        (since_iso,),
    ).fetchall()
    return [r["sender_id"] for r in rows]


def stats_by_provider(window_hours: int = 24) -> list[dict[str, Any]]:
    """Per-provider breakdown for /stats providers.

    Returns one row per provider seen in llm_calls within the window:
      provider, total_calls, real_calls, estimated_calls, estimated_pct, cost_usd
    """
    conn = _get_conn()
    since = _run_hours_ago_expr(window_hours)
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(provider, '(unknown)') AS provider,
            COUNT(*)                                            AS total_calls,
            SUM(CASE WHEN estimated = 0 THEN 1 ELSE 0 END)    AS real_calls,
            SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END)    AS estimated_calls,
            ROUND(SUM(cost_usd), 6)                            AS cost_usd
        FROM llm_calls
        WHERE ts >= {since}
        GROUP BY COALESCE(provider, '(unknown)')
        ORDER BY cost_usd DESC
        """
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        total = row.get("total_calls") or 0
        est = row.get("estimated_calls") or 0
        row["estimated_pct"] = (est / total) if total else 0.0
        result.append(row)
    return result


def stats_by_model(window_hours: int = 24) -> list[dict[str, Any]]:
    """Per-model breakdown within each provider, for /stats models.

    Returns one row per (provider, model) seen in llm_calls within the window:
      provider, model, total_calls, real_calls, estimated_calls, estimated_pct, cost_usd

    Ordered by provider (asc) then call count (desc) so each provider's busiest
    models surface first — this is the view that exposes dated models costing
    $0.00 separately, without dropping to raw SQL.
    """
    conn = _get_conn()
    since = _run_hours_ago_expr(window_hours)
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(provider, '(unknown)') AS provider,
            COALESCE(model, '(unknown)')    AS model,
            COUNT(*)                                          AS total_calls,
            SUM(CASE WHEN estimated = 0 THEN 1 ELSE 0 END)   AS real_calls,
            SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END)   AS estimated_calls,
            ROUND(SUM(cost_usd), 6)                           AS cost_usd
        FROM llm_calls
        WHERE ts >= {since}
        GROUP BY COALESCE(provider, '(unknown)'), COALESCE(model, '(unknown)')
        ORDER BY provider ASC, total_calls DESC
        """
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        total = row.get("total_calls") or 0
        est = row.get("estimated_calls") or 0
        row["estimated_pct"] = (est / total) if total else 0.0
        result.append(row)
    return result


def close_thread_conn() -> None:
    """Close this thread's connection — call on clean thread exit if needed."""
    conn = getattr(_local, "conn", None)
    if conn:
        with contextlib.suppress(Exception):
            conn.close()
        _local.conn = None


# ---------------------------------------------------------------------------
# Estimated-price awareness (for budget degradation)
# ---------------------------------------------------------------------------
def estimated_price_share(scope: str, scope_id: str, since_iso: str) -> float:
    """Return the fraction of API calls since `since_iso` that used models
    marked with _estimated_price in pricing.yaml.

    This lets the budget engine degrade hard verdicts to soft when spend
    includes models without fixed pricing (e.g. OpenRouter auto-routing).

    Returns 0.0–1.0.
    """
    pricing_file = _get_db_path().parent / "pricing.yaml"
    if not pricing_file.exists():
        return 0.0
    try:
        import yaml

        cfg = yaml.safe_load(pricing_file.read_text()) or {}
        est_models = set(cfg.get("_meta", {}).get("estimated_price_models", []))
    except Exception:
        return 0.0
    if not est_models:
        return 0.0

    # Build WHERE clause for scope
    where = ["r.started_at >= ?"]
    params: list[Any] = [since_iso]
    if scope == "cron_job":
        where.append("r.cron_job_id = ?")
        params.append(scope_id)
    elif scope == "sender":
        where.append("r.sender_id = ?")
        params.append(scope_id)

    placeholders = ", ".join(f"'{m}'" for m in est_models)
    # Clamp to avoid SQLite parameter limits on huge lists
    row = (
        _get_conn()
        .execute(
            f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN l.model IN ({placeholders}) THEN 1 ELSE 0 END) AS est_calls
        FROM llm_calls l
        JOIN runs r ON l.session_id = r.session_id
        WHERE {" AND ".join(where)}
        """,
            params,
        )
        .fetchone()
    )
    total = row["total"] or 0
    est = row["est_calls"] or 0
    return est / total if total else 0.0


# ---------------------------------------------------------------------------
# Free→paid model tracking (issue #16)
# ---------------------------------------------------------------------------
def record_free_model(model: str, provider: str = "") -> None:
    """Persist a (model, provider) pair as known-free.

    Called whenever a call resolves to cost==0 with explicit pricing (not an
    unknown model). Subsequent calls to the same pair that cost >0 trigger the
    free→paid alert. INSERT OR IGNORE so we never overwrite first_seen_at.
    """
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO known_free_models(model, provider, first_seen_at) VALUES (?, ?, ?)",
        (model, provider, _utcnow()),
    )


def is_known_free_model(model: str, provider: str = "") -> bool:
    """Return True if this (model, provider) pair was previously seen at $0.

    Also matches rows with provider='' (wildcard sentinel written by
    backfill_known_free_models for pre-v5 installs that had no per-provider rows).
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT 1 FROM known_free_models WHERE model = ? AND (provider = ? OR provider = '')",
        (model, provider),
    ).fetchone()
    return row is not None


def is_free_tier_transition(model: str, provider: str = "") -> bool:
    """Return True if *model* looks like the paid successor of a known-free
    ``:free`` variant — i.e. a provider dropped the ``:free`` suffix (or renamed
    the promo id to its paid base) and started charging.

    Two ways a stored ``X:free`` row matches an incoming paid ``model``:
      1. Exact: ``model + ":free"`` is a known-free row — the bare-id rename,
         e.g. ``nvidia/nemotron-3-ultra`` ← ``nvidia/nemotron-3-ultra:free``.
      2. Prefix: a stored ``<base>:free`` whose ``<base>`` is a prefix of
         ``model`` at a token boundary — the suffixed paid id, e.g.
         ``nvidia/nemotron-3-ultra-550b-a55b`` ← ``nvidia/nemotron-3-ultra:free``.

    Matches the same provider or the wildcard ``provider=''`` backfill sentinel.
    """
    conn = _get_conn()
    # Check 1: incoming model is exactly a stored "<model>:free".
    row = conn.execute(
        "SELECT 1 FROM known_free_models WHERE model = ? AND (provider = ? OR provider = '')",
        (model + ":free", provider),
    ).fetchone()
    if row is not None:
        return True
    # Check 2: a stored "<base>:free" whose <base> is a prefix of model. Require
    # the char after <base> to be a separator so "…-3-ultra" matches the
    # "…-3-ultra-550b" variant but never an unrelated "…-3-ultraX" model.
    rows = conn.execute(
        "SELECT model FROM known_free_models"
        " WHERE (provider = ? OR provider = '') AND model LIKE '%:free'",
        (provider,),
    ).fetchall()
    for (free_model,) in rows:
        base = free_model[: -len(":free")]
        if base and model.startswith(base) and model[len(base) : len(base) + 1] in "-:/_":
            return True
    return False


def backfill_known_free_models(models: list) -> int:
    """Insert known-free models with provider='' (wildcard) for backward compat.

    Called once at plugin load for models that were explicitly $0 before the
    known_free_models table existed. INSERT OR IGNORE never overwrites real rows
    (rows with a specific provider take precedence and are unaffected).
    Returns the count of rows actually inserted.
    """
    conn = _get_conn()
    now = _utcnow()
    inserted = 0
    for model in models:
        cur = conn.execute(
            "INSERT OR IGNORE INTO known_free_models(model, provider, first_seen_at)"
            " VALUES (?, '', ?)",
            (model, now),
        )
        inserted += cur.rowcount
    return inserted
