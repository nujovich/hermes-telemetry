"""Tests for /stats smells -- AI smell detection.

Covers:
  - smell_detector.detect_all: all five detection heuristics
  - smell_detector.detect_by_session: session grouping
  - stats.handle('smells'): table formatting + window subcommands
  - Integration with isolated DB
"""

from __future__ import annotations

import hermes_telemetry.db as db
import hermes_telemetry.smell_detector as sd
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
# smell_detector.detect_all
# ---------------------------------------------------------------------------


def test_detect_all_empty():
    assert sd.detect_all(window_hours=24) == []


def test_detect_context_rotation():
    """Sessions with tokens_in >> tokens_out should be detected."""
    now = db._utcnow()
    db.start_run("s_rot", model="m", platform="cli")
    # 10,000 in, only 500 out => 5% output ratio (below 10% threshold)
    db.record_llm_call("s_rot", now, "modelA", "openai", 10000, 500, 0.05, 100)
    db.end_run("s_rot", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "context_rotation"]
    assert len(smells) == 1
    assert smells[0]["severity"] == "high"
    assert "5" in smells[0]["detail"]  # output ratio ~5%


def test_detect_context_rotation_excludes_normal():
    """Sessions with normal output ratio should not trigger context_rotation."""
    now = db._utcnow()
    db.start_run("s_normal", model="m", platform="cli")
    # 1000 in, 500 out => 50% output ratio (above 10% threshold)
    db.record_llm_call("s_normal", now, "modelA", "openai", 1000, 500, 0.001, 50)
    db.end_run("s_normal", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "context_rotation"]
    assert len(smells) == 0


def test_detect_context_rotation_excludes_low_tokens():
    """Sessions with < 1000 tokens in should not trigger context_rotation."""
    now = db._utcnow()
    db.start_run("s_small", model="m", platform="cli")
    db.record_llm_call("s_small", now, "modelA", "openai", 500, 50, 0.001, 50)
    db.end_run("s_small", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "context_rotation"]
    assert len(smells) == 0


def test_detect_loop_trap():
    """Sessions dominated by a single tool should be flagged."""
    now = db._utcnow()
    db.start_run("s_loop", model="m", platform="cli")
    for _ in range(15):
        db.record_tool_call("s_loop", now, "web_search", True, 100)
    for _ in range(2):
        db.record_tool_call("s_loop", now, "read_file", True, 50)
    db.end_run("s_loop", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "loop_trap"]
    assert len(smells) == 1
    assert smells[0]["severity"] == "medium"
    assert smells[0]["top_tool"] == "web_search"


def test_detect_loop_trap_excludes_diverse_tools():
    """Sessions with diverse tool usage should not trigger loop_trap."""
    now = db._utcnow()
    db.start_run("s_diverse", model="m", platform="cli")
    for i in range(12):
        tool = ["web_search", "read_file", "write_file", "terminal"][i % 4]
        db.record_tool_call("s_diverse", now, tool, True, 50)
    db.end_run("s_diverse", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "loop_trap"]
    assert len(smells) == 0


def test_detect_loop_trap_requires_min_tools():
    """Sessions with <= 10 tool calls should not trigger loop_trap."""
    now = db._utcnow()
    db.start_run("s_few", model="m", platform="cli")
    for _ in range(10):
        db.record_tool_call("s_few", now, "web_search", True, 50)
    db.end_run("s_few", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "loop_trap"]
    assert len(smells) == 0


def test_detect_tool_thrashing():
    """Sessions with many failed tool calls should be flagged."""
    now = db._utcnow()
    db.start_run("s_thrash", model="m", platform="cli")
    for _ in range(15):
        db.record_tool_call("s_thrash", now, "bad_tool", False, 1000)
    for _ in range(10):
        db.record_tool_call("s_thrash", now, "ok_tool", True, 50)
    db.end_run("s_thrash", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "tool_thrashing"]
    assert len(smells) == 1
    assert smells[0]["severity"] == "high"
    assert smells[0]["failed_tools"] == 15


def test_detect_tool_thrashing_excludes_healthy():
    """Sessions with low failure rate should not trigger tool_thrashing."""
    now = db._utcnow()
    db.start_run("s_healthy", model="m", platform="cli")
    for _ in range(25):
        db.record_tool_call("s_healthy", now, "tool", True, 50)
    db.end_run("s_healthy", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "tool_thrashing"]
    assert len(smells) == 0


def test_detect_high_error_rate():
    """Failed sessions should be detected."""
    now = db._utcnow()
    db.start_run("s_err", model="m", platform="cli")
    db.record_llm_call("s_err", now, "modelA", "openai", 100, 50, 0.001, 100)
    db.end_run("s_err", "error")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "high_error_rate"]
    assert len(smells) == 1
    assert smells[0]["status"] == "error"


def test_detect_high_error_rate_severity():
    """When > 30% sessions fail, severity should be 'high'."""
    now = db._utcnow()
    # 3 failed, 1 ok => 75% error rate
    for i in range(3):
        sid = f"s_err_{i}"
        db.start_run(sid, model="m", platform="cli")
        db.record_llm_call(sid, now, "modelA", "openai", 100, 50, 0.001, 100)
        db.end_run(sid, "error")
    db.start_run("s_ok", model="m", platform="cli")
    db.record_llm_call("s_ok", now, "modelA", "openai", 100, 50, 0.001, 100)
    db.end_run("s_ok", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "high_error_rate"]
    assert len(smells) == 3
    assert all(s["severity"] == "high" for s in smells)


def test_detect_massive_session():
    """Sessions with > 100k tokens should be flagged."""
    now = db._utcnow()
    db.start_run("s_big", model="m", platform="cli")
    db.record_llm_call("s_big", now, "modelA", "openai", 60000, 50000, 0.50, 1000)
    db.end_run("s_big", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "massive_session"]
    assert len(smells) == 1
    assert smells[0]["severity"] == "warning"
    assert smells[0]["total_tokens"] == 110000


def test_detect_massive_session_api_calls():
    """Sessions with > 50 API calls should be flagged."""
    now = db._utcnow()
    db.start_run("s_many_calls", model="m", platform="cli")
    for _ in range(55):
        db.record_llm_call("s_many_calls", now, "modelA", "openai", 100, 100, 0.001, 50)
    db.end_run("s_many_calls", "ok")

    results = sd.detect_all(window_hours=24)
    smells = [r for r in results if r["smell"] == "massive_session"]
    assert len(smells) == 1
    assert smells[0]["api_calls"] == 55


def test_detect_all_sorted_by_severity():
    """Results should be ordered: high > medium > warning."""
    now = db._utcnow()

    # context_rotation (high)
    db.start_run("s_rot", model="m", platform="cli")
    db.record_llm_call("s_rot", now, "modelA", "openai", 10000, 500, 0.05, 100)
    db.end_run("s_rot", "ok")

    # massive_session (warning)
    db.start_run("s_big", model="m", platform="cli")
    db.record_llm_call("s_big", now, "modelA", "openai", 60000, 50000, 0.50, 1000)
    db.end_run("s_big", "ok")

    results = sd.detect_all(window_hours=24)
    # First results should be the high-severity ones
    assert results[0]["severity"] == "high"


# ---------------------------------------------------------------------------
# smell_detector.detect_by_session
# ---------------------------------------------------------------------------


def test_detect_by_session_groups_smells():
    """Multiple smells on the same session should be grouped together."""
    now = db._utcnow()
    # Session with both massive_session and context_rotation
    db.start_run("s_combo", model="m", platform="cli")
    db.record_llm_call("s_combo", now, "modelA", "openai", 10000, 500, 0.05, 100)
    db.record_llm_call("s_combo", now, "modelA", "openai", 50000, 50000, 0.40, 1000)
    db.end_run("s_combo", "ok")

    sessions = sd.detect_by_session(window_hours=24)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s_combo"
    assert sessions[0]["total_smells"] >= 1


def test_detect_by_session_orders_by_smells():
    """Sessions with more smells should appear first."""
    now = db._utcnow()

    # Session with only failure
    db.start_run("s_fail", model="m", platform="cli")
    db.record_llm_call("s_fail", now, "modelA", "openai", 100, 50, 0.001, 100)
    db.end_run("s_fail", "error")

    # Session with failure + context rotation
    db.start_run("s_multi", model="m", platform="cli")
    db.record_llm_call("s_multi", now, "modelA", "openai", 10000, 500, 0.05, 100)
    db.end_run("s_multi", "error")

    sessions = sd.detect_by_session(window_hours=24)
    # s_multi should have more smells (failed + context rotation)
    # s_fail should have only failed
    assert sessions[0]["total_smells"] >= sessions[-1]["total_smells"]


# ---------------------------------------------------------------------------
# stats.handle('smells')
# ---------------------------------------------------------------------------


def test_smells_block_empty():
    output = stats_mod.handle("smells")
    assert "No AI smells detected" in output


def test_smells_block_with_smells():
    now = db._utcnow()
    db.start_run("s_err", model="m", platform="cli")
    db.record_llm_call("s_err", now, "modelA", "openai", 100, 50, 0.001, 100)
    db.end_run("s_err", "error")

    output = stats_mod.handle("smells")
    assert "Smells detected" in output
    assert "HIGH" in output
    assert "s_err" in output


def test_smells_week_subcommand():
    """'smells week' should work and use 168h window."""
    output = stats_mod.handle("smells week")
    assert "No AI smells detected" in output or "last 7 days" in output


def test_smells_with_date_range():
    """smells --from and --to should work."""
    output = stats_mod.handle("smells --from 2026-06-01 --to 2026-06-10")
    assert "No AI smells detected" in output or "2026-06-01" in output


def test_smells_shown_in_usage():
    output = stats_mod.handle("unknown")
    assert "smells" in output


def test_smells_excludes_running():
    """Sessions still in 'running' status should not trigger smells."""
    now = db._utcnow()
    db.start_run("s_running", model="m", platform="cli")
    db.record_llm_call("s_running", now, "modelA", "openai", 10000, 500, 0.05, 100)
    # Do not call end_run -- stays 'running'

    results = sd.detect_all(window_hours=24)
    assert len(results) == 0
