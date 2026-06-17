"""Tests for /stats efficiency — per-session efficiency scores.

Covers:
  - db.efficiency_runs: correct score computation, ordering (highest first)
  - stats.handle('efficiency'): table formatting + window subcommands
"""

from __future__ import annotations

import hermes_telemetry.db as db
import hermes_telemetry.stats as stats_mod
import pytest


@pytest.fixture(autouse=True)
def isolated_db():
    # HERMES_HOME is isolated by the conftest baseline; reset the DB conn here.
    db._local.conn = None
    yield
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# db.efficiency_runs
# ---------------------------------------------------------------------------


def test_efficiency_runs_empty():
    assert db.efficiency_runs(window_hours=24) == []


def test_efficiency_runs_scores_perfect_session():
    """A session with high token productivity, no errors, few turns."""
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelA", "openai", 100, 200, 0.001, 50)
    db.record_llm_call("s1", now, "modelA", "openai", 100, 200, 0.001, 50)
    db.end_run("s1", "ok")

    rows = db.efficiency_runs(window_hours=24)
    assert len(rows) == 1
    r = rows[0]
    # tokens_out/tokens_in = 400/200 = 2.0
    # output_contribution = min(60, 2.0 * 40) = 60
    # error_penalty = 0 (ok), turn_penalty = min(30, 2 * 1.5) = 3
    # score = 40 + 60 - 0 - 3 = 97
    assert r["efficiency_score"] == 97.0
    assert r["status"] == "ok"


def test_efficiency_runs_penalizes_failures():
    """Errored sessions should receive a heavy penalty.

    'error' is the real failure status the plugin writes (__init__.py session
    end); 'failed' is only a subagent child_status and never reaches a run.
    """
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelA", "openai", 100, 100, 0.001, 50)
    db.end_run("s1", "error")

    rows = db.efficiency_runs(window_hours=24)
    assert len(rows) == 1
    r = rows[0]
    # output_ratio = 1.0, contribution = 40
    # error_penalty = 30 (error), turn_penalty = 1.5
    # score = 40 + 40 - 30 - 1.5 = 48.5
    assert r["efficiency_score"] == 48.5


def test_efficiency_runs_turn_penalty():
    """Many API calls should reduce the efficiency score."""
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    for _ in range(25):
        db.record_llm_call("s1", now, "modelA", "openai", 10, 10, 0.001, 50)
    db.end_run("s1", "ok")

    rows = db.efficiency_runs(window_hours=24)
    assert len(rows) == 1
    r = rows[0]
    # api_calls = 25, turn_penalty = min(30, 25 * 1.5) = 30 (capped)
    # output_ratio = 1.0, contribution = 40
    # score = 40 + 40 - 0 - 30 = 50
    assert r["efficiency_score"] == 50.0


def test_efficiency_runs_ordered_highest_first():
    """Rows should be sorted by efficiency_score descending."""
    now = db._utcnow()
    # Good session
    db.start_run("s_good", model="m", platform="cli")
    db.record_llm_call("s_good", now, "mA", "oai", 100, 150, 0.001, 50)
    db.end_run("s_good", "ok")
    # Errored session
    db.start_run("s_bad", model="m", platform="cli")
    db.record_llm_call("s_bad", now, "mA", "oai", 100, 100, 0.001, 50)
    db.end_run("s_bad", "error")

    rows = db.efficiency_runs(window_hours=24)
    assert len(rows) == 2
    assert rows[0]["efficiency_score"] >= rows[1]["efficiency_score"]
    # Good session should be first
    assert rows[0]["session_id"] == "s_good"
    assert rows[1]["session_id"] == "s_bad"


def test_efficiency_runs_excludes_running():
    """Sessions still in 'running' status should not be scored."""
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelA", "openai", 100, 100, 0.001, 50)
    # Do not call end_run — stays 'running'

    rows = db.efficiency_runs(window_hours=24)
    assert len(rows) == 0


def test_efficiency_runs_interrupted_penalty():
    """Interrupted sessions get a smaller penalty than errored ones."""
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelA", "openai", 100, 100, 0.001, 50)
    db.end_run("s1", "interrupted")

    rows = db.efficiency_runs(window_hours=24)
    assert len(rows) == 1
    # error_penalty = 10 (interrupted)
    # score = 40 + 40 - 10 - 1.5 = 68.5
    assert rows[0]["efficiency_score"] == 68.5


# ---------------------------------------------------------------------------
# stats.handle('efficiency')
# ---------------------------------------------------------------------------


def test_efficiency_block_empty():
    output = stats_mod.handle("efficiency")
    assert "No completed sessions found" in output


def test_efficiency_block_with_sessions():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelA", "openai", 100, 200, 0.001, 50)
    db.end_run("s1", "ok")

    output = stats_mod.handle("efficiency")
    assert "Average efficiency" in output
    assert "Sessions scored" in output
    assert "s1" in output
    assert "Score" in output


def test_efficiency_week_subcommand():
    """'efficiency week' should work and use 168h window."""
    # Just verify it doesn't crash when there is no data.
    output = stats_mod.handle("efficiency week")
    assert "No completed sessions found" in output or "last 7 days" in output


def test_efficiency_with_date_range():
    """efficiency --from and --to should work."""
    output = stats_mod.handle("efficiency --from 2026-06-01 --to 2026-06-10")
    assert "No completed sessions found" in output or "2026-06-01" in output


def test_efficiency_shown_in_usage():
    output = stats_mod.handle("unknown")
    assert "efficiency" in output
    assert "0-100" in output
