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

_SCHEMA_VERSION = 11
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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?", (column,))
    return cur.fetchone() is not None


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, typedef: str) -> None:
    """Idempotently add ``column`` to ``table``.

    Distinguishes "already exists" (benign — skip) from any other
    ``OperationalError`` like ``SQLITE_LOCKED`` or I/O failure (transient —
    must re-raise so the surrounding migration is NOT marked applied and the
    next connect retries). Catching ``OperationalError`` blindly and then
    marking the version as applied is what produced the v7 silent-skip bug
    (issue: provider_assumed column missing on DBs that show schema_version=7).
    """
    if _column_exists(conn, table, column):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


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
    _migrate_v6(conn)
    _migrate_v7(conn)
    _migrate_v8(conn)
    _migrate_v9(conn)
    _migrate_v10(conn)
    _migrate_v11(conn)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add v2 columns: cache tokens + estimated flag on llm_calls;
    parent_session_id + estimated_llm_calls on runs."""
    # Check if migration already applied
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 2")
    if cur.fetchone() is not None:
        return

    for col, typedef in (
        ("cache_read_tokens", "INTEGER DEFAULT 0"),
        ("cache_write_tokens", "INTEGER DEFAULT 0"),
        ("reasoning_tokens", "INTEGER DEFAULT 0"),
        ("estimated", "INTEGER DEFAULT 0"),
    ):
        _add_column_if_missing(conn, "llm_calls", col, typedef)

    for col, typedef in (
        ("parent_session_id", "TEXT"),
        ("estimated_llm_calls", "INTEGER DEFAULT 0"),
    ):
        _add_column_if_missing(conn, "runs", col, typedef)

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

    _add_column_if_missing(conn, "runs", "sender_id", "TEXT")

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
        _add_column_if_missing(conn, "runs", col, "INTEGER DEFAULT 0")

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


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Add v6 schema: free_paid_transitions table — historical record of
    every model that flipped from $0 to a paid charge. Powers the free→paid
    widget rendered inside TelemetryPage. One row per (model, provider) —
    first flip wins."""
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 6")
    if cur.fetchone() is not None:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS free_paid_transitions (
            model                TEXT NOT NULL,
            provider             TEXT NOT NULL DEFAULT '',
            detected_at          TEXT NOT NULL,
            session_id           TEXT,
            first_paid_cost_usd  REAL NOT NULL DEFAULT 0.0,
            first_free_seen_at   TEXT,
            PRIMARY KEY (model, provider)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_free_paid_detected ON free_paid_transitions(detected_at)"
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (6, ?)",
        (_utcnow(),),
    )


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """Add v7 schema: provider_assumed flag on llm_calls and a per-session
    counter on runs (issue #42).

    Marks calls whose cost used a provider-assumed rate — a source-ineligible
    (OpenRouter) price applied to a call another provider served, because no
    source-eligible price existed. The cost is real spend; the flag only lets
    stats / dashboard surface it as 'assumed' so the user can pin the rate."""
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 7")
    if cur.fetchone() is not None:
        return

    _add_column_if_missing(conn, "llm_calls", "provider_assumed", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "runs", "provider_assumed_calls", "INTEGER DEFAULT 0")

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (7, ?)",
        (_utcnow(),),
    )


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """Add v8 schema: model_unavailable_alerts table — surfaces 404s from the
    `api_request_error` hook so the user is notified when a model is removed
    (e.g. a `:free` promo end where hermes-agent does NOT silently re-route).
    Sibling to free_paid_transitions: same family of provider-side changes, but
    the call fails entirely instead of just billing real money.

    One row per (model, provider); repeated 404s bump ``occurrences`` and
    ``last_seen_at`` rather than inserting duplicates.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 8")
    if cur.fetchone() is not None:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_unavailable_alerts (
            model           TEXT NOT NULL,
            provider        TEXT NOT NULL DEFAULT '',
            error_code      INTEGER NOT NULL,
            error_message   TEXT,
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            occurrences     INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (model, provider)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_unavailable_last_seen"
        " ON model_unavailable_alerts(last_seen_at)"
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (8, ?)",
        (_utcnow(),),
    )


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """Reconcile columns from earlier ALTER-based migrations that may have been
    silently skipped on DBs migrated under the old ``except OperationalError:
    pass`` pattern.

    Background: ``_migrate_v2``/``_v3``/``_v4``/``_v7`` used a blanket
    ``except OperationalError`` around each ALTER and then unconditionally
    wrote the version marker. A transient ``SQLITE_LOCKED`` (cross-process
    contention from concurrent cron jobs on the same DB) would be swallowed,
    the column never added, and the version recorded as applied — wedging the
    DB until manually repaired.

    This migration re-adds any missing column using ``_add_column_if_missing``
    (idempotent). It is safe regardless of the prior state.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 9")
    if cur.fetchone() is not None:
        return

    for table, col, typedef in (
        ("llm_calls", "cache_read_tokens", "INTEGER DEFAULT 0"),
        ("llm_calls", "cache_write_tokens", "INTEGER DEFAULT 0"),
        ("llm_calls", "reasoning_tokens", "INTEGER DEFAULT 0"),
        ("llm_calls", "estimated", "INTEGER DEFAULT 0"),
        ("llm_calls", "provider_assumed", "INTEGER DEFAULT 0"),
        ("runs", "parent_session_id", "TEXT"),
        ("runs", "estimated_llm_calls", "INTEGER DEFAULT 0"),
        ("runs", "sender_id", "TEXT"),
        ("runs", "cache_read_tokens", "INTEGER DEFAULT 0"),
        ("runs", "cache_write_tokens", "INTEGER DEFAULT 0"),
        ("runs", "provider_assumed_calls", "INTEGER DEFAULT 0"),
    ):
        _add_column_if_missing(conn, table, col, typedef)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (9, ?)",
        (_utcnow(),),
    )


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """Add v10 schema: MoA (Mixture-of-Agents) attribution.

    Hermes' MoA is a virtual provider — ``post_api_request`` reports
    ``provider="moa"`` and ``model="<preset>"`` while the usage belongs to the
    preset's aggregator (the acting model). The plugin re-attributes the call to
    the aggregator's real provider/model (see ``moa.py``) and tags the row with
    the preset name so surfaces can flag that reference-model tokens are NOT
    captured (they run through Hermes' auxiliary call_llm path, which fires no
    hooks) and the recorded cost is therefore a lower bound.

    - ``llm_calls.moa_preset``: preset name for a MoA aggregator call; NULL for
      every non-MoA call.
    - ``runs.moa_calls``: per-session counter of MoA aggregator calls.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 10")
    if cur.fetchone() is not None:
        return

    _add_column_if_missing(conn, "llm_calls", "moa_preset", "TEXT")
    _add_column_if_missing(conn, "runs", "moa_calls", "INTEGER DEFAULT 0")

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (10, ?)",
        (_utcnow(),),
    )


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """Add v11 schema: subagent_edges — the persistent parent→child delegation
    tree used to attribute async/nested subagent cost to per_cron_job budgets
    (issue #49).

    child_session_id is the correlation key present in BOTH subagent_start and
    subagent_stop (subagent_stop carries no child_subagent_id). No cost is stored
    here; cost is resolved to the cron root at query time via a recursive CTE in
    spend_by_scope, so there is no double counting against the global tally.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 11")
    if cur.fetchone() is not None:
        return

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subagent_edges (
            child_session_id   TEXT PRIMARY KEY,
            parent_session_id  TEXT NOT NULL,
            parent_turn_id     TEXT,
            parent_subagent_id TEXT,
            child_subagent_id  TEXT,
            child_role         TEXT,
            started_at         TEXT NOT NULL,
            stopped_at         TEXT,
            child_status       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_subagent_edges_parent
            ON subagent_edges(parent_session_id);
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (11, ?)",
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
    provider_assumed: bool = False,
    moa_preset: str | None = None,
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO llm_calls
            (session_id, ts, model, provider, tokens_in, tokens_out, cost_usd, latency_ms,
             cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated,
             provider_assumed, moa_preset)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            1 if provider_assumed else 0,
            moa_preset,
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
    if provider_assumed:
        conn.execute(
            "UPDATE runs SET provider_assumed_calls = provider_assumed_calls + 1 "
            "WHERE session_id = ?",
            (session_id,),
        )
    if moa_preset:
        conn.execute(
            "UPDATE runs SET moa_calls = moa_calls + 1 WHERE session_id = ?",
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


def record_subagent_start(
    child_session_id: str,
    parent_session_id: str,
    parent_turn_id: str | None = None,
    parent_subagent_id: str | None = None,
    child_subagent_id: str | None = None,
    child_role: str | None = None,
    started_at: str | None = None,
) -> None:
    """Record a parent→child delegation edge. Idempotent on child_session_id.

    Fires from the subagent_start hook, which runs synchronously in Hermes'
    _build_child_agent BEFORE async dispatch — so the edge exists before the
    child's first post_api_request and resolution never races the child's events.
    """
    _get_conn().execute(
        """
        INSERT OR IGNORE INTO subagent_edges
            (child_session_id, parent_session_id, parent_turn_id,
             parent_subagent_id, child_subagent_id, child_role, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            child_session_id,
            parent_session_id,
            parent_turn_id,
            parent_subagent_id,
            child_subagent_id,
            child_role,
            started_at or _utcnow(),
        ),
    )


def record_subagent_stop(
    child_session_id: str,
    parent_session_id: str = "",
    child_status: str | None = None,
    child_role: str | None = None,
    stopped_at: str | None = None,
) -> None:
    """Finalize a delegation edge: set stopped_at + child_status.

    If the edge is missing (subagent_start not seen — rare, since start fires
    synchronously before dispatch), backfill it from the stop kwargs so the
    child's cost still resolves to its parent. child_subagent_id is absent on the
    stop hook, so a backfilled edge has no subagent id.
    """
    conn = _get_conn()
    now = stopped_at or _utcnow()
    cur = conn.execute(
        """
        UPDATE subagent_edges
        SET stopped_at   = ?,
            child_status = ?,
            child_role   = COALESCE(child_role, ?)
        WHERE child_session_id = ?
        """,
        (now, child_status, child_role, child_session_id),
    )
    if cur.rowcount == 0 and parent_session_id:
        conn.execute(
            """
            INSERT OR IGNORE INTO subagent_edges
                (child_session_id, parent_session_id, child_role,
                 started_at, stopped_at, child_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (child_session_id, parent_session_id, child_role, now, now, child_status),
        )


# ---------------------------------------------------------------------------
# Read API (used by stats.py)
# ---------------------------------------------------------------------------


def _build_where_clause(
    window_hours: int | None = None, date_from: str | None = None, date_to: str | None = None
) -> tuple[str, list]:
    """Build WHERE clause for date filtering.

    Priority: explicit date_from/date_to > window_hours > default (24h)
    Returns (where_sql, params)
    """
    where_parts = []
    params = []

    if date_from is not None:
        where_parts.append("started_at >= ?")
        params.append(date_from)
        if date_to is not None:
            where_parts.append("started_at < ?")
            params.append(date_to)
    elif window_hours is not None:
        where_parts.append(f"started_at >= {_run_hours_ago_expr(window_hours)}")
    else:
        # Default to last 24h
        where_parts.append(f"started_at >= {_run_hours_ago_expr(24)}")

    return (" AND ".join(where_parts), params)


def _build_where_clause_ts(
    window_hours: int | None = None, date_from: str | None = None, date_to: str | None = None
) -> tuple[str, list]:
    """Build WHERE clause for date filtering on llm_calls.ts column."""
    where_parts = []
    params = []

    if date_from is not None:
        where_parts.append("ts >= ?")
        params.append(date_from)
        if date_to is not None:
            where_parts.append("ts < ?")
            params.append(date_to)
    elif window_hours is not None:
        where_parts.append(f"ts >= {_run_hours_ago_expr(window_hours)}")
    else:
        where_parts.append(f"ts >= {_run_hours_ago_expr(24)}")

    return (" AND ".join(where_parts), params)


def _build_tools_where(
    window_hours: int | None = None, date_from: str | None = None, date_to: str | None = None
) -> tuple[str, list]:
    """Build WHERE clause for tool_calls JOIN runs query."""
    where_parts = []
    params = []

    if date_from is not None:
        where_parts.append("r.started_at >= ?")
        params.append(date_from)
        if date_to is not None:
            where_parts.append("r.started_at < ?")
            params.append(date_to)
    elif window_hours is not None:
        where_parts.append(f"r.started_at >= {_run_hours_ago_expr(window_hours)}")
    else:
        where_parts.append(f"r.started_at >= {_run_hours_ago_expr(24)}")

    return (" AND ".join(where_parts), params)


def stats_summary(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    conn = _get_conn()
    where_sql, params = _build_where_clause(window_hours, date_from, date_to)
    where_sql_ts, params_ts = _build_where_clause_ts(window_hours, date_from, date_to)
    tools_where, tools_params = _build_tools_where(window_hours, date_from, date_to)

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
            SUM(estimated_llm_calls)                          AS estimated_llm_calls,
            SUM(moa_calls)                                    AS moa_calls
        FROM runs
        WHERE {where_sql}
        """,
        params,
    ).fetchone()

    llm_row = conn.execute(
        f"""
        SELECT
            COUNT(*)        AS api_calls,
            AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls
        WHERE {where_sql_ts}
        """,
        params_ts,
    ).fetchone()

    top_tools = conn.execute(
        f"""
        SELECT tool_name,
               COUNT(*) AS calls,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,
               AVG(latency_ms) AS avg_ms
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE {tools_where}
        GROUP BY tool_name
        ORDER BY calls DESC
        LIMIT 10
        """,
        tools_params,
    ).fetchall()

    result = dict(runs_row)
    result.update(dict(llm_row))
    result["top_tools"] = [dict(t) for t in top_tools]
    result["window_hours"] = window_hours
    result["date_from"] = date_from
    result["date_to"] = date_to
    # Parent-child attribution is not yet populated (see ONBOARDING.md)
    result["parent_links_available"] = False
    return result


def cost_by_job(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> list[dict[str, Any]]:
    conn = _get_conn()
    where_sql, params = _build_where_clause(window_hours, date_from, date_to)
    if where_sql:
        where_sql = f"WHERE cron_job_id IS NOT NULL AND {where_sql}"
    else:
        where_sql = "WHERE cron_job_id IS NOT NULL"

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
        {where_sql}
        GROUP BY cron_job_id
        ORDER BY cost_usd DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def recent_runs(
    limit: int = 20,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    window_hours: int | None = None,
) -> list[dict[str, Any]]:
    conn = _get_conn()
    where_sql, params = _build_where_clause(window_hours, date_from, date_to)

    query = """
        SELECT session_id, platform, cron_job_id, sender_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cost_usd, duration_ms,
               api_calls, tool_calls,
               parent_session_id, estimated_llm_calls
        FROM runs
    """
    if where_sql:
        query += f" WHERE {where_sql}"
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
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
    if scope == "cron_job":
        # Attribute the whole delegation subtree to the cron job: seed from the
        # cron root session(s), walk down subagent_edges to every descendant, and
        # sum cost over root + descendants. This pulls in async and nested
        # subagent spend that lands under child session_ids with cron_job_id NULL.
        # The "global" branch below sums ALL runs, so the same cost rows are only
        # regrouped here — no double counting. UNION (not UNION ALL) guards cycles.
        row = conn.execute(
            """
            WITH RECURSIVE tree(session_id) AS (
                SELECT session_id FROM runs WHERE cron_job_id = ?
                UNION
                SELECT e.child_session_id
                FROM subagent_edges e
                JOIN tree t ON e.parent_session_id = t.session_id
            )
            SELECT COALESCE(SUM(r.cost_usd), 0.0)          AS spent_usd,
                   COALESCE(SUM(r.estimated_llm_calls), 0) AS estimated_calls,
                   COALESCE(SUM(r.api_calls), 0)           AS total_calls
            FROM runs r
            JOIN tree ON r.session_id = tree.session_id
            WHERE r.started_at >= ?
            """,
            (scope_id, since_iso),
        ).fetchone()
    else:
        where = ["started_at >= ?"]
        params: list[Any] = [since_iso]
        if scope == "sender":
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


def stats_by_provider(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> list[dict[str, Any]]:
    """Per-provider breakdown for /stats providers.

    Returns one row per provider seen in llm_calls within the window:
      provider, total_calls, real_calls, estimated_calls, estimated_pct,
      provider_assumed_calls, provider_assumed_pct, cost_usd
    """
    conn = _get_conn()
    where_sql, params = _build_where_clause_ts(window_hours, date_from, date_to)

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(provider, '(unknown)') AS provider,
            COUNT(*)                                            AS total_calls,
            SUM(CASE WHEN estimated = 0 THEN 1 ELSE 0 END)    AS real_calls,
            SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END)    AS estimated_calls,
            SUM(CASE WHEN provider_assumed = 1 THEN 1 ELSE 0 END) AS provider_assumed_calls,
            ROUND(SUM(cost_usd), 6)                            AS cost_usd
        FROM llm_calls
        WHERE {where_sql}
        GROUP BY COALESCE(provider, '(unknown)')
        ORDER BY cost_usd DESC
        """,
        params,
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        total = row.get("total_calls") or 0
        est = row.get("estimated_calls") or 0
        assumed = row.get("provider_assumed_calls") or 0
        row["estimated_pct"] = (est / total) if total else 0.0
        row["provider_assumed_pct"] = (assumed / total) if total else 0.0
        result.append(row)
    return result


def stats_by_model(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> list[dict[str, Any]]:
    """Per-model breakdown within each provider, for /stats models.

    Returns one row per (provider, model) seen in llm_calls within the window:
      provider, model, total_calls, real_calls, estimated_calls, estimated_pct,
      provider_assumed_calls, provider_assumed_pct, cost_usd

    Ordered by provider (asc) then call count (desc) so each provider's busiest
    models surface first — this is the view that exposes dated models costing
    $0.00 separately, without dropping to raw SQL.
    """
    conn = _get_conn()
    where_sql, params = _build_where_clause_ts(window_hours, date_from, date_to)

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(provider, '(unknown)') AS provider,
            COALESCE(model, '(unknown)')    AS model,
            COUNT(*)                                          AS total_calls,
            SUM(CASE WHEN estimated = 0 THEN 1 ELSE 0 END)   AS real_calls,
            SUM(CASE WHEN estimated = 1 THEN 1 ELSE 0 END)   AS estimated_calls,
            SUM(CASE WHEN provider_assumed = 1 THEN 1 ELSE 0 END) AS provider_assumed_calls,
            ROUND(SUM(cost_usd), 6)                           AS cost_usd
        FROM llm_calls
        WHERE {where_sql}
        GROUP BY COALESCE(provider, '(unknown)'), COALESCE(model, '(unknown)')
        ORDER BY provider ASC, total_calls DESC
        """,
        params,
    ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        total = row.get("total_calls") or 0
        est = row.get("estimated_calls") or 0
        assumed = row.get("provider_assumed_calls") or 0
        row["estimated_pct"] = (est / total) if total else 0.0
        row["provider_assumed_pct"] = (assumed / total) if total else 0.0
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


def record_free_paid_transition(
    model: str,
    provider: str,
    session_id: str | None,
    first_paid_cost_usd: float,
) -> None:
    """Persist the first time *model* (previously seen at $0) was charged.

    INSERT OR IGNORE — one row per (model, provider). ``first_free_seen_at`` is
    looked up from ``known_free_models`` so the dashboard can show how long
    the model was free before flipping.
    """
    conn = _get_conn()
    seen = conn.execute(
        "SELECT first_seen_at FROM known_free_models"
        " WHERE model = ? AND (provider = ? OR provider = '')"
        " ORDER BY provider DESC LIMIT 1",
        (model, provider),
    ).fetchone()
    first_free_seen_at = seen[0] if seen else None
    conn.execute(
        "INSERT OR IGNORE INTO free_paid_transitions"
        "(model, provider, detected_at, session_id, first_paid_cost_usd, first_free_seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (model, provider, _utcnow(), session_id, first_paid_cost_usd, first_free_seen_at),
    )


# ---------------------------------------------------------------------------
# Model-unavailable alerts (issue #43)
# Sibling of free_paid_transitions: same family of provider-side changes
# (deprecation / promo end) but the call fails with 404 instead of billing.
# ---------------------------------------------------------------------------
def record_model_unavailable(
    model: str,
    provider: str,
    error_code: int,
    error_message: str | None = None,
) -> None:
    """Upsert a model-unavailable alert row.

    First time a (model, provider) returns 404: insert a new row with
    ``occurrences=1`` and ``first_seen_at == last_seen_at``.
    Subsequent 404s for the same pair: bump ``occurrences`` and refresh
    ``last_seen_at`` / ``error_message`` (latest message wins) without
    overwriting ``first_seen_at``.
    """
    now = _utcnow()
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO model_unavailable_alerts
            (model, provider, error_code, error_message,
             first_seen_at, last_seen_at, occurrences)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(model, provider) DO UPDATE SET
            last_seen_at  = excluded.last_seen_at,
            error_code    = excluded.error_code,
            error_message = excluded.error_message,
            occurrences   = model_unavailable_alerts.occurrences + 1
        """,
        (model, provider, error_code, error_message, now, now),
    )


def get_model_unavailable(model: str, provider: str = "") -> dict | None:
    """Return the current alert row for (model, provider), or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT model, provider, error_code, error_message,"
        " first_seen_at, last_seen_at, occurrences"
        " FROM model_unavailable_alerts"
        " WHERE model = ? AND provider = ?",
        (model, provider),
    ).fetchone()
    return dict(row) if row else None


def recent_model_unavailable(window_hours: int = 72) -> list[dict]:
    """Return model-unavailable alerts whose *last_seen_at* falls within the
    last *window_hours*. ``window_hours <= 0`` returns the full history.
    Newest (most-recently-seen) first.
    """
    conn = _get_conn()
    if window_hours and window_hours > 0:
        rows = conn.execute(
            "SELECT model, provider, error_code, error_message,"
            " first_seen_at, last_seen_at, occurrences"
            " FROM model_unavailable_alerts"
            f" WHERE last_seen_at >= {_run_hours_ago_expr(window_hours)}"
            " ORDER BY last_seen_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT model, provider, error_code, error_message,"
            " first_seen_at, last_seen_at, occurrences"
            " FROM model_unavailable_alerts"
            " ORDER BY last_seen_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def recent_free_paid_transitions(window_hours: int = 72) -> list[dict]:
    """Return free→paid transitions detected within the last *window_hours*.

    ``window_hours <= 0`` returns the full history. Newest first.
    """
    conn = _get_conn()
    if window_hours and window_hours > 0:
        rows = conn.execute(
            "SELECT model, provider, detected_at, session_id,"
            " first_paid_cost_usd, first_free_seen_at"
            " FROM free_paid_transitions"
            f" WHERE detected_at >= {_run_hours_ago_expr(window_hours)}"
            " ORDER BY detected_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT model, provider, detected_at, session_id,"
            " first_paid_cost_usd, first_free_seen_at"
            " FROM free_paid_transitions"
            " ORDER BY detected_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


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
