"""End-to-end tests for the dashboard plugin's read-only API.

These tests exercise the plugin endpoints as Python functions (no FastAPI
client) so they run in CI without pulling fastapi as a dev dep. The
isolation contract (HERMES_HOME → tmp dir) is enforced by conftest.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def plugin_api(monkeypatch):
    """Load ``dashboard/plugin_api.py`` the same way the Hermes loader does.

    Hermes imports the file via ``importlib.util.spec_from_file_location``,
    so the plugin runs **without** being part of any Python package. We
    mirror that here.

    Mirrors the production order: the runtime creates ``telemetry.db`` (with
    its schema) before the dashboard plugin queries it. We trigger schema
    creation here by touching the runtime db module once.
    """
    fastapi = pytest.importorskip("fastapi")  # noqa: F841

    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(repo_root))
    # The runtime caches a per-thread connection at module level; if a
    # previous test left one pointing at a now-deleted tmp dir, reset it.
    import threading as _t

    import db as runtime_db

    runtime_db._local = _t.local()
    runtime_db._get_conn()  # creates dir + tables on first call

    api_file = repo_root / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_telemetry", api_file
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(rows_runs=(), rows_llm=(), rows_tool=()):
    """Populate the telemetry DB through the runtime's own API.

    Using ``db.start_run`` / ``record_llm_call`` / ``record_tool_call`` keeps
    the schema migrations honest — if a future schema bump breaks the plugin
    queries, these tests catch it.
    """
    import db as runtime_db

    for r in rows_runs:
        runtime_db.start_run(**r)
    for r in rows_llm:
        runtime_db.record_llm_call(**r)
    for r in rows_tool:
        runtime_db.record_tool_call(**r)


def test_requests_and_providers_expose_provider_assumed(plugin_api):
    """The /requests and /providers endpoints surface the provider_assumed flag
    and per-provider assumed count (issue #42)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[{"session_id": "sa", "model": "moonshotai/kimi-k2.6", "platform": "cli"}],
        rows_llm=[
            {
                "session_id": "sa",
                "ts": now,
                "model": "moonshotai/kimi-k2.6",
                "provider": "nous",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": 0.0004,
                "latency_ms": 100,
                "provider_assumed": True,
            },
            {
                "session_id": "sa",
                "ts": now,
                "model": "claude-sonnet-4-6",
                "provider": "nous",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": 0.001,
                "latency_ms": 100,
                "provider_assumed": False,
            },
        ],
    )

    reqs = plugin_api.requests(limit=10, window_hours=24)
    flags = {r["model"]: r["provider_assumed"] for r in reqs["rows"]}
    assert flags["moonshotai/kimi-k2.6"] == 1
    assert flags["claude-sonnet-4-6"] == 0

    providers = plugin_api.providers(window_hours=24)
    nous = next(r for r in providers["rows"] if r["provider"] == "nous")
    assert nous["provider_assumed_calls"] == 1


def test_health(plugin_api):
    out = plugin_api.health()
    assert out["ok"] is True
    assert out["runs_total"] == 0


def test_summary_empty(plugin_api):
    out = plugin_api.summary(window_hours=24)
    assert out["window_hours"] == 24
    assert out["runs"]["total_runs"] == 0
    assert out["llm"]["api_calls"] == 0
    assert out["daily_cost"] == []


def test_summary_and_listings_with_data(plugin_api):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {"session_id": "s1", "model": "gpt-4o", "platform": "cli"},
            {
                "session_id": "cron_demo_20260101_120000",
                "model": "gpt-4o",
                "platform": "cron",
                "cron_job_id": "demo",
            },
        ],
        rows_llm=[
            {
                "session_id": "s1",
                "ts": now,
                "model": "gpt-4o",
                "provider": "openai",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": 0.0025,
                "latency_ms": 320,
            },
        ],
    )

    summary = plugin_api.summary(window_hours=24)
    assert summary["runs"]["total_runs"] == 2
    assert summary["llm"]["api_calls"] == 1

    runs = plugin_api.runs(limit=10, window_hours=24)
    assert runs["total_runs"] == 2
    assert {r["session_id"] for r in runs["rows"]} == {"s1", "cron_demo_20260101_120000"}

    reqs = plugin_api.requests(limit=10, window_hours=24)
    assert reqs["total_requests"] == 1
    assert reqs["rows"][0]["model"] == "gpt-4o"

    providers = plugin_api.providers(window_hours=24)
    assert providers["rows"][0]["provider"] == "openai"
    assert providers["rows"][0]["total_calls"] == 1

    cron = plugin_api.cron(window_hours=720)
    assert cron["rows"] and cron["rows"][0]["cron_job_id"] == "demo"

    detail = plugin_api.session_detail("s1")
    assert detail["run"]["session_id"] == "s1"
    assert detail["llm_summary"]["api_calls"] == 1

    tokens = plugin_api.token_breakdown(window_hours=24)
    assert tokens["tokens_in"] == 100
    assert tokens["tokens_out"] == 50
    assert tokens["total_tokens"] == 150


def test_session_detail_missing(plugin_api):
    out = plugin_api.session_detail("nope")
    assert out["error"] == "session not found"


def test_budget_missing_yaml(plugin_api):
    assert plugin_api.budget() == {"enabled": False}


def test_budget_with_yaml(plugin_api, tmp_path, monkeypatch):
    import os

    hermes_home = Path(os.environ["HERMES_HOME"])
    (hermes_home / "telemetry").mkdir(parents=True, exist_ok=True)
    (hermes_home / "telemetry" / "budget.yaml").write_text(
        "budgets:\n"
        "  global:\n"
        "    daily_usd: 1.0\n"
        "    monthly_usd: 10.0\n"
        "thresholds:\n"
        "  soft_pct: 0.5\n"
        "  hard_pct: 1.0\n",
        encoding="utf-8",
    )
    out = plugin_api.budget()
    assert out["enabled"] is True
    scopes = {s["scope"]: s for s in out["scopes"]}
    assert "global/daily" in scopes and "global/monthly" in scopes
    assert scopes["global/daily"]["limit_usd"] == 1.0
    assert scopes["global/daily"]["level"] == "ok"  # no spend


def test_tier_transitions_empty(plugin_api):
    out = plugin_api.tier_transitions(window_hours=72)
    assert out == {"window_hours": 72, "rows": []}


def test_tier_transitions_returns_recorded_flip(plugin_api):
    import db as runtime_db

    runtime_db.record_free_model("owl-alpha", "nous")
    runtime_db.record_free_paid_transition("owl-alpha", "nous", "sess-x", 0.5)
    out = plugin_api.tier_transitions(window_hours=72)
    assert out["window_hours"] == 72
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["model"] == "owl-alpha"
    assert row["provider"] == "nous"
    assert row["session_id"] == "sess-x"
    assert abs(row["first_paid_cost_usd"] - 0.5) < 1e-9


def test_tier_transitions_window_filters_old_rows(plugin_api):
    import db as runtime_db

    runtime_db.record_free_paid_transition("recent", "p", "s", 0.1)
    runtime_db._get_conn().execute(
        "INSERT INTO free_paid_transitions"
        "(model, provider, detected_at, session_id, first_paid_cost_usd, first_free_seen_at)"
        " VALUES (?, ?, datetime('now', '-100 hours'), ?, ?, NULL)",
        ("old", "p", "s", 0.1),
    )
    out = plugin_api.tier_transitions(window_hours=72)
    assert [r["model"] for r in out["rows"]] == ["recent"]


def test_model_unavailable_empty(plugin_api):
    out = plugin_api.model_unavailable(window_hours=72)
    assert out == {"window_hours": 72, "rows": []}


def test_model_unavailable_returns_recorded_alert(plugin_api):
    import db as runtime_db

    runtime_db.record_model_unavailable(
        "nvidia/nemotron-3-ultra:free", "nous", 404, "Model not found"
    )
    out = plugin_api.model_unavailable(window_hours=72)
    assert out["window_hours"] == 72
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["model"] == "nvidia/nemotron-3-ultra:free"
    assert row["provider"] == "nous"
    assert row["error_code"] == 404
    assert row["occurrences"] == 1


def test_model_unavailable_window_filters_old_rows(plugin_api):
    import db as runtime_db

    runtime_db.record_model_unavailable("recent", "p", 404, "msg")
    runtime_db._get_conn().execute(
        "INSERT INTO model_unavailable_alerts"
        "(model, provider, error_code, error_message,"
        " first_seen_at, last_seen_at, occurrences)"
        " VALUES (?, ?, 404, 'msg',"
        " datetime('now', '-100 hours'),"
        " datetime('now', '-100 hours'), 1)",
        ("old", "p"),
    )
    out = plugin_api.model_unavailable(window_hours=72)
    assert [r["model"] for r in out["rows"]] == ["recent"]


def test_db_connection_is_read_only(plugin_api):
    """plugin_api opens the DB with PRAGMA query_only — writes must fail."""
    import sqlite3

    conn = plugin_api._conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO runs (session_id, started_at) VALUES ('x', 'y')")


def test_rollups_endpoint_reads_tiered_tables(plugin_api):
    """The /rollups endpoint returns bucketed history from the tiered tables.

    Seeds a session through the runtime API, folds it into the rollup tables
    via ``upsert_rollups``, then asserts /rollups surfaces the daily bucket
    with the expected cost aggregation.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[{"session_id": "r1", "model": "gpt-4o", "platform": "cli"}],
        rows_llm=[
            {
                "session_id": "r1",
                "ts": now,
                "model": "gpt-4o",
                "provider": "openai",
                "tokens_in": 200,
                "tokens_out": 100,
                "cost_usd": 0.005,
                "latency_ms": 200,
            }
        ],
    )
    import db as runtime_db

    runtime_db.upsert_rollups("r1")

    daily = plugin_api.rollups(granularity="day")
    assert daily["granularity"] == "day"
    assert daily["bucket_count"] == 1
    bucket = daily["rows"][0]
    assert bucket["model"] == "gpt-4o"
    assert bucket["provider"] == "openai"
    assert bucket["session_count"] == 1
    assert abs(bucket["cost_usd"] - 0.005) < 1e-9
    assert abs(daily["total_cost_usd"] - 0.005) < 1e-9


def test_rollups_endpoint_tier_aware_filters(plugin_api):
    """model/provider params narrow the rollup buckets (tier-aware query)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {"session_id": "r1", "model": "gpt-4o", "platform": "cli"},
            {"session_id": "r2", "model": "claude-sonnet-4-6", "platform": "cli"},
        ],
        rows_llm=[
            {
                "session_id": "r1",
                "ts": now,
                "model": "gpt-4o",
                "provider": "openai",
                "tokens_in": 200,
                "tokens_out": 100,
                "cost_usd": 0.005,
                "latency_ms": 200,
            },
            {
                "session_id": "r2",
                "ts": now,
                "model": "claude-sonnet-4-6",
                "provider": "anthropic",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": 0.001,
                "latency_ms": 150,
            },
        ],
    )
    import db as runtime_db

    runtime_db.upsert_rollups("r1")
    runtime_db.upsert_rollups("r2")

    filtered = plugin_api.rollups(granularity="day", model="gpt-4o")
    assert filtered["model"] == "gpt-4o"
    assert filtered["bucket_count"] == 1
    assert filtered["rows"][0]["provider"] == "openai"

    by_provider = plugin_api.rollups(granularity="day", provider="anthropic")
    assert by_provider["provider"] == "anthropic"
    assert by_provider["bucket_count"] == 1
    assert by_provider["rows"][0]["model"] == "claude-sonnet-4-6"


def test_rollups_endpoint_unknown_granularity_defaults_to_day(plugin_api):
    """An unrecognized granularity label must not crash and must default to day."""
    out = plugin_api.rollups(granularity="yearly")
    assert out["granularity"] == "day"
    assert out["bucket_count"] == 0
    assert out["rows"] == []
