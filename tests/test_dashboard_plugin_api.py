"""End-to-end tests for the dashboard plugin's read-only API.

These tests exercise the plugin endpoints as Python functions (no FastAPI
client) so they run in CI without pulling fastapi as a dev dep. The
isolation contract (HERMES_HOME → tmp dir) is enforced by conftest.py.
"""

from __future__ import annotations

import importlib.util
import os
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


def test_summary_and_requests_expose_moa(plugin_api):
    """The /summary and /requests endpoints surface MoA attribution
    (moa_calls in the summary, moa_preset per request)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[{"session_id": "sm", "model": "anthropic/claude-opus-4.8", "platform": "cli"}],
        rows_llm=[
            {
                "session_id": "sm",
                "ts": now,
                "model": "anthropic/claude-opus-4.8",
                "provider": "openrouter",
                "tokens_in": 100,
                "tokens_out": 50,
                "cost_usd": 0.01,
                "latency_ms": 200,
                "moa_preset": "default",
            },
        ],
    )

    summary = plugin_api.summary(window_hours=24)
    assert int(summary["runs"].get("moa_calls") or 0) == 1

    reqs = plugin_api.requests(limit=10, window_hours=24)
    presets = {r["model"]: r.get("moa_preset") for r in reqs["rows"]}
    assert presets["anthropic/claude-opus-4.8"] == "default"


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


def test_desktop_aggregate_empty(plugin_api):
    """/desktop returns an aggregate payload the Desktop panel can poll,
    with sane empty-state defaults when no runs exist yet."""
    out = plugin_api.desktop()
    assert out["plugin"] == "hermes-telemetry"
    assert out["surface"] == "desktop"
    assert out["last_run"] is None
    assert out["session_count"] == 0
    assert out["running_count"] == 0
    assert out["month_to_date"]["cost_usd"] == 0.0
    # budget() returns {'enabled': False} when no budget.yaml is present.
    assert out["budget"]["enabled"] is False
    assert out["actions"]["open_dashboard"] == "/desktop/open-dashboard"
    assert out["actions"]["pause_cron"] == "/cron"


def test_desktop_aggregate_with_run_and_budget(plugin_api, tmp_path, monkeypatch):
    """/desktop surfaces last-run status, session count, month-to-date spend,
    and the budget scopes read from budget.yaml."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {
                "session_id": "s1",
                "model": "gpt-4o",
                "platform": "cron",
            },
            {
                "session_id": "s2",
                "model": "gpt-4o",
                "platform": "cli",
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
                "cost_usd": 0.5,
                "latency_ms": 100,
            },
        ],
    )
    import db as runtime_db

    # s1 stays 'running' (default); s2 is finalized as 'ok'.
    runtime_db.end_run("s2", "ok")

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

    out = plugin_api.desktop()
    assert out["session_count"] == 2
    assert out["running_count"] == 1
    assert out["last_run"] is not None
    assert out["last_run"]["status"] in ("running", "ok")
    # Month-to-date cost sums runs.cost_usd; only s1 recorded an llm_call
    # (cost_usd 0.5), so the aggregate is 0.5.
    assert out["month_to_date"]["cost_usd"] == 0.5
    assert out["budget"]["enabled"] is True
    scopes = {s["scope"]: s for s in out["budget"]["scopes"]}
    assert "global/daily" in scopes and "global/monthly" in scopes


def test_desktop_open_dashboard_action(plugin_api):
    """/desktop/open-dashboard returns the standalone dashboard deep link and a
    reachability flag (no real server running in tests -> reachable False)."""
    out = plugin_api.desktop_open_dashboard()
    assert out["url"] == "http://localhost:8765"
    assert out["reachable"] is False
    assert out["parsed_host"] == "localhost"
    assert "serve.py" in out["hint"]
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


def test_db_path_honors_telemetry_home(plugin_api, tmp_path, monkeypatch):
    """_db_path resolves the consolidated DB when HERMES_TELEMETRY_HOME is set."""
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    assert plugin_api._db_path() == tmp_path / "shared" / "telemetry" / "telemetry.db"


def test_profiles_lists_distinct_non_null(plugin_api):
    _seed(
        rows_runs=[
            {"session_id": "p1", "model": "m", "platform": "cli", "profile": "coder"},
            {"session_id": "p2", "model": "m", "platform": "cli", "profile": "ops"},
            {"session_id": "p3", "model": "m", "platform": "cli", "profile": "coder"},
            {"session_id": "p4", "model": "m", "platform": "cli"},  # NULL profile
        ]
    )
    out = plugin_api.profiles()
    assert out["profiles"] == ["coder", "ops"]


def _seed_two_profiles():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {
                "session_id": "c1",
                "model": "m",
                "platform": "cli",
                "profile": "coder",
                "cron_job_id": "job_a",
            },
            {
                "session_id": "o1",
                "model": "m",
                "platform": "cli",
                "profile": "ops",
                "cron_job_id": "job_b",
            },
        ],
        rows_llm=[
            {
                "session_id": "c1",
                "ts": now,
                "model": "m",
                "provider": "nous",
                "tokens_in": 100,
                "tokens_out": 10,
                "cost_usd": 1.0,
                "latency_ms": 5,
            },
            {
                "session_id": "o1",
                "ts": now,
                "model": "m",
                "provider": "openai",
                "tokens_in": 200,
                "tokens_out": 20,
                "cost_usd": 2.0,
                "latency_ms": 9,
            },
        ],
    )


def test_summary_filters_by_profile(plugin_api):
    _seed_two_profiles()
    out = plugin_api.summary(window_hours=24, profile="coder")
    assert out["runs"]["total_runs"] == 1
    assert out["runs"]["tokens_in"] == 100
    assert out["llm"]["api_calls"] == 1


def test_runs_filters_by_profile(plugin_api):
    _seed_two_profiles()
    out = plugin_api.runs(limit=50, window_hours=24, profile="ops")
    assert out["total_runs"] == 1
    assert [r["session_id"] for r in out["rows"]] == ["o1"]


def test_requests_filters_by_profile(plugin_api):
    _seed_two_profiles()
    out = plugin_api.requests(limit=100, window_hours=24, profile="coder")
    assert out["total_requests"] == 1
    assert all(r["provider"] == "nous" for r in out["rows"])


def test_providers_filters_by_profile(plugin_api):
    _seed_two_profiles()
    out = plugin_api.providers(window_hours=24, profile="ops")
    assert {r["provider"] for r in out["rows"]} == {"openai"}


def test_cron_filters_by_profile(plugin_api):
    _seed_two_profiles()
    out = plugin_api.cron(window_hours=168, profile="coder")
    assert [r["cron_job_id"] for r in out["rows"]] == ["job_a"]


def test_token_breakdown_filters_by_profile(plugin_api):
    _seed_two_profiles()
    out = plugin_api.token_breakdown(window_hours=24, profile="ops")
    assert out["tokens_in"] == 200


def test_no_profile_returns_all(plugin_api):
    _seed_two_profiles()
    assert plugin_api.summary(window_hours=24)["runs"]["total_runs"] == 2
    assert plugin_api.runs(limit=50, window_hours=24)["total_runs"] == 2


def test_summary_http_query_param_filters_by_profile(plugin_api):
    """FastAPI wires ?profile= as an optional query param (direct-call tests
    bypass this layer, so lock the HTTP wiring here)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _seed_two_profiles()
    app = FastAPI()
    app.include_router(plugin_api.router)
    client = TestClient(app)

    filtered = client.get("/summary", params={"profile": "coder"}).json()
    assert filtered["runs"]["total_runs"] == 1
    assert filtered["runs"]["tokens_in"] == 100

    unfiltered = client.get("/summary").json()
    assert unfiltered["runs"]["total_runs"] == 2


def test_efficiency_endpoint_penalizes_error(plugin_api):
    """/efficiency scores completed sessions; 'error' carries the heavy penalty."""
    from datetime import datetime, timezone

    import db as runtime_db

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {"session_id": "e_ok", "model": "m", "platform": "cli"},
            {"session_id": "e_err", "model": "m", "platform": "cli"},
        ],
        rows_llm=[
            {
                "session_id": "e_ok",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 100,
                "tokens_out": 100,
                "cost_usd": 0.001,
                "latency_ms": 50,
            },
            {
                "session_id": "e_err",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 100,
                "tokens_out": 100,
                "cost_usd": 0.001,
                "latency_ms": 50,
            },
        ],
    )
    runtime_db.end_run("e_ok", "ok")
    runtime_db.end_run("e_err", "error")

    out = plugin_api.efficiency(window_hours=24)
    by_sid = {s["session_id"]: s for s in out["sessions"]}
    # ok:    40 + 40 - 0  - 1.5 = 78.5 ; error: 40 + 40 - 30 - 1.5 = 48.5
    assert by_sid["e_ok"]["efficiency_score"] == 78.5
    assert by_sid["e_err"]["efficiency_score"] == 48.5


def test_smells_endpoint_detects_context_rotation(plugin_api):
    """/smells surfaces anti-patterns over existing telemetry."""
    from datetime import datetime, timezone

    import db as runtime_db

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[{"session_id": "sr", "model": "m", "platform": "cli"}],
        rows_llm=[
            {
                "session_id": "sr",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10000,
                "tokens_out": 500,
                "cost_usd": 0.05,
                "latency_ms": 100,
            }
        ],
    )
    runtime_db.end_run("sr", "ok")

    out = plugin_api.smells(window_hours=24)
    assert out["count"] >= 1
    assert any(s["smell"] == "context_rotation" for s in out["smells"])


def test_forecast_endpoint_reads_global_limit(plugin_api):
    """/forecast projects burn rate against the configured global limit."""
    import os
    from datetime import datetime, timezone

    import yaml

    import db as runtime_db

    budget_path = plugin_api._budget_path()
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    budget_path.write_text(yaml.safe_dump({"budgets": {"global": {"daily_usd": 10.0}}}))
    assert os.path.exists(budget_path)

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[{"session_id": "f1", "model": "m", "platform": "cli"}],
        rows_llm=[
            {
                "session_id": "f1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10,
                "tokens_out": 10,
                "cost_usd": 2.0,
                "latency_ms": 10,
            }
        ],
    )
    runtime_db.end_run("f1", "ok")

    out = plugin_api.forecast(window="daily")
    assert out["enabled"] is True
    assert out["limit_usd"] == 10.0
    assert out["spent_so_far_usd"] >= 2.0
    assert out["status"] in ("ok", "soft", "hard")


def test_efficiency_filters_by_profile(plugin_api):
    """/efficiency scopes scored sessions to a single profile when asked."""
    from datetime import datetime, timezone

    import db as runtime_db

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {"session_id": "c1", "model": "m", "platform": "cli", "profile": "coder"},
            {"session_id": "o1", "model": "m", "platform": "cli", "profile": "ops"},
        ],
        rows_llm=[
            {
                "session_id": "c1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 100,
                "tokens_out": 100,
                "cost_usd": 0.001,
                "latency_ms": 50,
            },
            {
                "session_id": "o1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 100,
                "tokens_out": 100,
                "cost_usd": 0.001,
                "latency_ms": 50,
            },
        ],
    )
    runtime_db.end_run("c1", "ok")
    runtime_db.end_run("o1", "ok")

    out = plugin_api.efficiency(window_hours=24, profile="coder")
    assert [s["session_id"] for s in out["sessions"]] == ["c1"]
    assert out["sessions_scored"] == 1

    # No profile = both sessions (unchanged behavior).
    assert plugin_api.efficiency(window_hours=24)["sessions_scored"] == 2


def test_smells_filters_by_profile(plugin_api):
    """/smells scopes detected anti-patterns to a single profile when asked."""
    from datetime import datetime, timezone

    import db as runtime_db

    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {"session_id": "c1", "model": "m", "platform": "cli", "profile": "coder"},
            {"session_id": "o1", "model": "m", "platform": "cli", "profile": "ops"},
        ],
        rows_llm=[
            {
                "session_id": "c1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10000,
                "tokens_out": 500,
                "cost_usd": 0.05,
                "latency_ms": 100,
            },
            {
                "session_id": "o1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10000,
                "tokens_out": 500,
                "cost_usd": 0.05,
                "latency_ms": 100,
            },
        ],
    )
    runtime_db.end_run("c1", "ok")
    runtime_db.end_run("o1", "ok")

    out = plugin_api.smells(window_hours=24, profile="coder")
    assert {s["session_id"] for s in out["smells"]} == {"c1"}

    # No profile = smells from both profiles.
    assert {s["session_id"] for s in plugin_api.smells(window_hours=24)["smells"]} == {"c1", "o1"}


def test_budget_includes_per_profile_scopes(plugin_api):
    """/budget emits a scope per profile present in runs, using override-else-default limits."""
    import os
    from datetime import datetime, timezone

    import db as runtime_db

    hermes_home = Path(os.environ["HERMES_HOME"])
    (hermes_home / "telemetry").mkdir(parents=True, exist_ok=True)
    (hermes_home / "telemetry" / "budget.yaml").write_text(
        "budgets:\n"
        "  global:\n"
        "    daily_usd: 100.0\n"
        "  per_profile:\n"
        "    default:\n"
        "      daily_usd: 2.0\n"
        "    overrides:\n"
        "      coder:\n"
        "        daily_usd: 10.0\n"
        "thresholds:\n"
        "  soft_pct: 0.8\n"
        "  hard_pct: 1.0\n",
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[
            {"session_id": "c1", "model": "m", "platform": "cli", "profile": "coder"},
            {"session_id": "o1", "model": "m", "platform": "cli", "profile": "ops"},
        ],
        rows_llm=[
            {
                "session_id": "c1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10,
                "tokens_out": 10,
                "cost_usd": 3.0,
                "latency_ms": 5,
            },
            {
                "session_id": "o1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10,
                "tokens_out": 10,
                "cost_usd": 1.0,
                "latency_ms": 5,
            },
        ],
    )
    runtime_db.end_run("c1", "ok")
    runtime_db.end_run("o1", "ok")

    out = plugin_api.budget()
    scopes = {s["scope"]: s for s in out["scopes"]}
    assert "global/daily" in scopes
    assert scopes["profile:coder/daily"]["scope_id"] == "coder"
    assert scopes["profile:coder/daily"]["limit_usd"] == 10.0
    assert scopes["profile:coder/daily"]["spent_usd"] == 3.0
    assert scopes["profile:ops/daily"]["limit_usd"] == 2.0
    assert scopes["profile:ops/daily"]["spent_usd"] == 1.0


def test_budget_no_per_profile_block_stays_global(plugin_api):
    """With no per_profile config, /budget emits only global scopes (unchanged)."""
    import os

    hermes_home = Path(os.environ["HERMES_HOME"])
    (hermes_home / "telemetry").mkdir(parents=True, exist_ok=True)
    (hermes_home / "telemetry" / "budget.yaml").write_text(
        "budgets:\n  global:\n    daily_usd: 1.0\n",
        encoding="utf-8",
    )
    out = plugin_api.budget()
    assert all(s["scope"].startswith("global/") for s in out["scopes"])


def test_budget_empty_profile_override_falls_back_to_default(plugin_api):
    """An empty per-profile override ({}) falls back to the default limit,
    matching the runtime budget engine (budget._resolve_limits)."""
    import os
    from datetime import datetime, timezone

    import db as runtime_db

    hermes_home = Path(os.environ["HERMES_HOME"])
    (hermes_home / "telemetry").mkdir(parents=True, exist_ok=True)
    (hermes_home / "telemetry" / "budget.yaml").write_text(
        "budgets:\n"
        "  global:\n"
        "    daily_usd: 100.0\n"
        "  per_profile:\n"
        "    default:\n"
        "      daily_usd: 5.0\n"
        "    overrides:\n"
        "      coder: {}\n",
        encoding="utf-8",
    )
    now = datetime.now(timezone.utc).isoformat()
    _seed(
        rows_runs=[{"session_id": "c1", "model": "m", "platform": "cli", "profile": "coder"}],
        rows_llm=[
            {
                "session_id": "c1",
                "ts": now,
                "model": "m",
                "provider": "p",
                "tokens_in": 10,
                "tokens_out": 10,
                "cost_usd": 1.0,
                "latency_ms": 5,
            },
        ],
    )
    runtime_db.end_run("c1", "ok")

    out = plugin_api.budget()
    scopes = {s["scope"]: s for s in out["scopes"]}
    assert scopes["profile:coder/daily"]["limit_usd"] == 5.0
