"""Tests for db.py — write/read, idempotent migrations, aggregations, concurrency."""

from __future__ import annotations

import threading

import pytest

# ---------------------------------------------------------------------------
# Fixture: isolate each test in a fresh temp DB
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test a clean DB connection.

    HERMES_HOME (and thus the DB path) is isolated to a per-test tmp dir by the
    project-level autouse fixture in conftest.py; here we just reset the
    per-thread connection between tests.
    """
    import hermes_telemetry.db as db_mod

    db_mod._local.conn = None
    yield
    # Cleanup
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


import hermes_telemetry.db as db
from hermes_telemetry.db import _SCHEMA_VERSION

# ---------------------------------------------------------------------------
# Schema migrations — idempotent
# ---------------------------------------------------------------------------


def test_schema_creates_tables():
    conn = db._get_conn()
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "runs" in tables
    assert "llm_calls" in tables
    assert "tool_calls" in tables
    assert "schema_version" in tables


def test_migrate_v9_repairs_missing_column_from_wedged_v7(monkeypatch):
    """Regression: if an earlier process wedged the DB by marking
    schema_version=7 without actually adding ``llm_calls.provider_assumed``
    (the v7 ALTER was swallowed by a transient SQLITE_LOCKED under the old
    blanket-except code), the next connect must self-heal via v9 — not crash
    every ``record_llm_call`` with ``no such column: provider_assumed``."""
    conn = db._get_conn()

    # Simulate the wedged state: drop the column added by v7, while leaving
    # schema_version=7 (and 8, 9) in place. SQLite has no DROP COLUMN before
    # 3.35; instead we rebuild the table without the column to mimic the
    # production state observed in the field.
    conn.execute("ALTER TABLE llm_calls RENAME TO _llm_calls_old")
    conn.execute("""
        CREATE TABLE llm_calls (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          TEXT NOT NULL,
            ts                  TEXT NOT NULL,
            model               TEXT,
            provider            TEXT DEFAULT '',
            tokens_in           INTEGER DEFAULT 0,
            tokens_out          INTEGER DEFAULT 0,
            cost_usd            REAL DEFAULT 0,
            latency_ms          INTEGER DEFAULT 0,
            cache_read_tokens   INTEGER DEFAULT 0,
            cache_write_tokens  INTEGER DEFAULT 0,
            reasoning_tokens    INTEGER DEFAULT 0,
            estimated           INTEGER DEFAULT 0
        )
    """)
    conn.execute("DROP TABLE _llm_calls_old")
    # Force v9+ to re-run by deleting their markers (mimics a DB that predates
    # v9). The rebuilt table above also lacks v10's moa_preset column, so v10
    # must re-run too — otherwise record_llm_call below hits "no such column:
    # moa_preset" against a table stuck in the pre-v10 shape.
    conn.execute("DELETE FROM schema_version WHERE version >= 9")

    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('llm_calls')")}
    assert "provider_assumed" not in cols  # wedged state confirmed

    # Re-run migrations: v9 must self-heal
    db._ensure_schema(conn)

    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('llm_calls')")}
    assert "provider_assumed" in cols, "v9 must re-add provider_assumed when v7 silently skipped it"

    # And record_llm_call must now succeed end-to-end
    db.record_llm_call(
        session_id="repair-test",
        ts="2026-06-20T20:00:00+00:00",
        model="deepseek-chat",
        provider="deepseek",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.001,
        latency_ms=500,
        provider_assumed=False,
    )
    row = conn.execute(
        "SELECT tokens_in, tokens_out, provider_assumed FROM llm_calls WHERE session_id = 'repair-test'"
    ).fetchone()
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["provider_assumed"] == 0


def test_alter_failure_does_not_mark_migration_applied(monkeypatch):
    """Regression: if ALTER TABLE raises a non-"duplicate column" error
    (e.g. SQLITE_LOCKED from cross-process contention), the surrounding
    migration must NOT write its schema_version row — otherwise the next
    connect skips the migration permanently and the column never lands."""
    import sqlite3 as _sqlite3

    conn = db._get_conn()
    # Pretend we're a pre-v7 DB that just finished v6.
    conn.execute("DELETE FROM schema_version WHERE version >= 7")
    # Drop columns added by v7 to mimic a fresh-from-v6 DB.
    conn.execute("ALTER TABLE llm_calls RENAME TO _old")
    conn.execute("""
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            model TEXT,
            provider TEXT DEFAULT '',
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            estimated INTEGER DEFAULT 0
        )
    """)
    conn.execute("DROP TABLE _old")

    # Force the ALTER inside _add_column_if_missing to raise SQLITE_LOCKED.
    original = db._add_column_if_missing
    call_count = {"n": 0}

    def flaky(conn_, table, column, typedef):
        call_count["n"] += 1
        if call_count["n"] == 1 and column == "provider_assumed":
            raise _sqlite3.OperationalError("database is locked")
        return original(conn_, table, column, typedef)

    monkeypatch.setattr(db, "_add_column_if_missing", flaky)

    with pytest.raises(_sqlite3.OperationalError, match="locked"):
        db._migrate_v7(conn)

    # Crucial: v7 must NOT be marked applied, so the next connect retries.
    v7 = conn.execute("SELECT version FROM schema_version WHERE version = 7").fetchone()
    assert v7 is None, "migration must not be marked applied if ALTER failed transiently"


def test_schema_idempotent():
    """Calling _ensure_schema twice must not raise or duplicate version rows.

    There is exactly one row per applied migration (v1..v_SCHEMA_VERSION).
    Repeated calls must not add more rows.
    """
    conn = db._get_conn()
    # Called once already by _get_conn(); call twice more
    db._ensure_schema(conn)
    db._ensure_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    # One row per schema version
    assert count == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# start_run / end_run
# ---------------------------------------------------------------------------


def test_start_run_creates_row():
    db.start_run("sess-1", model="claude-sonnet", platform="cli")
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM runs WHERE session_id = 'sess-1'").fetchone()
    assert row is not None
    assert row["status"] == "running"
    assert row["platform"] == "cli"
    assert row["model"] == "claude-sonnet"
    assert row["cron_job_id"] is None


def test_start_run_with_cron_job_id():
    db.start_run(
        "cron_abc123_20260601_120000", model="gpt-4o", platform="cron", cron_job_id="abc123"
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT * FROM runs WHERE session_id = 'cron_abc123_20260601_120000'"
    ).fetchone()
    assert row["cron_job_id"] == "abc123"
    assert row["platform"] == "cron"


def test_start_run_idempotent():
    """Second INSERT OR IGNORE must not raise."""
    db.start_run("sess-dup", model="m", platform="cli")
    db.start_run("sess-dup", model="m", platform="cli")
    conn = db._get_conn()
    count = conn.execute("SELECT COUNT(*) FROM runs WHERE session_id='sess-dup'").fetchone()[0]
    assert count == 1


def test_end_run_sets_status():
    db.start_run("sess-end", model="m", platform="cli")
    db.end_run("sess-end", status="ok")
    conn = db._get_conn()
    row = conn.execute(
        "SELECT status, ended_at, duration_ms FROM runs WHERE session_id='sess-end'"
    ).fetchone()
    assert row["status"] == "ok"
    assert row["ended_at"] is not None
    assert row["duration_ms"] is not None
    assert row["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# record_llm_call
# ---------------------------------------------------------------------------


def test_record_llm_call_writes_row():
    db.start_run("sess-llm", model="gpt-4o", platform="cli")
    db.record_llm_call(
        "sess-llm", "2026-01-01T00:00:00+00:00", "gpt-4o", "openai", 1000, 200, 0.003, 500
    )
    conn = db._get_conn()
    rows = conn.execute("SELECT * FROM llm_calls WHERE session_id='sess-llm'").fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["tokens_in"] == 1000
    assert r["tokens_out"] == 200
    assert abs(r["cost_usd"] - 0.003) < 1e-9
    assert r["latency_ms"] == 500


def test_record_llm_call_accumulates_in_runs():
    db.start_run("sess-accum", model="gpt-4o", platform="cli")
    db.record_llm_call(
        "sess-accum", "2026-01-01T00:00:00+00:00", "gpt-4o", "openai", 500, 100, 0.001, 300
    )
    db.record_llm_call(
        "sess-accum", "2026-01-01T00:01:00+00:00", "gpt-4o", "openai", 500, 100, 0.001, 300
    )
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM runs WHERE session_id='sess-accum'").fetchone()
    assert row["tokens_in"] == 1000
    assert row["tokens_out"] == 200
    assert abs(row["cost_usd"] - 0.002) < 1e-9
    assert row["api_calls"] == 2


# ---------------------------------------------------------------------------
# record_tool_call
# ---------------------------------------------------------------------------


def test_record_tool_call_ok():
    db.start_run("sess-tool", model="m", platform="cli")
    db.record_tool_call("sess-tool", "2026-01-01T00:00:00+00:00", "read_file", True, 42)
    conn = db._get_conn()
    rows = conn.execute("SELECT * FROM tool_calls WHERE session_id='sess-tool'").fetchall()
    assert len(rows) == 1
    assert rows[0]["ok"] == 1
    assert rows[0]["latency_ms"] == 42
    # Counter on runs
    row = conn.execute("SELECT tool_calls FROM runs WHERE session_id='sess-tool'").fetchone()
    assert row["tool_calls"] == 1


def test_record_tool_call_failure():
    db.start_run("sess-tool-fail", model="m", platform="cli")
    db.record_tool_call("sess-tool-fail", "2026-01-01T00:00:00+00:00", "bash", False, 1000)
    conn = db._get_conn()
    row = conn.execute("SELECT ok FROM tool_calls WHERE session_id='sess-tool-fail'").fetchone()
    assert row["ok"] == 0


# ---------------------------------------------------------------------------
# Aggregation queries
# ---------------------------------------------------------------------------


def _seed_data():
    """Insert a minimal dataset for aggregation tests."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)

    for i in range(3):
        sid = f"sess-agg-{i}"
        db.start_run(sid, model="gpt-4o", platform="cli")
        db.record_llm_call(sid, now.isoformat(), "gpt-4o", "openai", 1000, 200, 0.003, 400)
        db.record_tool_call(sid, now.isoformat(), "read_file", True, 50)
        db.end_run(sid, "ok")

    # One cron run
    db.start_run(
        "cron_job1_20260601_120000", model="claude-sonnet", platform="cron", cron_job_id="job1"
    )
    db.record_llm_call(
        "cron_job1_20260601_120000",
        now.isoformat(),
        "claude-sonnet",
        "anthropic",
        2000,
        500,
        0.01,
        800,
    )
    db.end_run("cron_job1_20260601_120000", "ok")


def test_stats_summary_counts():
    _seed_data()
    s = db.stats_summary(window_hours=24)
    assert s["total_runs"] == 4
    assert s["ok_runs"] == 4
    assert s["api_calls"] == 4
    assert s["tokens_in"] == 3 * 1000 + 2000
    assert s["tokens_out"] == 3 * 200 + 500


def test_cost_by_job_returns_cron():
    _seed_data()
    rows = db.cost_by_job(window_hours=168)
    assert len(rows) == 1
    assert rows[0]["cron_job_id"] == "job1"
    assert rows[0]["ok_runs"] == 1
    assert abs(rows[0]["cost_usd"] - 0.01) < 1e-9


def test_recent_runs_ordered():
    _seed_data()
    runs = db.recent_runs(limit=10)
    assert len(runs) == 4
    # Most recent first
    for i in range(len(runs) - 1):
        assert runs[i]["started_at"] >= runs[i + 1]["started_at"]


# ---------------------------------------------------------------------------
# Concurrency: N threads writing simultaneously must not corrupt/lose rows
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Schema v2 columns (Refinement 2)
# ---------------------------------------------------------------------------


def test_schema_v2_columns():
    conn = db._get_conn()
    # Check llm_calls columns
    llm_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert "cache_read_tokens" in llm_cols
    assert "cache_write_tokens" in llm_cols
    assert "reasoning_tokens" in llm_cols
    assert "estimated" in llm_cols
    # Check runs columns
    runs_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "parent_session_id" in runs_cols
    assert "estimated_llm_calls" in runs_cols


def test_schema_v7_provider_assumed_columns():
    """v7 adds provider_assumed on llm_calls and provider_assumed_calls on runs."""
    conn = db._get_conn()
    llm_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert "provider_assumed" in llm_cols
    runs_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "provider_assumed_calls" in runs_cols


def test_record_llm_call_provider_assumed():
    """provider_assumed=True stores the flag and increments the runs counter."""
    db.start_run("sess-asm", model="moonshotai/kimi-k2.6", platform="cli")
    db.record_llm_call(
        "sess-asm",
        "2026-01-01T00:00:00+00:00",
        "moonshotai/kimi-k2.6",
        "nous",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0004,
        latency_ms=100,
        provider_assumed=True,
    )
    conn = db._get_conn()
    call = conn.execute(
        "SELECT provider_assumed FROM llm_calls WHERE session_id='sess-asm'"
    ).fetchone()
    assert call["provider_assumed"] == 1
    run = conn.execute(
        "SELECT provider_assumed_calls FROM runs WHERE session_id='sess-asm'"
    ).fetchone()
    assert run["provider_assumed_calls"] == 1

    # A second non-assumed call must not increment the counter.
    db.record_llm_call(
        "sess-asm",
        "2026-01-01T00:01:00+00:00",
        "moonshotai/kimi-k2.6",
        "nous",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0004,
        latency_ms=100,
        provider_assumed=False,
    )
    run = conn.execute(
        "SELECT provider_assumed_calls FROM runs WHERE session_id='sess-asm'"
    ).fetchone()
    assert run["provider_assumed_calls"] == 1


def test_record_llm_call_default_provider_assumed_zero():
    """A normal call leaves provider_assumed at 0 (default)."""
    db.start_run("sess-asm0", model="claude-sonnet-4-6", platform="cli")
    db.record_llm_call(
        "sess-asm0",
        "2026-01-01T00:00:00+00:00",
        "claude-sonnet-4-6",
        "anthropic",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.001,
        latency_ms=100,
    )
    conn = db._get_conn()
    call = conn.execute(
        "SELECT provider_assumed FROM llm_calls WHERE session_id='sess-asm0'"
    ).fetchone()
    assert call["provider_assumed"] == 0


def test_stats_by_provider_exposes_provider_assumed():
    """stats_by_provider reports provider_assumed_calls and provider_assumed_pct."""
    db.start_run("sess-pp", model="moonshotai/kimi-k2.6", platform="cli")
    # 1 assumed + 1 real under the same provider → pct = 0.5
    db.record_llm_call(
        "sess-pp",
        db._utcnow(),
        "moonshotai/kimi-k2.6",
        "nous",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0004,
        latency_ms=100,
        provider_assumed=True,
    )
    db.record_llm_call(
        "sess-pp",
        db._utcnow(),
        "claude-sonnet-4-6",
        "nous",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.001,
        latency_ms=100,
        provider_assumed=False,
    )
    rows = {r["provider"]: r for r in db.stats_by_provider(window_hours=24)}
    assert "nous" in rows
    assert rows["nous"]["provider_assumed_calls"] == 1
    assert abs(rows["nous"]["provider_assumed_pct"] - 0.5) < 1e-9


def test_record_llm_call_with_cache_tokens():
    db.start_run("sess-cache", model="claude-sonnet-4-6", platform="cli")
    db.record_llm_call(
        "sess-cache",
        "2026-01-01T00:00:00+00:00",
        "claude-sonnet-4-6",
        "anthropic",
        tokens_in=1000,
        tokens_out=200,
        cost_usd=0.004,
        latency_ms=300,
        cache_read_tokens=1000,
        cache_write_tokens=500,
    )
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM llm_calls WHERE session_id='sess-cache'").fetchone()
    assert row["cache_read_tokens"] == 1000
    assert row["cache_write_tokens"] == 500
    assert row["reasoning_tokens"] == 0
    assert row["estimated"] == 0


def test_record_llm_call_estimated():
    """estimated=True increments estimated_llm_calls on the runs row."""
    db.start_run("sess-est", model="gpt-4o", platform="cli")
    db.record_llm_call(
        "sess-est",
        "2026-01-01T00:00:00+00:00",
        "gpt-4o",
        "openai",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.001,
        latency_ms=100,
        estimated=True,
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT estimated_llm_calls FROM runs WHERE session_id='sess-est'"
    ).fetchone()
    assert row["estimated_llm_calls"] == 1

    # A second non-estimated call should not increment
    db.record_llm_call(
        "sess-est",
        "2026-01-01T00:01:00+00:00",
        "gpt-4o",
        "openai",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.001,
        latency_ms=100,
        estimated=False,
    )
    row = conn.execute(
        "SELECT estimated_llm_calls FROM runs WHERE session_id='sess-est'"
    ).fetchone()
    assert row["estimated_llm_calls"] == 1


def test_stats_summary_estimated_percentage():
    """Seed 2 real + 1 estimated calls; stats_summary should report correct estimated_llm_calls."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    db.start_run("sess-mix", model="gpt-4o", platform="cli")
    # Two real calls
    db.record_llm_call("sess-mix", now, "gpt-4o", "openai", 100, 50, 0.001, 100, estimated=False)
    db.record_llm_call("sess-mix", now, "gpt-4o", "openai", 100, 50, 0.001, 100, estimated=False)
    # One estimated call
    db.record_llm_call("sess-mix", now, "gpt-4o", "openai", 50, 20, 0.0005, 80, estimated=True)
    db.end_run("sess-mix", "ok")

    s = db.stats_summary(window_hours=24)
    assert s["api_calls"] == 3
    assert s["estimated_llm_calls"] == 1


# ---------------------------------------------------------------------------
# Concurrency: N threads writing simultaneously must not corrupt/lose rows
# ---------------------------------------------------------------------------


def test_concurrent_writes(tmp_path, monkeypatch):
    """WAL + per-thread connections — N concurrent writers, no corruption."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import hermes_telemetry.db as db_mod

    errors = []
    N_THREADS = 5
    N_WRITES_PER_THREAD = 3
    MAX_RETRIES = 3

    def worker(thread_idx: int) -> None:
        # Each thread has its own _local, so it will create its own connection
        db_mod._local.conn = None  # ensure fresh connection per thread
        import datetime
        import time

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            for j in range(N_WRITES_PER_THREAD):
                sid = f"t{thread_idx}-j{j}"
                for attempt in range(MAX_RETRIES):
                    try:
                        db_mod.start_run(
                            sid, model="m", platform="cron", cron_job_id=f"job{thread_idx}"
                        )
                        db_mod.record_llm_call(sid, now, "m", "p", 100, 50, 0.001, 200)
                        db_mod.record_tool_call(sid, now, "tool", True, 10)
                        db_mod.end_run(sid, "ok")
                        break  # success, no retry needed
                    except Exception as exc:
                        if "locked" in str(exc).lower() and attempt < MAX_RETRIES - 1:
                            time.sleep(0.1 * (attempt + 1))
                            continue
                        raise
        except Exception as exc:
            errors.append(str(exc))
        finally:
            db_mod.close_thread_conn()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent write errors: {errors}"

    # Use a fresh connection to verify row counts
    db_mod._local.conn = None
    expected = N_THREADS * N_WRITES_PER_THREAD
    conn = db_mod._get_conn()
    run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    llm_count = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    tool_count = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    assert run_count == expected, f"Expected {expected} runs, got {run_count}"
    assert llm_count == expected, f"Expected {expected} llm_calls, got {llm_count}"
    assert tool_count == expected, f"Expected {expected} tool_calls, got {tool_count}"
    db_mod.close_thread_conn()


# ---------------------------------------------------------------------------
# Schema v3: sender_id + budget_alerts + budget queries
# ---------------------------------------------------------------------------


def test_schema_v3_columns_and_table():
    conn = db._get_conn()
    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    assert "sender_id" in run_cols
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "budget_alerts" in tables


def test_set_sender_first_wins():
    db.start_run("s1", model="m", platform="telegram")
    db.set_sender("s1", "alice")
    db.set_sender("s1", "bob")  # ignored — first non-null wins
    assert db.get_run("s1")["sender_id"] == "alice"


def test_set_sender_ignores_empty():
    db.start_run("s1", model="m", platform="cli")
    db.set_sender("s1", "")
    assert db.get_run("s1")["sender_id"] is None


def test_spend_by_scope_global_and_cron():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cron", cron_job_id="job1")
    db.record_llm_call("s1", now, "m", "p", 0, 0, 1.50, 0)
    db.start_run("s2", model="m", platform="cli")
    db.record_llm_call("s2", now, "m", "p", 0, 0, 0.50, 0)

    past = "2000-01-01T00:00:00+00:00"
    g = db.spend_by_scope("global", "", past)
    assert abs(g["spent_usd"] - 2.00) < 1e-9
    j = db.spend_by_scope("cron_job", "job1", past)
    assert abs(j["spent_usd"] - 1.50) < 1e-9


def test_spend_by_scope_estimated_pct():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "m", "p", 0, 0, 1.0, 0, estimated=True)
    db.record_llm_call("s1", now, "m", "p", 0, 0, 1.0, 0, estimated=False)
    past = "2000-01-01T00:00:00+00:00"
    s = db.spend_by_scope("global", "", past)
    assert s["total_calls"] == 2
    assert s["estimated_calls"] == 1
    assert abs(s["estimated_pct"] - 0.5) < 1e-9


def test_estimated_price_share_cron_job_includes_subagent_subtree():
    """estimated_price_share("cron_job", ...) must resolve the same delegation
    subtree as spend_by_scope. A delegated child (cron_job_id NULL) that uses
    an estimated-price model has to count toward the parent cron job's share —
    otherwise budget.py never degrades a HARD verdict driven by that child's
    spend to SOFT, and the job hard-pauses on pricing the system itself deems
    unreliable."""
    import yaml

    pricing_file = db._get_db_path().parent / "pricing.yaml"
    pricing_file.write_text(
        yaml.safe_dump(
            {
                "models": {},
                "_meta": {"estimated_price_models": ["some/estimated-model"]},
            }
        )
    )

    db.start_run("cron_job1_20260601_020000", model="m", platform="cron", cron_job_id="job1")
    db.start_run("child-est-1", model="m", platform="cli")
    db.record_subagent_start(
        child_session_id="child-est-1", parent_session_id="cron_job1_20260601_020000"
    )
    db.record_llm_call(
        "child-est-1", db._utcnow(), "some/estimated-model", "openrouter", 100, 50, 0.02, 100
    )

    past = "2000-01-01T00:00:00+00:00"
    assert db.estimated_price_share("cron_job", "job1", past) > 0.0


def test_try_budget_alert_is_idempotent():
    first = db.try_budget_alert("global", "", "daily", "2026-05-31", "soft", 4.0, 5.0)
    second = db.try_budget_alert("global", "", "daily", "2026-05-31", "soft", 4.0, 5.0)
    assert first is True
    assert second is False
    # A different level is a distinct alert
    assert db.try_budget_alert("global", "", "daily", "2026-05-31", "hard", 5.0, 5.0) is True


# ---------------------------------------------------------------------------
# Orphan-session recovery (issue #3)
#
# Some callers fire post_api_request / on_session_end without an earlier
# on_session_start having reached the plugin. This happens for any chat
# platform session that pre-existed the plugin enable, and for sessions
# resumed across gateway restarts via resume_pending (see hermes-agent
# gateway/session.py:889 — "Restart-interrupted session: preserve the
# session_id"). The plugin must still aggregate those calls under a runs
# row instead of silently no-op'ing.
# ---------------------------------------------------------------------------


def test_record_llm_call_lazy_creates_runs_row_when_start_missed():
    """record_llm_call for an unknown session must create a 'running' row
    and aggregate against it — not silently UPDATE WHERE no row matches."""
    db.record_llm_call(
        "orphan-session",
        "2026-06-05T12:00:00+00:00",
        "gemini-3-flash-preview",
        "gemini",
        100,
        50,
        0.001,
        200,
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT session_id, status, tokens_in, tokens_out, cost_usd, api_calls, model, provider "
        "FROM runs WHERE session_id = ?",
        ("orphan-session",),
    ).fetchone()
    assert row is not None
    assert dict(row) == {
        "session_id": "orphan-session",
        "status": "running",
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_usd": 0.001,
        "api_calls": 1,
        "model": "gemini-3-flash-preview",
        "provider": "gemini",
    }


def test_record_llm_call_does_not_duplicate_with_explicit_start_run():
    """When on_session_start did fire normally, the lazy INSERT OR IGNORE in
    record_llm_call must be a no-op — same row, aggregated correctly."""
    db.start_run("happy-path", model="gemini-3-flash-preview", platform="cli")
    db.record_llm_call(
        "happy-path",
        "2026-06-05T12:00:00+00:00",
        "gemini-3-flash-preview",
        "gemini",
        100,
        50,
        0.001,
        200,
    )
    conn = db._get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE session_id = ?", ("happy-path",)
    ).fetchone()[0]
    assert count == 1
    row = conn.execute(
        "SELECT platform, tokens_in, api_calls FROM runs WHERE session_id = ?", ("happy-path",)
    ).fetchone()
    # platform was set by start_run and preserved (not overwritten by lazy insert)
    assert row["platform"] == "cli"
    assert row["tokens_in"] == 100
    assert row["api_calls"] == 1


def test_end_run_lazy_creates_row_when_start_missed():
    """end_run for an unknown session_id (no prior start_run, no prior
    record_llm_call) still records a closing row. Defensive guard against
    rare hook ordering — Nadia's review case 4."""
    db.end_run("never-started", status="ok")
    conn = db._get_conn()
    row = conn.execute(
        "SELECT session_id, status, ended_at IS NOT NULL AS has_end FROM runs WHERE session_id = ?",
        ("never-started",),
    ).fetchone()
    assert row is not None
    assert row["status"] == "ok"
    assert row["has_end"] == 1


def test_chat_bot_session_aggregates_after_orphan_recovery():
    """Regression: chat-bot scenario — plugin enabled mid-session.
    Multiple record_llm_call for the same orphan session_id, then a
    per-turn end_run closes the run cleanly. /stats must see the
    aggregated cost.
    """
    sid = "20260605_074023_3cd3bf2a"  # mimics the production session id format

    # Three turns of a Telegram bot — no preceding start_run because the
    # session pre-dated the plugin install.
    db.record_llm_call(
        sid,
        "2026-06-05T17:35:48+00:00",
        "gemini-3-flash-preview",
        "gemini",
        29912,
        19,
        0.024799,
        2575,
    )
    db.record_llm_call(
        sid,
        "2026-06-05T17:41:39+00:00",
        "gemini-3-flash-preview",
        "gemini",
        10808,
        39,
        0.005534,
        2359,
    )
    db.record_llm_call(
        sid,
        "2026-06-05T17:48:30+00:00",
        "gemini-3-flash-preview",
        "gemini",
        49285,
        52,
        0.024799,
        1840,
    )

    # Hermes fires on_session_end per-turn (agent/conversation_loop.py:4870)
    db.end_run(sid, status="ok")

    # /stats reads from runs — must see the aggregated chat-bot cost
    conn = db._get_conn()
    row = conn.execute(
        "SELECT tokens_in, tokens_out, api_calls, cost_usd, status FROM runs WHERE session_id = ?",
        (sid,),
    ).fetchone()
    assert row is not None
    assert row["api_calls"] == 3
    assert row["tokens_in"] == 90005
    assert row["tokens_out"] == 110
    assert abs(row["cost_usd"] - 0.055132) < 1e-6
    assert row["status"] == "ok"


def test_orphan_session_appears_in_stats_summary():
    """End-to-end: orphan-recovered session shows up in stats_summary
    aggregates (previously invisible). 24h window includes recent insert."""
    db.record_llm_call(
        "orphan-stats", db._utcnow(), "gemini-3-flash-preview", "gemini", 1000, 200, 0.005, 500
    )
    db.end_run("orphan-stats", status="ok")

    summary = db.stats_summary(window_hours=24)
    assert summary["total_runs"] >= 1
    assert summary["tokens_in"] >= 1000
    assert summary["cost_usd"] >= 0.005


# ---------------------------------------------------------------------------
# known_free_models — free→paid tracking (issue #16)
# ---------------------------------------------------------------------------


def test_record_and_detect_known_free_model():
    assert not db.is_known_free_model("owl-alpha", "nous")
    db.record_free_model("owl-alpha", "nous")
    assert db.is_known_free_model("owl-alpha", "nous")


def test_record_free_model_is_idempotent():
    db.record_free_model("owl-alpha", "nous")
    db.record_free_model("owl-alpha", "nous")  # should not raise
    assert db.is_known_free_model("owl-alpha", "nous")


def test_known_free_model_is_provider_scoped():
    db.record_free_model("owl-alpha", "nous")
    # Specific-provider row does NOT match other providers (no wildcard row present)
    assert not db.is_known_free_model("owl-alpha", "openrouter")
    assert not db.is_known_free_model("owl-alpha", "")


def test_unknown_model_is_not_known_free():
    assert not db.is_known_free_model("completely-unknown-model-xyz", "nvidia")


def test_schema_v5_recorded():
    from db import _get_conn

    versions = {row[0] for row in _get_conn().execute("SELECT version FROM schema_version")}
    assert 5 in versions


# ---------------------------------------------------------------------------
# backfill_known_free_models — backward-compat wildcard rows (issue #16)
# ---------------------------------------------------------------------------


def test_backfill_inserts_wildcard_rows():
    """backfill_known_free_models inserts rows with provider=''."""
    n = db.backfill_known_free_models(["owl-alpha", "hermes-4-qwen"])
    assert n == 2
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT provider FROM known_free_models WHERE model IN ('owl-alpha', 'hermes-4-qwen')"
    ).fetchall()
    assert all(r[0] == "" for r in rows)


def test_backfill_wildcard_matches_any_provider():
    """After backfill, is_known_free_model returns True regardless of provider."""
    db.backfill_known_free_models(["owl-alpha"])
    assert db.is_known_free_model("owl-alpha", "nous")
    assert db.is_known_free_model("owl-alpha", "openrouter")
    assert db.is_known_free_model("owl-alpha", "nvidia")
    assert db.is_known_free_model("owl-alpha", "")


def test_backfill_is_idempotent():
    """Second backfill call returns 0 — INSERT OR IGNORE prevents duplicates."""
    assert db.backfill_known_free_models(["owl-alpha"]) == 1
    assert db.backfill_known_free_models(["owl-alpha"]) == 0


def test_backfill_does_not_overwrite_specific_provider_row():
    """Backfill and record_free_model coexist — separate PRIMARY KEY rows."""
    db.record_free_model("owl-alpha", "nous")
    db.backfill_known_free_models(["owl-alpha"])
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT provider FROM known_free_models WHERE model = 'owl-alpha'"
    ).fetchall()
    providers = {r[0] for r in rows}
    assert providers == {"nous", ""}


# ---------------------------------------------------------------------------
# is_free_tier_transition — id-change detection (issue #32)
# ---------------------------------------------------------------------------


def test_free_tier_transition_bare_id_rename():
    """Paid call under the bare id matches the stored `<id>:free` row."""
    db.record_free_model("nvidia/nemotron-3-ultra:free", "nvidia")
    # `:free` suffix dropped — paid call arrives as the bare id
    assert db.is_free_tier_transition("nvidia/nemotron-3-ultra", "nvidia")


def test_free_tier_transition_suffixed_paid_id():
    """Paid call under a `<base>-…` suffixed id matches the stored `<base>:free`."""
    db.record_free_model("nvidia/nemotron-3-ultra:free", "nvidia")
    assert db.is_free_tier_transition("nvidia/nemotron-3-ultra-550b-a55b", "nvidia")


# ---------------------------------------------------------------------------
# free_paid_transitions — historical record for the dashboard alerts slot
# ---------------------------------------------------------------------------


def test_schema_v6_recorded():
    from db import _get_conn

    versions = {row[0] for row in _get_conn().execute("SELECT version FROM schema_version")}
    assert 6 in versions


def test_record_free_paid_transition_persists_row():
    db.record_free_model("owl-alpha", "nous")
    db.record_free_paid_transition("owl-alpha", "nous", "sess-1", 0.25)
    rows = db.recent_free_paid_transitions(window_hours=0)
    assert len(rows) == 1
    assert rows[0]["model"] == "owl-alpha"
    assert rows[0]["provider"] == "nous"
    assert rows[0]["session_id"] == "sess-1"
    assert abs(rows[0]["first_paid_cost_usd"] - 0.25) < 1e-9
    # first_free_seen_at copied from the known_free_models row
    assert rows[0]["first_free_seen_at"] is not None


def test_record_free_paid_transition_is_idempotent_per_model():
    db.record_free_model("owl-alpha", "nous")
    db.record_free_paid_transition("owl-alpha", "nous", "sess-1", 0.25)
    db.record_free_paid_transition("owl-alpha", "nous", "sess-2", 9.99)
    rows = db.recent_free_paid_transitions(window_hours=0)
    assert len(rows) == 1
    # First flip wins — second call is a no-op.
    assert rows[0]["session_id"] == "sess-1"
    assert abs(rows[0]["first_paid_cost_usd"] - 0.25) < 1e-9


def test_recent_free_paid_transitions_window_filter():
    """Rows older than the window are excluded; window<=0 returns everything."""
    db.record_free_paid_transition("recent-model", "p", "s", 0.1)
    # Backdate one row to 100h ago — outside a 72h window. Use the same module
    # object the autouse fixture resets, not a standalone `from db import …`.
    db._get_conn().execute(
        "INSERT INTO free_paid_transitions"
        "(model, provider, detected_at, session_id, first_paid_cost_usd, first_free_seen_at)"
        " VALUES (?, ?, datetime('now', '-100 hours'), ?, ?, NULL)",
        ("old-model", "p", "s", 0.1),
    )
    recent = db.recent_free_paid_transitions(window_hours=72)
    assert [r["model"] for r in recent] == ["recent-model"]
    full = db.recent_free_paid_transitions(window_hours=0)
    assert {r["model"] for r in full} == {"recent-model", "old-model"}


def test_free_tier_transition_matches_wildcard_provider():
    """Backfilled provider='' rows match a transition under any provider."""
    db.backfill_known_free_models(["nvidia/nemotron-3-ultra:free"])
    assert db.is_free_tier_transition("nvidia/nemotron-3-ultra", "nvidia")
    assert db.is_free_tier_transition("nvidia/nemotron-3-ultra-550b-a55b", "openrouter")


def test_free_tier_transition_ignores_unrelated_model():
    """An unrelated paid model does not false-positive off a `:free` row."""
    db.record_free_model("nvidia/nemotron-3-ultra:free", "nvidia")
    assert not db.is_free_tier_transition("nvidia/nemotron-super-49b", "nvidia")


def test_free_tier_transition_requires_token_boundary():
    """Prefix match only at a separator — `…-ultraX` must not match `…-ultra:free`."""
    db.record_free_model("nvidia/nemotron-3-ultra:free", "nvidia")
    # No separator after the base → not a transition
    assert not db.is_free_tier_transition("nvidia/nemotron-3-ultrablend", "nvidia")


def test_free_tier_transition_false_without_free_row():
    """No `:free` row recorded → never a transition."""
    assert not db.is_free_tier_transition("nvidia/nemotron-3-ultra", "nvidia")


def test_free_tier_transition_provider_scoped():
    """A specific-provider `:free` row does not match a different provider."""
    db.record_free_model("nvidia/nemotron-3-ultra:free", "nvidia")
    assert not db.is_free_tier_transition("nvidia/nemotron-3-ultra", "openrouter")


# ---------------------------------------------------------------------------
# model_unavailable_alerts — issue #43
# ---------------------------------------------------------------------------


def test_model_unavailable_insert_first_occurrence():
    """First 404 for a (model, provider) inserts a row with occurrences=1."""
    db.record_model_unavailable("nvidia/foo:free", "nous", 404, "Model not found")
    row = db.get_model_unavailable("nvidia/foo:free", "nous")
    assert row is not None
    assert row["model"] == "nvidia/foo:free"
    assert row["provider"] == "nous"
    assert row["error_code"] == 404
    assert row["error_message"] == "Model not found"
    assert row["occurrences"] == 1
    assert row["first_seen_at"] == row["last_seen_at"]


def test_model_unavailable_increments_on_repeat():
    """Repeated 404s for the same (model, provider) bump occurrences without
    duplicating rows; first_seen_at is preserved, last_seen_at advances."""
    db.record_model_unavailable("nvidia/foo:free", "nous", 404, "first")
    first_row = db.get_model_unavailable("nvidia/foo:free", "nous")
    first_seen = first_row["first_seen_at"]

    db.record_model_unavailable("nvidia/foo:free", "nous", 404, "second")
    db.record_model_unavailable("nvidia/foo:free", "nous", 404, "third")

    row = db.get_model_unavailable("nvidia/foo:free", "nous")
    assert row["occurrences"] == 3
    assert row["first_seen_at"] == first_seen  # never overwritten
    assert row["last_seen_at"] >= first_seen
    # Latest message wins so the user sees the most recent diagnostic
    assert row["error_message"] == "third"

    # And there's still exactly one row for this pair
    only = db.recent_model_unavailable(window_hours=0)
    assert len([r for r in only if r["model"] == "nvidia/foo:free"]) == 1


def test_model_unavailable_provider_scoped():
    """Same model on different providers are independent rows."""
    db.record_model_unavailable("foo:free", "nous", 404, "a")
    db.record_model_unavailable("foo:free", "openrouter", 404, "b")
    nous_row = db.get_model_unavailable("foo:free", "nous")
    or_row = db.get_model_unavailable("foo:free", "openrouter")
    assert nous_row is not None and or_row is not None
    assert nous_row["error_message"] == "a"
    assert or_row["error_message"] == "b"
    assert nous_row["occurrences"] == 1
    assert or_row["occurrences"] == 1


def test_model_unavailable_get_missing_returns_none():
    """Lookup for a (model, provider) that never 404'd returns None."""
    assert db.get_model_unavailable("never-seen", "nous") is None


def test_model_unavailable_recent_orders_by_last_seen_desc():
    """recent_model_unavailable returns newest last_seen_at first."""
    # Older row, hand-crafted timestamps to avoid relying on time.sleep
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO model_unavailable_alerts"
        "(model, provider, error_code, error_message,"
        " first_seen_at, last_seen_at, occurrences)"
        " VALUES (?, ?, 404, 'old', datetime('now', '-100 hours'),"
        "         datetime('now', '-100 hours'), 1)",
        ("old-model", "nous"),
    )
    db.record_model_unavailable("new-model", "nous", 404, "new")

    full = db.recent_model_unavailable(window_hours=0)
    assert [r["model"] for r in full[:2]] == ["new-model", "old-model"]

    # window_hours filter excludes the 100h-old row
    recent = db.recent_model_unavailable(window_hours=72)
    assert [r["model"] for r in recent] == ["new-model"]


def test_model_unavailable_truncated_message_roundtrip():
    """A long error_message is stored and retrieved verbatim (truncation is the
    caller's responsibility — the column has no length limit, but the plugin
    side caps at 500 chars to keep the table tidy)."""
    long_msg = "x" * 500
    db.record_model_unavailable("m", "p", 404, long_msg)
    row = db.get_model_unavailable("m", "p")
    assert row["error_message"] == long_msg


# ---------------------------------------------------------------------------
# v10 — MoA (Mixture-of-Agents) attribution
# ---------------------------------------------------------------------------


def test_schema_v10_moa_columns():
    """v10 adds moa_preset on llm_calls and moa_calls on runs."""
    conn = db._get_conn()
    llm_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
    assert "moa_preset" in llm_cols
    runs_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "moa_calls" in runs_cols


def test_schema_v10_recorded():
    versions = {row[0] for row in db._get_conn().execute("SELECT version FROM schema_version")}
    assert 10 in versions


def test_record_llm_call_moa_preset():
    """moa_preset is stored on the call and increments runs.moa_calls; a
    non-MoA call leaves the counter untouched."""
    db.start_run("sess-moa", model="anthropic/claude-opus-4.8", platform="cli")
    db.record_llm_call(
        "sess-moa",
        "2026-01-01T00:00:00+00:00",
        "anthropic/claude-opus-4.8",
        "openrouter",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        latency_ms=200,
        moa_preset="default",
    )
    conn = db._get_conn()
    call = conn.execute(
        "SELECT provider, model, moa_preset FROM llm_calls WHERE session_id='sess-moa'"
    ).fetchone()
    # Attributed to the real aggregator provider/model, not "moa"/"default".
    assert call["provider"] == "openrouter"
    assert call["model"] == "anthropic/claude-opus-4.8"
    assert call["moa_preset"] == "default"
    run = conn.execute("SELECT moa_calls FROM runs WHERE session_id='sess-moa'").fetchone()
    assert run["moa_calls"] == 1

    # A plain (non-MoA) call must not bump the MoA counter.
    db.record_llm_call(
        "sess-moa",
        "2026-01-01T00:01:00+00:00",
        "anthropic/claude-opus-4.8",
        "openrouter",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        latency_ms=200,
    )
    run = conn.execute("SELECT moa_calls FROM runs WHERE session_id='sess-moa'").fetchone()
    assert run["moa_calls"] == 1


def test_record_llm_call_default_moa_preset_null():
    """A normal call leaves moa_preset NULL and moa_calls at 0."""
    db.start_run("sess-nomoa", model="claude-sonnet-4-6", platform="cli")
    db.record_llm_call(
        "sess-nomoa",
        "2026-01-01T00:00:00+00:00",
        "claude-sonnet-4-6",
        "anthropic",
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.001,
        latency_ms=50,
    )
    conn = db._get_conn()
    call = conn.execute("SELECT moa_preset FROM llm_calls WHERE session_id='sess-nomoa'").fetchone()
    assert call["moa_preset"] is None
    run = conn.execute("SELECT moa_calls FROM runs WHERE session_id='sess-nomoa'").fetchone()
    assert run["moa_calls"] == 0


def test_stats_summary_reports_moa_calls():
    """stats_summary surfaces the aggregated moa_calls count."""
    db.start_run("sess-moa-sum", model="anthropic/claude-opus-4.8", platform="cli")
    db.record_llm_call(
        "sess-moa-sum",
        "2026-01-01T00:00:00+00:00",
        "anthropic/claude-opus-4.8",
        "openrouter",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        latency_ms=200,
        moa_preset="review",
    )
    s = db.stats_summary(window_hours=0)
    assert int(s.get("moa_calls") or 0) == 1


def test_migrate_v10_adds_columns_from_wedged_v9(monkeypatch):
    """Upgrade path: a DB stuck in the pre-v10 shape (v10 marker absent, columns
    missing) self-heals on the next connect — record_llm_call must not crash
    with 'no such column: moa_preset'."""
    conn = db._get_conn()

    # Rebuild llm_calls without moa_preset and runs without moa_calls, mimicking
    # a v9 DB that predates the v10 migration. SQLite has no DROP COLUMN before
    # 3.35, so rebuild the tables.
    conn.execute("ALTER TABLE llm_calls RENAME TO _llm_calls_old")
    conn.execute("""
        CREATE TABLE llm_calls (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          TEXT NOT NULL,
            ts                  TEXT NOT NULL,
            model               TEXT,
            provider            TEXT DEFAULT '',
            tokens_in           INTEGER DEFAULT 0,
            tokens_out          INTEGER DEFAULT 0,
            cost_usd            REAL DEFAULT 0,
            latency_ms          INTEGER DEFAULT 0,
            cache_read_tokens   INTEGER DEFAULT 0,
            cache_write_tokens  INTEGER DEFAULT 0,
            reasoning_tokens    INTEGER DEFAULT 0,
            estimated           INTEGER DEFAULT 0,
            provider_assumed    INTEGER DEFAULT 0
        )
    """)
    conn.execute("DROP TABLE _llm_calls_old")
    # Drop moa_calls from runs by rebuilding is heavy; simpler to assert it is
    # re-added even when we just remove the v10 marker after dropping the llm
    # column. Remove the v10 marker so the migration re-runs.
    conn.execute("DELETE FROM schema_version WHERE version = 10")

    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('llm_calls')")}
    assert "moa_preset" not in cols  # wedged state confirmed

    db._ensure_schema(conn)

    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('llm_calls')")}
    assert "moa_preset" in cols, "v10 must re-add moa_preset when the marker is cleared"

    # And record_llm_call with a preset must succeed end-to-end.
    db.record_llm_call(
        "moa-repair",
        "2026-06-20T20:00:00+00:00",
        "anthropic/claude-opus-4.8",
        "openrouter",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        latency_ms=200,
        moa_preset="default",
    )
    row = conn.execute("SELECT moa_preset FROM llm_calls WHERE session_id='moa-repair'").fetchone()
    assert row["moa_preset"] == "default"


# ---------------------------------------------------------------------------
# v11 — subagent_edges (delegation tree for per_cron_job attribution, #49)
# ---------------------------------------------------------------------------


def test_schema_v11_subagent_edges_columns():
    """v11 creates the subagent_edges table with the delegation-tree columns."""
    conn = db._get_conn()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_edges" in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subagent_edges)")}
    assert cols == {
        "child_session_id",
        "parent_session_id",
        "parent_turn_id",
        "parent_subagent_id",
        "child_subagent_id",
        "child_role",
        "started_at",
        "stopped_at",
        "child_status",
    }


def test_schema_v11_recorded():
    versions = {r[0] for r in db._get_conn().execute("SELECT version FROM schema_version")}
    assert 11 in versions


def test_record_subagent_start_inserts_edge():
    db.record_subagent_start(
        child_session_id="c1",
        parent_session_id="p1",
        parent_turn_id="t1",
        parent_subagent_id="sa-0-aaa",
        child_subagent_id="sa-1-bbb",
        child_role="researcher",
        started_at="2026-07-03T00:00:00+00:00",
    )
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM subagent_edges WHERE child_session_id='c1'").fetchone()
    assert row["parent_session_id"] == "p1"
    assert row["child_subagent_id"] == "sa-1-bbb"
    assert row["child_role"] == "researcher"
    assert row["stopped_at"] is None
    assert row["child_status"] is None


def test_record_subagent_start_idempotent():
    """First edge wins (INSERT OR IGNORE) — a duplicate start does not clobber."""
    db.record_subagent_start(child_session_id="c1", parent_session_id="p1")
    db.record_subagent_start(child_session_id="c1", parent_session_id="p_other")
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT parent_session_id FROM subagent_edges WHERE child_session_id='c1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["parent_session_id"] == "p1"


def test_record_subagent_stop_finalizes_edge():
    db.record_subagent_start(
        child_session_id="c1", parent_session_id="p1", started_at="2026-07-03T00:00:00+00:00"
    )
    db.record_subagent_stop(
        child_session_id="c1",
        parent_session_id="p1",
        child_status="completed",
        stopped_at="2026-07-03T00:05:00+00:00",
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT stopped_at, child_status FROM subagent_edges WHERE child_session_id='c1'"
    ).fetchone()
    assert row["stopped_at"] == "2026-07-03T00:05:00+00:00"
    assert row["child_status"] == "completed"


def test_record_subagent_stop_backfills_when_start_missed():
    """If subagent_start was never seen, stop backfills the edge so the child
    still resolves to its parent. child_subagent_id is NULL (absent on stop)."""
    db.record_subagent_stop(
        child_session_id="c-orphan",
        parent_session_id="p1",
        child_status="completed",
        stopped_at="2026-07-03T00:05:00+00:00",
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT parent_session_id, child_status, child_subagent_id "
        "FROM subagent_edges WHERE child_session_id='c-orphan'"
    ).fetchone()
    assert row is not None
    assert row["parent_session_id"] == "p1"
    assert row["child_status"] == "completed"
    assert row["child_subagent_id"] is None


def test_migrate_v11_creates_table_from_wedged_v10():
    """Upgrade path: a DB stuck in the pre-v11 shape (no subagent_edges, v11
    marker absent) self-heals on the next connect and the edge write works."""
    conn = db._get_conn()
    conn.execute("DROP TABLE IF EXISTS subagent_edges")
    conn.execute("DELETE FROM schema_version WHERE version >= 11")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_edges" not in tables  # wedged state confirmed

    db._ensure_schema(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_edges" in tables
    db.record_subagent_start(
        child_session_id="c-heal",
        parent_session_id="p-heal",
        started_at="2026-07-03T00:00:00+00:00",
    )
    row = conn.execute(
        "SELECT parent_session_id FROM subagent_edges WHERE child_session_id='c-heal'"
    ).fetchone()
    assert row["parent_session_id"] == "p-heal"


def test_unattributed_child_cost_flags_missing_parent():
    """A child edge whose parent has no runs row surfaces as unattributed."""
    db.start_run("orphan-child", model="m", platform="cli")
    db.record_llm_call("orphan-child", db._utcnow(), "m", "p", 100, 50, 0.02, 100)
    db.record_subagent_start(child_session_id="orphan-child", parent_session_id="ghost-parent")

    d = db.unattributed_child_cost("2000-01-01T00:00:00+00:00")
    assert d["edges"] == 1
    assert d["unattributed_usd"] == pytest.approx(0.02)


def test_unattributed_child_cost_zero_when_parent_present():
    db.start_run("real-parent", model="m", platform="cli")
    db.start_run("linked-child", model="m", platform="cli")
    db.record_llm_call("linked-child", db._utcnow(), "m", "p", 100, 50, 0.02, 100)
    db.record_subagent_start(child_session_id="linked-child", parent_session_id="real-parent")

    d = db.unattributed_child_cost("2000-01-01T00:00:00+00:00")
    assert d["edges"] == 0
    assert d["unattributed_usd"] == pytest.approx(0.0)
