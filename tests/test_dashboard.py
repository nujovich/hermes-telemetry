"""Tests for dashboard/serve.py CLI parsing, API helpers, and bind-warning behaviour."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import hermes_telemetry.db as db
import pytest

# The dashboard ships as a standalone script at dashboard/serve.py — not under
# the hermes_telemetry package — so import it by file path.
_SERVE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "serve.py"


@pytest.fixture
def serve_module(tmp_path):
    spec = importlib.util.spec_from_file_location("dashboard_serve", _SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve"] = module
    spec.loader.exec_module(module)
    cron_root = tmp_path / "cron"
    cron_root.mkdir()
    module.CRON_JOBS_PATH = cron_root / "jobs.json"
    module.CRON_OUTPUT_DIR = cron_root / "output"
    module.CRON_OUTPUT_DIR.mkdir()
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


def _seed_state_db(path: Path, rows: list[tuple[float, str, str]]):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO messages (timestamp, role, content) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _seed_cron_history(serve_module, jobs: list[dict], output_runs: list[tuple[str, str]]):
    serve_module.CRON_JOBS_PATH.write_text(json.dumps({"jobs": jobs}))
    for job_id, ts in output_runs:
        job_dir = serve_module.CRON_OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / f"{ts}.md").write_text("ok\n")


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


@pytest.mark.skipif(sys.version_info < (3, 9), reason="zoneinfo requires Python 3.9+")
def test_budget_window_bounds_follow_viewer_timezone_daily(serve_module):
    bounds = serve_module._budget_window_bounds_utc(
        "daily",
        "Asia/Dhaka",
        now_utc=datetime(2026, 6, 13, 18, 30, tzinfo=timezone.utc),
    )
    assert bounds["viewer_timezone"] == "Asia/Dhaka"
    assert bounds["window_start_utc"] == "2026-06-13T18:00:00+00:00"
    assert bounds["window_end_utc"] == "2026-06-14T18:00:00+00:00"


@pytest.mark.skipif(sys.version_info < (3, 9), reason="zoneinfo requires Python 3.9+")
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


@pytest.mark.skipif(sys.version_info < (3, 9), reason="zoneinfo requires Python 3.9+")
def test_dashboard_period_helper_respects_viewer_timezone(serve_module):
    assert (
        serve_module._sqlite_dashboard_period("2026-06-13T18:30:00+00:00", "day", "Asia/Dhaka")
        == "2026-06-14"
    )
    assert (
        serve_module._sqlite_dashboard_period("2026-06-13T18:30:00+00:00", "day", "UTC")
        == "2026-06-13"
    )


def test_parse_window_hours_supports_subhour_ranges(serve_module):
    assert serve_module._parse_window_hours("0.25") == 0.25
    assert serve_module._parse_window_hours("0") == 0.0


@pytest.mark.skipif(sys.version_info < (3, 9), reason="zoneinfo requires Python 3.9+")
def test_daily_token_chart_groups_by_viewer_timezone_day(serve_module, monkeypatch, tmp_path):
    state_db = tmp_path / "state.db"
    _seed_state_db(state_db, [])
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-13T17:00:00+00:00")
    db.start_run("tz-a", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "tz-a", "2026-06-13T17:30:00+00:00", "gpt-5.4", "openai-codex", 100, 10, 0.01, 50
    )
    db.end_run("tz-a", "ok", ended_at="2026-06-13T17:35:00+00:00")

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-13T18:00:00+00:00")
    db.start_run("tz-b", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "tz-b", "2026-06-13T18:30:00+00:00", "gpt-5.4", "openai-codex", 200, 20, 0.02, 50
    )
    db.end_run("tz-b", "ok", ended_at="2026-06-13T18:35:00+00:00")

    utc_rows = serve_module.api_daily_token_chart(window_hours=0, limit_days=10, tz_name="UTC")
    dhaka_rows = serve_module.api_daily_token_chart(
        window_hours=0, limit_days=10, tz_name="Asia/Dhaka"
    )

    assert [row["day"] for row in utc_rows] == ["2026-06-13"]
    assert utc_rows[0]["api_calls"] == 2
    assert [row["day"] for row in dhaka_rows] == ["2026-06-13", "2026-06-14"]
    assert dhaka_rows[0]["api_calls"] == 1
    assert dhaka_rows[1]["api_calls"] == 1


@pytest.mark.skipif(sys.version_info < (3, 9), reason="zoneinfo requires Python 3.9+")
def test_daily_token_chart_supports_hourly_granularity(serve_module, monkeypatch, tmp_path):
    state_db = tmp_path / "state.db"
    _seed_state_db(
        state_db,
        [
            (datetime(2026, 6, 13, 10, 5, tzinfo=timezone.utc).timestamp(), "user", "u1"),
            (datetime(2026, 6, 13, 10, 6, tzinfo=timezone.utc).timestamp(), "assistant", "a1"),
            (datetime(2026, 6, 13, 11, 5, tzinfo=timezone.utc).timestamp(), "user", "u2"),
            (datetime(2026, 6, 13, 11, 6, tzinfo=timezone.utc).timestamp(), "assistant", "a2"),
        ],
    )
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-13T09:55:00+00:00")
    db.start_run("hour-a", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "hour-a", "2026-06-13T10:15:00+00:00", "gpt-5.4", "openai-codex", 100, 20, 0.01, 50
    )
    db.end_run("hour-a", "ok", ended_at="2026-06-13T10:20:00+00:00")

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-13T10:55:00+00:00")
    db.start_run("hour-b", model="gpt-5.4", platform="cron")
    db.record_llm_call(
        "hour-b", "2026-06-13T11:25:00+00:00", "gpt-5.4", "openai-codex", 200, 30, 0.02, 50
    )
    db.end_run("hour-b", "ok", ended_at="2026-06-13T11:30:00+00:00")

    rows = serve_module.api_daily_token_chart(
        window_hours=0, limit_days=48, granularity="hour", tz_name="UTC"
    )

    assert [row["day"] for row in rows] == [
        "2026-06-13T09:00",
        "2026-06-13T10:00",
        "2026-06-13T11:00",
    ]
    assert rows[0]["request_runs"] == 1
    assert rows[1]["api_calls"] == 1
    assert rows[1]["message_runs"] == 2
    assert rows[2]["api_calls"] == 1


@pytest.mark.skipif(sys.version_info < (3, 9), reason="zoneinfo requires Python 3.9+")
def test_daily_token_chart_supports_minute_granularity(serve_module, monkeypatch, tmp_path):
    state_db = tmp_path / "state.db"
    _seed_state_db(
        state_db,
        [
            (datetime(2026, 6, 13, 10, 15, tzinfo=timezone.utc).timestamp(), "user", "u1"),
            (datetime(2026, 6, 13, 10, 15, tzinfo=timezone.utc).timestamp(), "assistant", "a1"),
            (datetime(2026, 6, 13, 10, 17, tzinfo=timezone.utc).timestamp(), "user", "u2"),
        ],
    )
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-13T10:10:00+00:00")
    db.start_run("minute-a", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "minute-a", "2026-06-13T10:15:20+00:00", "gpt-5.4", "openai-codex", 100, 20, 0.01, 50
    )
    db.record_tool_call("minute-a", "2026-06-13T10:15:30+00:00", "read_file", True, 10)
    db.end_run("minute-a", "ok", ended_at="2026-06-13T10:15:40+00:00")

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-13T10:16:00+00:00")
    db.start_run("minute-b", model="gpt-5.4", platform="cron")
    db.record_llm_call(
        "minute-b", "2026-06-13T10:17:10+00:00", "gpt-5.4", "openai-codex", 200, 30, 0.02, 50
    )
    db.end_run("minute-b", "ok", ended_at="2026-06-13T10:17:20+00:00")

    rows = serve_module.api_daily_token_chart(
        window_hours=0, limit_days=20, granularity="minute", tz_name="UTC"
    )

    by_day = {row["day"]: row for row in rows}
    assert set(by_day) == {
        "2026-06-13T10:10",
        "2026-06-13T10:15",
        "2026-06-13T10:16",
        "2026-06-13T10:17",
    }
    assert by_day["2026-06-13T10:10"]["request_runs"] == 1
    assert by_day["2026-06-13T10:15"]["api_calls"] == 1
    assert by_day["2026-06-13T10:15"]["tool_calls"] == 1
    assert by_day["2026-06-13T10:15"]["message_runs"] == 2
    assert by_day["2026-06-13T10:16"]["request_runs"] == 1
    assert by_day["2026-06-13T10:17"]["api_calls"] == 1


def test_daily_token_chart_includes_requests_cost_savings_and_messages(
    serve_module, monkeypatch, tmp_path
):
    _seed_cron_history(
        serve_module,
        [
            {
                "id": "trend-cron",
                "name": "Trend Cron",
                "schedule_display": "0 * * * *",
                "repeat": {"completed": 1},
                "last_run_at": "2026-06-10T11:10:00+00:00",
                "last_status": "ok",
                "enabled": True,
            }
        ],
        [("trend-cron", "2026-06-10_11-10-00")],
    )
    state_db = tmp_path / "state.db"
    _seed_state_db(
        state_db,
        [
            (datetime(2026, 6, 10, 10, 1, tzinfo=timezone.utc).timestamp(), "user", "hello"),
            (datetime(2026, 6, 10, 10, 2, tzinfo=timezone.utc).timestamp(), "assistant", "hi"),
            (datetime(2026, 6, 10, 10, 3, tzinfo=timezone.utc).timestamp(), "assistant", ""),
        ],
    )
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)
    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T09:50:00+00:00")

    db.start_run("trend-a", model="gpt-5.4", platform="discord")
    db.record_tool_call("trend-a", "2026-06-10T10:05:00+00:00", "search_files", True, 15)
    db.record_tool_call("trend-a", "2026-06-10T10:06:00+00:00", "read_file", True, 20)
    db.record_llm_call(
        "trend-a",
        "2026-06-10T10:00:00+00:00",
        "gpt-5.4",
        "openai-codex",
        100,
        50,
        0.03,
        120,
        cache_read_tokens=150,
        cache_write_tokens=20,
        reasoning_tokens=10,
    )
    db.end_run("trend-a", "ok", ended_at="2026-06-10T10:10:00+00:00")

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T11:00:00+00:00")
    db.start_run("trend-cron", model="gpt-5.4", platform="cron")
    db.record_tool_call("trend-cron", "2026-06-10T11:05:00+00:00", "browser_navigate", True, 25)
    db.end_run("trend-cron", "ok", ended_at="2026-06-10T11:10:00+00:00")

    rows = serve_module.api_daily_token_chart(window_hours=0, limit_days=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-06-10"
    assert row["api_calls"] == 1
    assert row["request_runs"] == 2
    assert row["tool_calls"] == 3
    assert row["cron_job_runs"] == 1
    assert row["message_runs"] == 2
    assert row["user_messages"] == 1
    assert row["assistant_messages"] == 1
    assert row["cost_usd"] == pytest.approx(0.03, abs=1e-6)
    assert row["total_tokens"] == 330
    assert row["estimated_savings_usd"] == pytest.approx(0.025, abs=1e-6)
    assert row["savings_pct"] == pytest.approx(45.45, abs=0.01)


def test_daily_token_chart_hides_deleted_sessions_like_dashboard_tables(
    serve_module, monkeypatch, tmp_path
):
    _seed_cron_history(
        serve_module,
        [
            {
                "id": "alpha",
                "name": "Cron Alpha",
                "schedule_display": "0 * * * *",
                "repeat": {"completed": 1},
                "last_run_at": "2026-06-10T12:10:00+00:00",
                "last_status": "ok",
                "enabled": True,
            }
        ],
        [("alpha", "2026-06-10_12-10-00")],
    )
    state_db = tmp_path / "state.db"
    _seed_state_db(
        state_db,
        [
            (datetime(2026, 6, 10, 10, 1, tzinfo=timezone.utc).timestamp(), "user", "visible user"),
            (
                datetime(2026, 6, 10, 10, 2, tzinfo=timezone.utc).timestamp(),
                "assistant",
                "visible assistant",
            ),
        ],
    )
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)
    monkeypatch.setattr(
        serve_module, "_active_hermes_session_ids", lambda: ({"sess-visible"}, True)
    )
    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T09:55:00+00:00")

    db.start_run("sess-visible", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "sess-visible", "2026-06-10T10:00:00+00:00", "gpt-5.4", "openai-codex", 100, 50, 0.03, 120
    )
    db.end_run("sess-visible", "ok", ended_at="2026-06-10T10:10:00+00:00")

    db.start_run("sess-hidden", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "sess-hidden", "2026-06-10T11:00:00+00:00", "gpt-5.4", "openai-codex", 200, 75, 0.05, 100
    )
    db.end_run("sess-hidden", "ok", ended_at="2026-06-10T11:10:00+00:00")

    db.start_run("cron_alpha", model="gpt-5.4", platform="cron")
    db.record_llm_call(
        "cron_alpha", "2026-06-10T12:00:00+00:00", "gpt-5.4", "openai-codex", 80, 20, 0.01, 90
    )
    db.end_run("cron_alpha", "ok", ended_at="2026-06-10T12:10:00+00:00")

    rows = serve_module.api_daily_token_chart(window_hours=0, limit_days=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["api_calls"] == 2
    assert row["request_runs"] == 2
    assert row["cron_job_runs"] == 1
    assert row["message_runs"] == 2
    assert row["user_messages"] == 1
    assert row["assistant_messages"] == 1
    assert row["tokens_in"] == 180
    assert row["tokens_out"] == 70


def test_daily_token_chart_include_deleted_restores_archived_sessions(
    serve_module, monkeypatch, tmp_path
):
    state_db = tmp_path / "state.db"
    _seed_state_db(
        state_db,
        [
            (datetime(2026, 6, 10, 10, 1, tzinfo=timezone.utc).timestamp(), "user", "first user"),
            (
                datetime(2026, 6, 10, 10, 2, tzinfo=timezone.utc).timestamp(),
                "assistant",
                "first assistant",
            ),
            (datetime(2026, 6, 10, 11, 1, tzinfo=timezone.utc).timestamp(), "user", "second user"),
            (
                datetime(2026, 6, 10, 11, 2, tzinfo=timezone.utc).timestamp(),
                "assistant",
                "second assistant",
            ),
        ],
    )
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)
    monkeypatch.setattr(
        serve_module, "_active_hermes_session_ids", lambda: ({"sess-visible"}, True)
    )
    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T09:55:00+00:00")

    db.start_run("sess-visible", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "sess-visible", "2026-06-10T10:00:00+00:00", "gpt-5.4", "openai-codex", 100, 50, 0.03, 120
    )
    db.end_run("sess-visible", "ok", ended_at="2026-06-10T10:10:00+00:00")

    db.start_run("sess-hidden", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "sess-hidden", "2026-06-10T11:00:00+00:00", "gpt-5.4", "openai-codex", 200, 75, 0.05, 100
    )
    db.end_run("sess-hidden", "ok", ended_at="2026-06-10T11:10:00+00:00")

    rows = serve_module.api_daily_token_chart(window_hours=0, limit_days=10, include_deleted=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["api_calls"] == 2
    assert row["request_runs"] == 2
    assert row["message_runs"] == 4
    assert row["user_messages"] == 2
    assert row["assistant_messages"] == 2
    assert row["tokens_in"] == 300
    assert row["tokens_out"] == 125


def test_daily_token_chart_supports_weekly_granularity(serve_module, monkeypatch, tmp_path):
    _seed_cron_history(
        serve_module,
        [
            {
                "id": "week-b",
                "name": "Week B",
                "schedule_display": "0 * * * *",
                "repeat": {"completed": 1},
                "last_run_at": "2026-06-12T09:10:00+00:00",
                "last_status": "ok",
                "enabled": True,
            }
        ],
        [("week-b", "2026-06-12_09-10-00")],
    )
    state_db = tmp_path / "state.db"
    _seed_state_db(
        state_db,
        [
            (datetime(2026, 6, 10, 10, 1, tzinfo=timezone.utc).timestamp(), "user", "u1"),
            (datetime(2026, 6, 10, 10, 2, tzinfo=timezone.utc).timestamp(), "assistant", "a1"),
            (datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc).timestamp(), "user", "u2"),
            (datetime(2026, 6, 12, 9, 1, tzinfo=timezone.utc).timestamp(), "assistant", "a2"),
        ],
    )
    monkeypatch.setattr(serve_module, "STATE_DB_PATH", state_db)
    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T08:00:00+00:00")

    db.start_run("week-a", model="gpt-5.4", platform="discord")
    db.record_llm_call(
        "week-a", "2026-06-10T10:00:00+00:00", "gpt-5.4", "openai-codex", 100, 50, 0.03, 100
    )
    db.end_run("week-a", "ok", ended_at="2026-06-10T10:10:00+00:00")

    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-12T08:00:00+00:00")
    db.start_run("week-b", model="gpt-5.4", platform="cron")
    db.record_llm_call(
        "week-b", "2026-06-12T09:00:00+00:00", "gpt-5.4", "openai-codex", 200, 75, 0.05, 100
    )
    db.end_run("week-b", "ok", ended_at="2026-06-12T09:10:00+00:00")

    rows = serve_module.api_daily_token_chart(window_hours=0, limit_days=10, granularity="week")
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-06-08"
    assert row["api_calls"] == 2
    assert row["request_runs"] == 2
    assert row["cron_job_runs"] == 1
    assert row["message_runs"] == 4
    assert row["tokens_in"] == 300
    assert row["tokens_out"] == 125


def test_api_summary_uses_tool_call_events_not_run_aggregate(serve_module, monkeypatch):
    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T09:55:00+00:00")
    db.start_run("summary-tools", model="gpt-5.4", platform="discord")
    db.record_tool_call("summary-tools", "2026-06-10T10:00:00+00:00", "read_file", True, 10)
    db.record_tool_call("summary-tools", "2026-06-10T10:00:01+00:00", "search_files", True, 12)
    db.end_run("summary-tools", "ok", ended_at="2026-06-10T10:01:00+00:00")

    conn = sqlite3.connect(serve_module.DB_PATH)
    conn.execute("UPDATE runs SET tool_calls = 1 WHERE session_id = 'summary-tools'")
    conn.commit()
    conn.close()

    summary = serve_module.api_summary(window_hours=0)
    assert summary["runs"]["tool_calls"] == 2


def test_api_cron_includes_scheduler_runs_without_telemetry(serve_module, monkeypatch):
    _seed_cron_history(
        serve_module,
        [
            {
                "id": "script-only",
                "name": "Script Only",
                "schedule_display": "every 15m",
                "repeat": {"completed": 3},
                "last_run_at": "2026-06-10T12:30:00+00:00",
                "last_status": "ok",
                "enabled": True,
            },
            {
                "id": "telemetry-job",
                "name": "Telemetry Job",
                "schedule_display": "0 * * * *",
                "repeat": {"completed": 2},
                "last_run_at": "2026-06-10T11:00:00+00:00",
                "last_status": "ok",
                "enabled": True,
            },
        ],
        [
            ("script-only", "2026-06-10_12-00-00"),
            ("script-only", "2026-06-10_12-15-00"),
            ("script-only", "2026-06-10_12-30-00"),
            ("telemetry-job", "2026-06-10_11-00-00"),
        ],
    )
    monkeypatch.setattr(db, "_utcnow", lambda: "2026-06-10T10:55:00+00:00")
    db.start_run(
        "cron_telemetry-job_20260610_110000",
        model="gpt-5.4",
        platform="cron",
        cron_job_id="telemetry-job",
    )
    db.record_llm_call(
        "cron_telemetry-job_20260610_110000",
        "2026-06-10T11:00:00+00:00",
        "gpt-5.4",
        "openai-codex",
        50,
        10,
        0.01,
        80,
    )
    db.end_run("cron_telemetry-job_20260610_110000", "ok", ended_at="2026-06-10T11:05:00+00:00")

    rows = serve_module.api_cron(window_hours=0)
    by_job = {row["cron_job_id"]: row for row in rows}
    assert by_job["script-only"]["runs"] == 3
    assert by_job["script-only"]["tokens_in"] == 0
    assert by_job["script-only"]["last_status"] == "ok"
    assert by_job["telemetry-job"]["runs"] == 2
    assert by_job["telemetry-job"]["tokens_in"] == 50


def test_serve_db_path_honors_telemetry_home(tmp_path, monkeypatch):
    """serve.py resolves its telemetry DB from HERMES_TELEMETRY_HOME when set,
    but keeps state.db / cron on HERMES_HOME."""
    import importlib.util

    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))

    spec = importlib.util.spec_from_file_location("dashboard_serve_th", _SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert tmp_path / "shared" / "telemetry" / "telemetry.db" == module.DB_PATH
    assert tmp_path / "profile" / "state.db" == module.STATE_DB_PATH
    assert tmp_path / "profile" / "cron" / "jobs.json" == module.CRON_JOBS_PATH
