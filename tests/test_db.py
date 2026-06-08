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


def test_schema_idempotent():
    """Calling _ensure_schema twice must not raise or duplicate version rows.

    With schema v2 applied, there are exactly 2 rows (v1 and v2).
    Repeated calls must not add more rows.
    """
    conn = db._get_conn()
    # Called once already by _get_conn(); call twice more
    db._ensure_schema(conn)
    db._ensure_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    # One row per schema version: v1 + v2 = 2
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
