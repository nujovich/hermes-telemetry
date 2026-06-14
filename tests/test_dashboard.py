"""Tests for dashboard/serve.py CLI parsing, API helpers, and bind-warning behaviour."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import hermes_telemetry.db as db
import pytest

# The dashboard ships as a standalone script at dashboard/serve.py — not under
# the hermes_telemetry package — so import it by file path.
_SERVE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "serve.py"


@pytest.fixture
def serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_serve", _SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("dashboard_serve", None)


@pytest.fixture(autouse=True)
def isolated_dashboard_db():
    db._local.conn = None
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults(serve_module):
    host, port = serve_module._parse_args([])
    assert host == "127.0.0.1"
    assert port == 8765


def test_parse_args_custom_port_flag(serve_module):
    host, port = serve_module._parse_args(["--port", "9090"])
    assert host == "127.0.0.1"
    assert port == 9090


def test_parse_args_custom_host_flag(serve_module):
    host, port = serve_module._parse_args(["--host", "0.0.0.0"])
    assert host == "0.0.0.0"
    assert port == 8765


def test_parse_args_host_and_port(serve_module):
    host, port = serve_module._parse_args(["--host", "192.168.1.42", "--port", "9999"])
    assert host == "192.168.1.42"
    assert port == 9999


def test_parse_args_positional_port_back_compat(serve_module):
    """Original usage `serve.py 9090` must keep working."""
    host, port = serve_module._parse_args(["9090"])
    assert host == "127.0.0.1"
    assert port == 9090


def test_parse_args_named_port_wins_over_positional(serve_module):
    """If a user passes both forms, --port takes precedence."""
    host, port = serve_module._parse_args(["--port", "5000", "8765"])
    assert port == 5000


# ---------------------------------------------------------------------------
# _warn_if_exposed
# ---------------------------------------------------------------------------


def test_warn_if_exposed_quiet_for_loopback(serve_module, capsys, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.dashboard"):
        serve_module._warn_if_exposed("127.0.0.1")
        serve_module._warn_if_exposed("localhost")
        serve_module._warn_if_exposed("::1")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_warn_if_exposed_warns_for_wildcard(serve_module, capsys, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.dashboard"):
        serve_module._warn_if_exposed("0.0.0.0")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "NO" in captured.err and "authentication" in captured.err
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_warn_if_exposed_warns_for_specific_lan_ip(serve_module, capsys):
    """Binding to a specific non-loopback IP still warrants the warning."""
    serve_module._warn_if_exposed("192.168.1.42")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "192.168.1.42" in captured.err


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def test_api_requests_filters_by_tool_and_status(serve_module):
    now = db._utcnow()
    db.start_run("sess-ok", model="m1", platform="cli")
    db.record_llm_call("sess-ok", now, "m1", "openrouter", 100, 50, 0.01, 120)
    db.record_tool_call("sess-ok", now, "read_file", True, 20)
    db.end_run("sess-ok", "ok")

    db.start_run("sess-error", model="m2", platform="telegram")
    db.record_llm_call(
        "sess-error", now, "m2", "anthropic", 200, 80, 0.02, 300, reasoning_tokens=15
    )
    db.record_tool_call("sess-error", now, "terminal", False, 900)
    db.end_run("sess-error", "error")

    data = serve_module.api_requests(
        limit=20,
        window_hours=24,
        status="error",
        tool_name="terminal",
        include_deleted=True,
    )
    assert data["total_requests"] == 1
    row = data["rows"][0]
    assert row["session_id"] == "sess-error"
    assert row["provider"] == "anthropic"
    assert row["reasoning_tokens"] == 15


def test_api_request_detail_returns_session_context(serve_module):
    now = db._utcnow()
    db.start_run("sess-detail", model="m1", platform="cli")
    db.record_llm_call("sess-detail", now, "m1", "openrouter", 100, 20, 0.01, 111)
    db.record_llm_call(
        "sess-detail", now, "m1", "openrouter", 120, 40, 0.02, 222, cache_read_tokens=30
    )
    db.record_tool_call("sess-detail", now, "read_file", True, 44)
    db.end_run("sess-detail", "ok")

    req_id = db._get_conn().execute("SELECT MAX(id) FROM llm_calls").fetchone()[0]
    detail = serve_module.api_request_detail(req_id)
    assert detail["request"]["session_id"] == "sess-detail"
    assert detail["session_totals"]["api_calls"] == 2
    assert len(detail["sibling_requests"]) == 2
    assert detail["session_tools"][0]["tool_name"] == "read_file"


def test_api_tool_analytics_and_error_center(serve_module):
    now = db._utcnow()
    db.start_run("sess-a", model="m1", platform="cli")
    db.record_llm_call("sess-a", now, "m1", "openrouter", 100, 20, 0.01, 111)
    db.record_tool_call("sess-a", now, "read_file", True, 44)
    db.end_run("sess-a", "ok")

    db.start_run("sess-b", model="m2", platform="discord")
    db.record_llm_call("sess-b", now, "m2", "anthropic", 150, 35, 0.03, 333)
    db.record_tool_call("sess-b", now, "terminal", False, 950)
    db.record_tool_call("sess-b", now, "terminal", False, 870)
    db.end_run("sess-b", "interrupted")

    analytics = serve_module.api_tool_analytics(window_hours=24, include_deleted=True)
    by_tool = {row["tool_name"]: row for row in analytics["by_tool"]}
    assert analytics["overall"]["failed_calls"] == 2
    assert by_tool["terminal"]["failed_calls"] == 2
    assert by_tool["terminal"]["failure_pct"] == 100.0

    errors = serve_module.api_error_center(window_hours=24, include_deleted=True)
    assert errors["summary"]["runs"]["interrupted_runs"] == 1
    assert errors["summary"]["tools"]["failed_tool_calls"] == 2
    assert errors["failed_tools"][0]["tool_name"] == "terminal"
    assert any(row["kind"] == "tool_failure" for row in errors["incidents"])


def test_api_provider_health_flags_estimated_and_failures(serve_module):
    now = db._utcnow()
    db.start_run("sess-health-ok", model="m1", platform="cli")
    db.record_llm_call(
        "sess-health-ok", now, "m1", "openrouter", 100, 20, 0.01, 100, estimated=False
    )
    db.end_run("sess-health-ok", "ok")

    db.start_run("sess-health-warn", model="m2", platform="discord")
    db.record_llm_call("sess-health-warn", now, "m2", "nous", 120, 40, 0.02, 250, estimated=True)
    db.record_llm_call("sess-health-warn", now, "m2", "nous", 100, 30, 0.02, 300, estimated=True)
    db.end_run("sess-health-warn", "error")

    health = serve_module.api_provider_health(window_hours=24)
    by_provider = {row["provider"]: row for row in health["rows"]}
    assert by_provider["nous"]["estimated_pct"] == 100.0
    assert by_provider["nous"]["failed_runs_current"] == 1
    assert by_provider["nous"]["health"] in {"warn", "error"}
    assert by_provider["openrouter"]["calls_current"] == 1


def test_operator_followup_api_surfaces(serve_module):
    now = db._utcnow()
    db.start_run("cron_job-a_20260613_120000", model="m1", platform="cron", cron_job_id="job-a")
    db.record_llm_call(
        "cron_job-a_20260613_120000",
        now,
        "m1",
        "openrouter",
        1000,
        100,
        0.05,
        800,
        cache_read_tokens=300,
    )
    db.record_tool_call("cron_job-a_20260613_120000", now, "terminal", False, 1200)
    db.end_run("cron_job-a_20260613_120000", "error")

    db.start_run("sess-model-ok", model="m2", platform="cli")
    db.record_llm_call(
        "sess-model-ok", now, "m2", "nous", 500, 250, 0.01, 400, cache_read_tokens=500
    )
    db.record_tool_call("sess-model-ok", now, "read_file", True, 20)
    db.end_run("sess-model-ok", "ok")

    efficiency = serve_module.api_model_efficiency(window_hours=24, include_deleted=True)
    by_model = {row["model"]: row for row in efficiency}
    assert by_model["m2"]["output_input_ratio"] == 0.5
    assert by_model["m2"]["cache_hit_share_pct"] == 50.0
    assert "efficiency_score" in by_model["m2"]

    heatmap = serve_module.api_tool_failure_heatmap(window_hours=24, include_deleted=True)
    terminal_rows = [row for row in heatmap if row["tool_name"] == "terminal"]
    assert terminal_rows
    assert terminal_rows[0]["failed_calls"] == 1
    assert terminal_rows[0]["failure_pct"] == 100.0

    cron_waste = serve_module.api_cron_failure_waste(window_hours=24, include_deleted=True)
    by_cron = {row["cron_job_id"]: row for row in cron_waste}
    assert by_cron["job-a"]["failed_runs"] == 1
    assert by_cron["job-a"]["wasted_tokens"] == 1400
    assert "failure-rate" in by_cron["job-a"]["risks"]


def test_model_period_trends_and_share_comparison(serve_module):
    db.start_run("sess-may-m1", model="gpt-5.4", platform="cli")
    db.record_llm_call(
        "sess-may-m1", "2026-05-15T10:00:00+00:00", "gpt-5.4", "openai-codex", 100, 50, 0.01, 120
    )
    db.end_run("sess-may-m1", "ok")

    db.start_run("sess-may-m2", model="claude", platform="discord")
    db.record_llm_call(
        "sess-may-m2", "2026-05-16T10:00:00+00:00", "claude", "anthropic", 50, 50, 0.02, 140
    )
    db.end_run("sess-may-m2", "ok")

    db.start_run("sess-june-m1", model="gpt-5.4", platform="cli")
    db.record_llm_call(
        "sess-june-m1", "2026-06-10T10:00:00+00:00", "gpt-5.4", "openai-codex", 300, 100, 0.03, 220
    )
    db.end_run("sess-june-m1", "ok")

    db.start_run("sess-june-m2", model="claude", platform="discord")
    db.record_llm_call(
        "sess-june-m2", "2026-06-11T10:00:00+00:00", "claude", "anthropic", 100, 100, 0.06, 180
    )
    db.end_run("sess-june-m2", "ok")

    trends = serve_module.api_model_period_trends(
        window_hours=0, granularity="month", metric="tokens", top_n=2, limit_periods=12
    )
    assert trends["granularity"] == "month"
    assert trends["models"] == ["gpt-5.4", "claude"]
    assert [row["period"] for row in trends["rows"]] == ["2026-05", "2026-06"]
    june = trends["rows"][-1]
    assert june["models"]["gpt-5.4"]["total_tokens"] == 400
    assert june["models"]["claude"]["total_tokens"] == 200
    assert june["totals"]["total_tokens"] == 600

    comparison = serve_module.api_model_share_comparison(
        window_hours=0, granularity="month", limit=10
    )
    assert comparison["current_period"] == "2026-06"
    assert comparison["previous_period"] == "2026-05"
    by_model = {row["model"]: row for row in comparison["rows"]}
    assert by_model["gpt-5.4"]["current_token_share_pct"] == pytest.approx(66.67, abs=0.01)
    assert by_model["gpt-5.4"]["previous_token_share_pct"] == pytest.approx(60.0, abs=0.01)
    assert by_model["gpt-5.4"]["token_share_delta_pct"] == pytest.approx(6.67, abs=0.01)
    assert by_model["claude"]["current_cost_share_pct"] == pytest.approx(66.67, abs=0.01)


def test_model_share_comparison_requires_two_periods(serve_module):
    db.start_run("sess-only", model="gpt-5.4", platform="cli")
    db.record_llm_call(
        "sess-only", "2026-06-10T10:00:00+00:00", "gpt-5.4", "openai-codex", 300, 100, 0.03, 220
    )
    db.end_run("sess-only", "ok")

    comparison = serve_module.api_model_share_comparison(
        window_hours=0, granularity="month", limit=10
    )
    assert comparison["current_period"] == "2026-06"
    assert comparison["previous_period"] is None
    assert comparison["rows"] == []


def test_model_period_trends_week_groups_by_monday_start(serve_module):
    db.start_run("sess-week-a", model="gpt-5.4", platform="cli")
    db.record_llm_call(
        "sess-week-a", "2025-12-29T10:00:00+00:00", "gpt-5.4", "openai-codex", 100, 0, 0.01, 100
    )
    db.end_run("sess-week-a", "ok")

    db.start_run("sess-week-b", model="gpt-5.4", platform="cli")
    db.record_llm_call(
        "sess-week-b", "2026-01-02T10:00:00+00:00", "gpt-5.4", "openai-codex", 200, 0, 0.02, 100
    )
    db.end_run("sess-week-b", "ok")

    trends = serve_module.api_model_period_trends(
        window_hours=0, granularity="week", metric="tokens", top_n=2, limit_periods=12
    )
    assert [row["period"] for row in trends["rows"]] == ["2025-12-29"]
    assert trends["rows"][0]["models"]["gpt-5.4"]["total_tokens"] == 300


def test_budget_window_bounds_follow_viewer_timezone_daily(serve_module):
    bounds = serve_module._budget_window_bounds_utc(
        "daily",
        "Asia/Dhaka",
        now_utc=datetime(2026, 6, 13, 18, 30, tzinfo=timezone.utc),
    )
    assert bounds["viewer_timezone"] == "Asia/Dhaka"
    assert bounds["window_start_utc"] == "2026-06-13T18:00:00+00:00"
    assert bounds["window_end_utc"] == "2026-06-14T18:00:00+00:00"


def test_budget_window_bounds_follow_viewer_timezone_monthly(serve_module):
    bounds = serve_module._budget_window_bounds_utc(
        "monthly",
        "Asia/Dhaka",
        now_utc=datetime(2026, 12, 31, 20, 30, tzinfo=timezone.utc),
    )
    assert bounds["viewer_timezone"] == "Asia/Dhaka"
    assert bounds["window_start_utc"] == "2026-12-31T18:00:00+00:00"
    assert bounds["window_end_utc"] == "2027-01-31T18:00:00+00:00"


def test_budget_update_returns_viewer_timezone_metadata(serve_module, tmp_path):
    import sqlite3

    db_path = tmp_path / "telemetry.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (started_at TEXT, cost_usd REAL, estimated_llm_calls INTEGER, api_calls INTEGER)"
    )
    conn.execute(
        "INSERT INTO runs (started_at, cost_usd, estimated_llm_calls, api_calls) VALUES (?, ?, ?, ?)",
        ("2026-06-13T01:00:00+00:00", 0.5, 0, 1),
    )
    conn.commit()
    conn.close()

    budget_path = tmp_path / "budget.yaml"
    budget_path.write_text(
        "budgets:\n  global:\n    daily_usd: 5.0\n    monthly_usd: 100.0\nthresholds:\n  soft_pct: 0.8\n  hard_pct: 1.0\non_estimated:\n  mode: warn_only\n"
    )

    serve_module.DB_PATH = db_path
    serve_module._local.c = None

    updated = serve_module.api_budget_update(
        {"scope": "global", "window": "daily", "limit_usd": 5.0}, "UTC"
    )
    expected = serve_module.api_budget("UTC")

    updated_daily = next(x for x in updated["budgets"] if x["scope"] == "global/daily")
    expected_daily = next(x for x in expected["budgets"] if x["scope"] == "global/daily")
    assert updated_daily["viewer_timezone"] == expected_daily["viewer_timezone"] == "UTC"
    assert updated_daily["window_start_utc"] == expected_daily["window_start_utc"]
    assert updated_daily["window_end_utc"] == expected_daily["window_end_utc"]


def test_budget_detail_restores_default_thresholds(serve_module, tmp_path):
    import sqlite3

    db_path = tmp_path / "telemetry.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (started_at TEXT, cost_usd REAL, estimated_llm_calls INTEGER, api_calls INTEGER)"
    )
    conn.commit()
    conn.close()

    budget_path = tmp_path / "budget.yaml"
    budget_path.write_text("budgets:\n  global:\n    daily_usd: 5.0\n")

    serve_module.DB_PATH = db_path
    serve_module._local.c = None

    detail = serve_module.api_budget_detail("global", "daily", "UTC")
    assert detail["soft_pct"] == 0.8
    assert detail["hard_pct"] == 1.0
    assert detail["on_estimated_mode"] == "warn_only"
