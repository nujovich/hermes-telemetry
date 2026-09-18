"""Tests for loop_detector.py — loop detection from session and tool-call data."""

from __future__ import annotations

import hermes_telemetry.db as db
import hermes_telemetry.loop_detector as ld
import pytest


@pytest.fixture(autouse=True)
def isolated_db():
    """Reset per-thread connection between tests."""
    db._local.conn = None


def _seed_cron_session(session_id="cron_test-job_20260723_120000", cron_job_id="test-job"):
    """Create a cron-platform session."""
    db.start_run(
        session_id=session_id,
        model="gpt-4o",
        platform="cron",
        cron_job_id=cron_job_id,
    )
    db.end_run(session_id=session_id, status="ok")


def _seed_normal_session(session_id="normal-session-1"):
    """Create a non-cron session."""
    db.start_run(
        session_id=session_id,
        model="gpt-4o",
        platform="chat",
    )
    db.end_run(session_id=session_id, status="ok")


def _seed_tool_call(session_id, tool_name, ok=True):
    """Record a single tool call."""
    db.record_tool_call(
        session_id=session_id,
        ts=db._utcnow(),
        tool_name=tool_name,
        ok=ok,
        latency_ms=100,
    )


class TestDetectSessionLoop:
    def test_cron_platform_session_detected(self):
        """Cron sessions with a cron_job_id are detected as cron loops."""
        _seed_cron_session()
        result = ld.detect_session_loop("cron_test-job_20260723_120000")
        assert result is not None
        assert result["loop_type"] == "cron"
        assert result["detected_from"] == "cron_platform"
        assert result["cron_job_id"] == "test-job"

    def test_normal_session_not_detected(self):
        """Non-cron sessions without scheduling tools return None."""
        _seed_normal_session()
        result = ld.detect_session_loop("normal-session-1")
        assert result is None

    def test_self_perpetuating_via_cronjob_tool(self):
        """Sessions that use the cronjob tool are detected as self-perpetuating."""
        _seed_normal_session()
        _seed_tool_call("normal-session-1", "cronjob")
        result = ld.detect_session_loop("normal-session-1")
        assert result is not None
        assert result["loop_type"] == "self_perpetuating"
        assert result["detected_from"] == "cronjob_tool"
        assert result["tool_call_count"] == 1
        assert "cronjob" in result["tool_call_names"]

    def test_cron_session_with_cronjob_tool(self):
        """Cron sessions that also use cronjob tool (re-scheduling) are still cron type."""
        _seed_cron_session()
        _seed_tool_call("cron_test-job_20260723_120000", "cronjob")
        result = ld.detect_session_loop("cron_test-job_20260723_120000")
        assert result is not None
        assert result["loop_type"] == "cron"
        assert result["detected_from"] == "cron_platform"

    def test_non_loop_tool_not_detected(self):
        """Tools like terminal, web_search are not loop indicators."""
        _seed_normal_session()
        _seed_tool_call("normal-session-1", "terminal")
        _seed_tool_call("normal-session-1", "web_search")
        result = ld.detect_session_loop("normal-session-1")
        assert result is None

    def test_nonexistent_session(self):
        """Nonexistent session returns None."""
        result = ld.detect_session_loop("nonexistent-id")
        assert result is None


class TestListLoopSessions:
    def test_lists_cron_and_self_perpetuating(self):
        """list_loop_sessions returns both cron and self-perpetuating sessions."""
        _seed_cron_session("cron_job-a_20260723_120000", "job-a")
        _seed_cron_session("cron_job-b_20260723_130000", "job-b")
        _seed_normal_session("sp-session-1")
        _seed_tool_call("sp-session-1", "cronjob")

        sessions = ld.list_loop_sessions()
        sids = {s["session_id"] for s in sessions}

        assert "cron_job-a_20260723_120000" in sids
        assert "cron_job-b_20260723_130000" in sids
        assert "sp-session-1" in sids

    def test_no_duplicates(self):
        """Sessions that are both cron and use cronjob appear only once."""
        _seed_cron_session()
        _seed_tool_call("cron_test-job_20260723_120000", "cronjob")
        sessions = ld.list_loop_sessions()
        matching = [s for s in sessions if s["session_id"] == "cron_test-job_20260723_120000"]
        assert len(matching) == 1


class TestListLoopJobs:
    def test_aggregates_cron_jobs(self):
        """list_loop_jobs returns per-job aggregates."""
        _seed_cron_session("cron_job-x_20260720_120000", "job-x")
        _seed_cron_session("cron_job-x_20260721_120000", "job-x")
        _seed_cron_session("cron_job-x_20260722_120000", "job-x")

        jobs = ld.list_loop_jobs()
        job_x = [j for j in jobs if j["cron_job_id"] == "job-x"]
        assert len(job_x) == 1
        assert job_x[0]["session_count"] == 3

    def test_empty_when_no_cron(self):
        """No cron sessions yields empty list."""
        _seed_normal_session()
        jobs = ld.list_loop_jobs()
        assert jobs == []

    def test_status_active_for_recent(self):
        """Jobs seen recently are marked active."""
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        sid = f"cron_recent-job_{today}"
        db.start_run(
            session_id=sid,
            model="gpt-4o",
            platform="cron",
            cron_job_id="recent-job",
        )
        db.end_run(session_id=sid, status="ok")

        jobs = ld.list_loop_jobs()
        recent = [j for j in jobs if j["cron_job_id"] == "recent-job"]
        assert len(recent) == 1
        assert recent[0]["status"] == "active"

    def test_status_expired_for_old(self):
        """Jobs not seen in over 30 days are marked expired."""
        # Seed a session with an old timestamp by inserting directly.

        conn = db._get_conn()
        old_sid = "cron_old-job_20260601_120000"
        conn.execute(
            """INSERT INTO runs (session_id, platform, cron_job_id, model,
               started_at, status, tokens_in, tokens_out, cost_usd)
               VALUES (?, 'cron', 'old-job', 'gpt-4o',
               '2026-06-01T12:00:00+00:00', 'ok', 0, 0, 0.0)""",
            (old_sid,),
        )

        jobs = ld.list_loop_jobs()
        # The job should still appear with its data.
        old = [j for j in jobs if j["cron_job_id"] == "old-job"]
        assert len(old) == 1
        # Since today is 2026-07-23, 2026-06-01 is > 30 days ago.
        assert old[0]["status"] == "expired"
