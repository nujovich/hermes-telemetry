"""End-to-end tests for the dashboard plugin's read-only API.

These tests exercise the plugin endpoints as Python functions (no FastAPI
client) so they run in CI without pulling fastapi as a dev dep. The
isolation contract (HERMES_HOME → tmp dir) is enforced by conftest.py.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def plugin_api(monkeypatch):
    """Import dashboard_plugin.plugin_api fresh, skipping if fastapi missing.

    Mirrors the production order: the runtime creates ``telemetry.db`` (with
    its schema) before the dashboard plugin queries it. We trigger schema
    creation here by touching the runtime db module once.
    """
    fastapi = pytest.importorskip("fastapi")  # noqa: F841

    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(repo_root))
    # Drop any cached modules so the per-thread sqlite cache is fresh under the
    # new HERMES_HOME (set by the autouse fixture in conftest.py).
    for name in ("dashboard_plugin", "dashboard_plugin._db", "dashboard_plugin.plugin_api"):
        sys.modules.pop(name, None)
    # Force the runtime to materialize the DB + schema under the test HERMES_HOME.
    # The runtime caches a per-thread connection at module level; if a previous
    # test left one pointing at a now-deleted tmp dir, reset it.
    import threading as _t

    import db as runtime_db

    runtime_db._local = _t.local()
    runtime_db._get_conn()  # creates dir + tables on first call
    mod = importlib.import_module("dashboard_plugin.plugin_api")
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


def test_db_connection_is_read_only(plugin_api):
    """plugin_api opens the DB with PRAGMA query_only — writes must fail."""
    import sqlite3

    conn = plugin_api._db.conn()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO runs (session_id, started_at) VALUES ('x', 'y')")
