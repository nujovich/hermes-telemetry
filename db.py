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
from datetime import timedelta as _timedelta
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
    """Add v10 schema: tiered storage rollup tables for daily, weekly, and
    monthly aggregation of token usage, cost, API calls, and tool calls.

    These tables are populated by upsert_rollups() at session end and by
    periodic compaction. They power bucketed analytics queries (/stats with
    --granularity) without expensive full-table scans over llm_calls.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 10")
    if cur.fetchone() is not None:
        return

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_rollups (
            period_start TEXT NOT NULL,
            model        TEXT NOT NULL DEFAULT '',
            provider     TEXT NOT NULL DEFAULT '',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            cost_usd     REAL DEFAULT 0.0,
            api_calls    INTEGER DEFAULT 0,
            tool_calls   INTEGER DEFAULT 0,
            session_count INTEGER DEFAULT 0,
            PRIMARY KEY (period_start, model, provider)
        );

        CREATE TABLE IF NOT EXISTS weekly_rollups (
            period_start TEXT NOT NULL,
            model        TEXT NOT NULL DEFAULT '',
            provider     TEXT NOT NULL DEFAULT '',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            cost_usd     REAL DEFAULT 0.0,
            api_calls    INTEGER DEFAULT 0,
            tool_calls   INTEGER DEFAULT 0,
            session_count INTEGER DEFAULT 0,
            PRIMARY KEY (period_start, model, provider)
        );

        CREATE TABLE IF NOT EXISTS monthly_rollups (
            period_start TEXT NOT NULL,
            model        TEXT NOT NULL DEFAULT '',
            provider     TEXT NOT NULL DEFAULT '',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            cost_usd     REAL DEFAULT 0.0,
            api_calls    INTEGER DEFAULT 0,
            tool_calls   INTEGER DEFAULT 0,
            session_count INTEGER DEFAULT 0,
            PRIMARY KEY (period_start, model, provider)
        );

        CREATE INDEX IF NOT EXISTS idx_daily_period
            ON daily_rollups(period_start);
        CREATE INDEX IF NOT EXISTS idx_weekly_period
            ON weekly_rollups(period_start);
        CREATE INDEX IF NOT EXISTS idx_monthly_period
            ON monthly_rollups(period_start);
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (10, ?)",
        (_utcnow(),),
    )


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """Add v11 schema: rollup_contrib table — the per-session source of truth
    for the tiered rollup tables.

    The daily/weekly/monthly rollup tables are *derived* aggregates shared
    across sessions. Re-folding a single session by re-scanning all of its
    llm_calls would double-count on a re-``end_run``. Instead, ``upsert_rollups``
    writes the session's per-(period, model, provider) contribution into
    ``rollup_contrib`` first (``ON CONFLICT ... REPLACE`` wipes any prior
    contribution from the same session), then ``_apply_session_rollups`` adds the
    *current* contribution to the shared rollup tables. To re-derive the rollups
    from scratch, ``compact_rollups`` recomputes both ``rollup_contrib`` and the
    rollup tables from ``llm_calls``.

    Only ``status = 'applied'`` rows count toward the rollups so a re-ended
    session replaces rather than re-adds its contribution.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 11")
    if cur.fetchone() is not None:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rollup_contrib (
            session_id   TEXT NOT NULL,
            granularity  TEXT NOT NULL,
            period_start TEXT NOT NULL,
            model        TEXT NOT NULL DEFAULT '',
            provider     TEXT NOT NULL DEFAULT '',
            tokens_in    INTEGER DEFAULT 0,
            tokens_out   INTEGER DEFAULT 0,
            cost_usd     REAL DEFAULT 0.0,
            api_calls    INTEGER DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'applied',
            PRIMARY KEY (session_id, granularity, period_start, model, provider)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rollup_contrib_period "
        "ON rollup_contrib(granularity, period_start)"
    )

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
    # Roll the session up into the tiered daily/weekly/monthly rollup tables so
    # later /stats --granularity queries can read pre-aggregated buckets instead
    # of scanning llm_calls. Failure here must never break the session end
    # itself, so swallow and log.
    try:
        upsert_rollups(session_id)
    except Exception:  # pragma: no cover - defensive, rollups are best-effort
        logger.exception("upsert_rollups failed for session %s", session_id)


# ---------------------------------------------------------------------------
# Tiered rollups (issue #157, M2)
# ---------------------------------------------------------------------------

_ROLLUP_TABLES = ("daily_rollups", "weekly_rollups", "monthly_rollups")
_ROLLUP_GRANULARITIES = ("daily", "weekly", "monthly")


def _period_starts(dt: datetime) -> tuple[str, str, str]:
    """Return (day, week_start, month_start) ISO date strings for *dt*.

    Week is Monday-based: ``week_start`` is the Monday of the ISO week containing
    *dt*. All three are pure date strings ('YYYY-MM-DD') so they sort and group
    lexically.
    """
    day = dt.strftime("%Y-%m-%d")
    week_start = (dt - _timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    month_start = dt.strftime("%Y-%m-01")
    return day, week_start, month_start


def _compute_session_contrib(
    session_id: str,
) -> list[tuple[str, str, str, str, int, int, float, int]]:
    """Aggregate one session's ``llm_calls`` into per-(granularity, period,
    model, provider) contribution rows.

    Returns a list of tuples ready to insert into ``rollup_contrib``:
        (granularity, period_start, model, provider, tokens_in, tokens_out,
         cost_usd, api_calls).
    A session contributes at most once per (granularity, period, model, provider)
    group, so ``session_count`` (derived in the rollup tables) stays correct even
    when a single session makes multiple calls into the same bucket.
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT ts, model, provider, tokens_in, tokens_out, cost_usd
        FROM llm_calls
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        return []

    # key -> [tokens_in, tokens_out, cost_usd, api_calls]
    agg: dict[tuple[str, str, str, str], list[float]] = {}
    for r in rows:
        ts = r["ts"]
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            # Fall back to the date portion if the timestamp is malformed.
            dt = datetime.fromisoformat(ts[:10])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day, week, month = _period_starts(dt)
        model = r["model"] or ""
        provider = r["provider"] or ""
        for granularity, period_start in zip(_ROLLUP_GRANULARITIES, (day, week, month)):
            key = (granularity, period_start, model, provider)
            bucket = agg.get(key)
            if bucket is None:
                bucket = [0.0, 0.0, 0.0, 0]
                agg[key] = bucket
            bucket[0] += r["tokens_in"] or 0
            bucket[1] += r["tokens_out"] or 0
            bucket[2] += r["cost_usd"] or 0.0
            bucket[3] += 1

    return [
        (g, p, m, pr, int(tin), int(tout), cost, int(calls))
        for (g, p, m, pr), (tin, tout, cost, calls) in agg.items()
    ]


def upsert_rollups(session_id: str) -> None:
    """Fold one finished session into the tiered rollup tables.

    Called from ``end_run``. The session's prior contribution is *retracted*
    from the shared rollup tables, then its freshly-computed contribution is
    written to ``rollup_contrib`` (replacing any stale rows for this session)
    and re-applied. This makes folding fully idempotent at the session level:
    re-ending a session replaces rather than re-adds its numbers, and
    ``session_count`` stays equal to the number of distinct sessions per bucket.
    """
    conn = _get_conn()
    # Retract whatever this session previously contributed so a re-end does not
    # double-count. Safe no-op the first time (no applied rows yet).
    _retract_session_rollups(session_id)

    contrib = _compute_session_contrib(session_id)
    if not contrib:
        # No calls: ensure the session holds no applied contribution.
        conn.execute(
            "UPDATE rollup_contrib SET status = 'stale' WHERE session_id = ?",
            (session_id,),
        )
        return

    # Replace the session's contribution wholesale.
    conn.execute("DELETE FROM rollup_contrib WHERE session_id = ?", (session_id,))
    conn.executemany(
        """
        INSERT INTO rollup_contrib
            (session_id, granularity, period_start, model, provider,
             tokens_in, tokens_out, cost_usd, api_calls, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied')
        """,
        [(session_id, *c) for c in contrib],
    )
    _apply_session_rollups(session_id)


def _retract_session_rollups(session_id: str) -> None:
    """Subtract a session's currently-applied contribution from the shared
    rollup tables. Called before re-applying so a re-``end_run`` replaces rather
    than re-adds. Rows that reach zero are deleted; ``session_count`` drops by 1.
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT granularity, period_start, model, provider,
               tokens_in, tokens_out, cost_usd, api_calls
        FROM rollup_contrib
        WHERE session_id = ? AND status = 'applied'
        """,
        (session_id,),
    ).fetchall()
    for r in rows:
        table = _ROLLUP_TABLES[_ROLLUP_GRANULARITIES.index(r["granularity"])]
        # session_count guard: only assert removal when this session is the only
        # contributor to the bucket.
        conn.execute(
            f"""
            UPDATE {table}
            SET tokens_in     = tokens_in     - ?,
                tokens_out    = tokens_out    - ?,
                cost_usd      = cost_usd      - ?,
                api_calls     = api_calls     - ?,
                session_count = session_count - 1
            WHERE period_start = ? AND model = ? AND provider = ?
            """,
            (
                r["tokens_in"],
                r["tokens_out"],
                r["cost_usd"],
                r["api_calls"],
                r["period_start"],
                r["model"],
                r["provider"],
            ),
        )
        conn.execute(
            f"""
            DELETE FROM {table}
            WHERE period_start = ? AND model = ? AND provider = ?
              AND tokens_in <= 0 AND tokens_out <= 0 AND api_calls <= 0
              AND session_count <= 0
            """,
            (r["period_start"], r["model"], r["provider"]),
        )


def _apply_session_rollups(session_id: str) -> None:
    """Add a session's ``rollup_contrib`` rows to the shared rollup tables.

    Only ``status = 'applied'`` contributions count. ``session_count`` is added
    exactly once per (period, model, provider) bucket the session now touches
    (``_retract_session_rollups`` already removed the previous +1, so this is
    the only one).
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT granularity, period_start, model, provider,
               tokens_in, tokens_out, cost_usd, api_calls
        FROM rollup_contrib
        WHERE session_id = ? AND status = 'applied'
        """,
        (session_id,),
    ).fetchall()
    if not rows:
        return

    for r in rows:
        table = _ROLLUP_TABLES[_ROLLUP_GRANULARITIES.index(r["granularity"])]
        conn.execute(
            f"""
            INSERT INTO {table}
                (period_start, model, provider,
                 tokens_in, tokens_out, cost_usd, api_calls, tool_calls, session_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)
            ON CONFLICT(period_start, model, provider) DO UPDATE SET
                tokens_in     = {table}.tokens_in     + excluded.tokens_in,
                tokens_out    = {table}.tokens_out    + excluded.tokens_out,
                cost_usd      = {table}.cost_usd      + excluded.cost_usd,
                api_calls     = {table}.api_calls     + excluded.api_calls,
                session_count = {table}.session_count + 1
            """,
            (
                r["period_start"],
                r["model"],
                r["provider"],
                r["tokens_in"],
                r["tokens_out"],
                r["cost_usd"],
                r["api_calls"],
            ),
        )


def compact_rollups(since_iso: str | None = None) -> dict[str, int]:
    """Periodic compaction: rebuild the rollup tables from the canonical
    ``llm_calls`` source via ``rollup_contrib``.

    Idempotent and safe to run on a schedule (e.g. a maintenance cron). When
    *since_iso* is given, only calls with ``ts >= since_iso`` are rescanned;
    otherwise the whole table is recomputed. ``session_count`` is derived from
    the number of distinct sessions in each bucket, so re-running never
    double-counts sessions.

    Returns:
        dict with keys ``daily``, ``weekly``, ``monthly`` (rows written).
    """
    conn = _get_conn()
    call_where = "WHERE ts >= ?" if since_iso else ""
    call_params = (since_iso,) if since_iso else ()

    # 1) Recompute rollup_contrib for the affected calls from llm_calls.
    contrib_rows: list[tuple] = []
    for granularity, period_expr in (
        ("daily", "substr(ts, 1, 10)"),
        ("weekly", "date(ts, 'weekday 1', '-7 days')"),
        ("monthly", "substr(ts, 1, 7) || '-01'"),
    ):
        data = conn.execute(
            f"""
            SELECT session_id,
                   {period_expr}              AS period_start,
                   COALESCE(model, '')        AS model,
                   COALESCE(provider, '')     AS provider,
                   COALESCE(SUM(tokens_in), 0)   AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)  AS tokens_out,
                   ROUND(SUM(cost_usd), 6)       AS cost_usd,
                   COUNT(*)                    AS api_calls
            FROM llm_calls
            {call_where}
            GROUP BY session_id, period_start, model, provider
            """,
            call_params,
        ).fetchall()
        for r in data:
            contrib_rows.append(
                (
                    r["session_id"],
                    granularity,
                    r["period_start"],
                    r["model"],
                    r["provider"],
                    int(r["tokens_in"]),
                    int(r["tokens_out"]),
                    r["cost_usd"],
                    int(r["api_calls"]),
                )
            )

    if since_iso:
        # Delete only the contrib keys we are about to rewrite (per session).
        affected_sessions = {row[0] for row in contrib_rows}
        for sid in affected_sessions:
            conn.execute("DELETE FROM rollup_contrib WHERE session_id = ?", (sid,))
    else:
        conn.execute("DELETE FROM rollup_contrib")

    conn.executemany(
        """
        INSERT INTO rollup_contrib
            (session_id, granularity, period_start, model, provider,
             tokens_in, tokens_out, cost_usd, api_calls, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied')
        """,
        contrib_rows,
    )

    # 2) Rebuild the rollup tables from the (now-correct) contributions.
    written: dict[str, int] = {}
    for table, granularity in zip(_ROLLUP_TABLES, _ROLLUP_GRANULARITIES):
        data = conn.execute(
            """
            SELECT period_start, model, provider,
                   SUM(tokens_in)            AS tokens_in,
                   SUM(tokens_out)           AS tokens_out,
                   ROUND(SUM(cost_usd), 6)   AS cost_usd,
                   SUM(api_calls)            AS api_calls,
                   COUNT(DISTINCT session_id) AS session_count
            FROM rollup_contrib
            WHERE granularity = ? AND status = 'applied'
            GROUP BY period_start, model, provider
            """,
            (granularity,),
        ).fetchall()

        # Delete only the buckets touched by the rewritten contributions.
        affected = {(r["period_start"], r["model"], r["provider"]) for r in data}
        if since_iso:
            for period_start, model, provider in affected:
                conn.execute(
                    f"DELETE FROM {table} WHERE period_start = ? AND model = ? AND provider = ?",
                    (period_start, model, provider),
                )
        else:
            conn.execute(f"DELETE FROM {table}")

        conn.executemany(
            f"""
            INSERT INTO {table}
                (period_start, model, provider,
                 tokens_in, tokens_out, cost_usd, api_calls, tool_calls, session_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            [
                (
                    r["period_start"],
                    r["model"],
                    r["provider"],
                    int(r["tokens_in"]),
                    int(r["tokens_out"]),
                    r["cost_usd"],
                    int(r["api_calls"]),
                    int(r["session_count"]),
                )
                for r in data
            ],
        )
        written[granularity] = len(data)
    return written


# ---------------------------------------------------------------------------
# Rollup retention + auto-prune (issue #157, M3)
# ---------------------------------------------------------------------------

# Default per-tier retention windows (days). A tier may be disabled by setting
# its retention to 0 or a negative value in retention.yaml.
_DEFAULT_RETENTION_DAYS = {"daily": 90, "weekly": 365, "monthly": 1825}


def _retention_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "telemetry" / "retention.yaml"


def _load_retention_config() -> dict[str, int]:
    """Load per-tier ``retention_days`` from ``retention.yaml``.

    Returns a mapping granularity -> retention window in days. Tiers missing
    from the file fall back to ``_DEFAULT_RETENTION_DAYS``; a missing,
    unreadable, or ill-formed file yields the defaults wholesale. A value <= 0
    disables pruning for that tier (its buckets are kept forever).
    """
    defaults = dict(_DEFAULT_RETENTION_DAYS)
    path = _retention_path()
    if not path.exists():
        return defaults
    try:
        import yaml
    except ImportError:
        return defaults
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - never let a bad config crash pruning
        logger.error("Failed to load %s: %s", path, exc)
        return defaults
    raw = (cfg.get("retention_days", {}) or {}) if isinstance(cfg, dict) else {}
    result = dict(defaults)
    for granularity in _ROLLUP_GRANULARITIES:
        # An explicit value in the file wins (including 0 / negative, which
        # disables pruning for that tier). Only an ABSENT key falls back to the
        # built-in default, so a deliberate "keep forever" (0) is honored.
        if granularity in raw:
            val = raw[granularity]
            if isinstance(val, int):
                result[granularity] = val
    return result


def auto_prune_rollups(retention: dict[str, int] | None = None) -> dict[str, int]:
    """Delete rollup buckets (and their ``rollup_contrib`` source rows) older
    than the per-tier retention window.

    *retention* maps granularity -> retention days. When omitted it is loaded
    from ``_load_retention_config()``. A tier with retention <= 0 is skipped
    (kept forever). Only whole buckets whose ``period_start`` is strictly
    older than ``today - retention_days`` are removed, so in-window data is
    never touched. Pruning the matching ``rollup_contrib`` rows ensures a later
    ``compact_rollups()`` rebuild cannot resurrect the dropped bucket.

    Safe to run on a schedule (e.g. a maintenance cron) and idempotent.

    Returns:
        dict granularity -> number of bucket rows pruned from that tier's table.
    """
    if retention is None:
        retention = _load_retention_config()
    conn = _get_conn()
    today = datetime.now(timezone.utc).date()
    pruned: dict[str, int] = {}
    for granularity, table in zip(_ROLLUP_GRANULARITIES, _ROLLUP_TABLES):
        days = retention.get(granularity, 0)
        if not isinstance(days, int) or days <= 0:
            pruned[granularity] = 0
            continue
        cutoff = (today - _timedelta(days=days)).isoformat()
        # Prune the shared, derived rollup table for this tier.
        cur = conn.execute(
            f"DELETE FROM {table} WHERE period_start < ?",
            (cutoff,),
        )
        pruned[granularity] = cur.rowcount
        # Prune the matching source-of-truth contribution rows so a later
        # compact_rollups() rebuild cannot resurrect the deleted bucket.
        conn.execute(
            "DELETE FROM rollup_contrib WHERE granularity = ? AND period_start < ?",
            (granularity, cutoff),
        )
    return pruned


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
) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO llm_calls
            (session_id, ts, model, provider, tokens_in, tokens_out, cost_usd, latency_ms,
             cache_read_tokens, cache_write_tokens, reasoning_tokens, estimated,
             provider_assumed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            SUM(estimated_llm_calls)                          AS estimated_llm_calls
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
