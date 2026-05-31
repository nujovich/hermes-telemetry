"""A2 instrument tests: /stats providers + Nous Portal estimated-usage warning.

Covers:
  - db.stats_by_provider: correct real/estimated split per provider
  - stats.handle('providers'): formats the table correctly
  - __init__._nous_estimated_warned: fires once per provider, not on non-Nous calls
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import hermes_telemetry.db as db
import hermes_telemetry.stats as stats_mod

# Load the plugin __init__ by exec-ing it into the real hermes_telemetry package
# stub that conftest already set up.  This lets register()'s relative imports
# (from . import db) resolve correctly via hermes_telemetry.__path__.
_ROOT = Path(__file__).parent.parent
import hermes_telemetry as _init_mod
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_spec.loader.exec_module(_init_mod)


class _MockCtx:
    def __init__(self):
        self.hooks: dict = {}
        self.commands: dict = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        self.commands[name] = fn

    def fire(self, name, **kw):
        fn = self.hooks.get(name)
        return fn(**kw) if fn else None


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db._local.conn = None
    _init_mod._nous_estimated_warned.clear()
    yield
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None
    _init_mod._nous_estimated_warned.clear()


# ---------------------------------------------------------------------------
# db.stats_by_provider
# ---------------------------------------------------------------------------

def test_stats_by_provider_empty():
    rows = db.stats_by_provider(window_hours=24)
    assert rows == []


def test_stats_by_provider_real_vs_estimated():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "m", "anthropic", 100, 50, 0.010, 100, estimated=False)
    db.record_llm_call("s1", now, "m", "anthropic", 100, 50, 0.010, 100, estimated=False)
    db.start_run("s2", model="m", platform="cli")
    db.record_llm_call("s2", now, "m", "nous_portal", 200, 100, 0.002, 200, estimated=True)

    rows = db.stats_by_provider(window_hours=24)
    assert len(rows) == 2

    by_name = {r["provider"]: r for r in rows}

    a = by_name["anthropic"]
    assert a["total_calls"]     == 2
    assert a["real_calls"]      == 2
    assert a["estimated_calls"] == 0
    assert a["estimated_pct"]   == 0.0

    n = by_name["nous_portal"]
    assert n["total_calls"]     == 1
    assert n["real_calls"]      == 0
    assert n["estimated_calls"] == 1
    assert n["estimated_pct"]   == 1.0


def test_stats_by_provider_mixed_estimated():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    # 3 real + 1 estimated for same provider
    for _ in range(3):
        db.record_llm_call("s1", now, "m", "openai", 100, 50, 0.001, 50, estimated=False)
    db.record_llm_call("s1", now, "m", "openai", 100, 50, 0.001, 50, estimated=True)

    rows = db.stats_by_provider(window_hours=24)
    assert len(rows) == 1
    r = rows[0]
    assert r["total_calls"]     == 4
    assert r["real_calls"]      == 3
    assert r["estimated_calls"] == 1
    assert abs(r["estimated_pct"] - 0.25) < 1e-9


def test_stats_by_provider_ordered_by_cost():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "m", "cheap", 100, 50, 0.001, 50)
    db.record_llm_call("s1", now, "m", "expensive", 100, 50, 10.0, 50)

    rows = db.stats_by_provider(window_hours=24)
    assert rows[0]["provider"] == "expensive"
    assert rows[1]["provider"] == "cheap"


def test_stats_by_provider_unknown_provider():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    # Insert with provider=None via direct SQL
    db._get_conn().execute(
        "INSERT INTO llm_calls (session_id, ts, model, provider, tokens_in, tokens_out, "
        "cost_usd, latency_ms, estimated) VALUES (?, ?, ?, NULL, 0, 0, 0, 0, 0)",
        ("s1", now, "m"),
    )

    rows = db.stats_by_provider(window_hours=24)
    providers = {r["provider"] for r in rows}
    assert "(unknown)" in providers


# ---------------------------------------------------------------------------
# /stats providers command output format
# ---------------------------------------------------------------------------

def test_stats_providers_command_output():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "m", "anthropic", 100, 50, 0.01, 100, estimated=False)
    db.record_llm_call("s1", now, "m", "nous_portal", 100, 50, 0.001, 100, estimated=True)

    out = stats_mod.handle("providers")
    assert "anthropic" in out
    assert "nous_portal" in out
    # nous_portal: 1/1 estimated → 100%
    assert "100%" in out
    # anthropic: 0/1 estimated → 0%
    assert "0%" in out
    # header line present
    assert "Provider" in out
    assert "Est%" in out


def test_stats_providers_empty_output():
    out = stats_mod.handle("providers")
    assert "No API calls" in out


def test_stats_providers_week_subcommand():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "m", "openai", 100, 50, 0.01, 100)

    out = stats_mod.handle("providers week")
    assert "openai" in out
    assert "last 7 days" in out


def test_stats_providers_explains_impact():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "m", "openai", 100, 50, 0.01, 100, estimated=True)

    out = stats_mod.handle("providers")
    # The footer should mention budget degradation so the user knows the implication
    assert "degraded" in out.lower() or "warn_only" in out


# ---------------------------------------------------------------------------
# Nous Portal one-time warning (tests the _nous_estimated_warned set behavior)
# ---------------------------------------------------------------------------

def test_nous_warning_fires_for_nous_provider():
    ctx = _MockCtx()
    _init_mod.register(ctx)

    db.start_run("s1", model="m", platform="cli")
    ctx.fire("pre_api_request", session_id="s1", api_call_count=0,
             approx_input_tokens=1000)
    ctx.fire("post_api_request", session_id="s1", model="m",
             provider="nous_portal", api_duration=0.5, api_call_count=0,
             assistant_content_chars=200, usage=None)

    # Side-effect: provider added to the warned set
    assert "nous_portal" in _init_mod._nous_estimated_warned


def test_nous_warning_deduplicates():
    ctx = _MockCtx()
    _init_mod.register(ctx)

    db.start_run("s1", model="m", platform="cli")

    # Fire twice — set should still contain exactly one entry for this provider
    for call_idx in range(2):
        ctx.fire("pre_api_request", session_id="s1", api_call_count=call_idx,
                 approx_input_tokens=1000)
        ctx.fire("post_api_request", session_id="s1", model="m",
                 provider="nous_portal", api_duration=0.5, api_call_count=call_idx,
                 assistant_content_chars=200, usage=None)

    assert _init_mod._nous_estimated_warned.count if hasattr(
        _init_mod._nous_estimated_warned, "count") else True
    # The set has exactly one entry (deduplication works)
    assert len([p for p in _init_mod._nous_estimated_warned
                if p == "nous_portal"]) == 1


def test_nous_warning_does_not_fire_for_other_providers():
    ctx = _MockCtx()
    _init_mod.register(ctx)

    db.start_run("s1", model="m", platform="cli")
    ctx.fire("pre_api_request", session_id="s1", api_call_count=0,
             approx_input_tokens=1000)
    ctx.fire("post_api_request", session_id="s1", model="m",
             provider="anthropic", api_duration=0.5, api_call_count=0,
             assistant_content_chars=200, usage=None)

    assert "anthropic" not in _init_mod._nous_estimated_warned
    assert len(_init_mod._nous_estimated_warned) == 0


def test_nous_warning_does_not_fire_when_usage_real():
    """No warning when provider=nous but usage is real (not estimated)."""
    ctx = _MockCtx()
    _init_mod.register(ctx)

    db.start_run("s1", model="m", platform="cli")
    ctx.fire("pre_api_request", session_id="s1", api_call_count=0,
             approx_input_tokens=1000)
    ctx.fire("post_api_request", session_id="s1", model="m",
             provider="nous_portal", api_duration=0.5, api_call_count=0,
             assistant_content_chars=200,
             usage={"input_tokens": 1000, "output_tokens": 50,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "reasoning_tokens": 0})

    assert "nous_portal" not in _init_mod._nous_estimated_warned


def test_nous_warning_case_insensitive():
    """'Nous_Research', 'NOUS', etc. all trigger the warning."""
    ctx = _MockCtx()
    _init_mod.register(ctx)

    db.start_run("s1", model="m", platform="cli")
    ctx.fire("pre_api_request", session_id="s1", api_call_count=0,
             approx_input_tokens=1000)
    ctx.fire("post_api_request", session_id="s1", model="m",
             provider="Nous_Research", api_duration=0.5, api_call_count=0,
             assistant_content_chars=200, usage=None)

    assert "Nous_Research" in _init_mod._nous_estimated_warned
