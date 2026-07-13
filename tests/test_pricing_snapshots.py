"""core_pricing seam + post_api_request pricing-snapshot capture (v14)."""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)


class MockPluginContext:
    """Minimal stand-in for Hermes PluginContext."""

    def __init__(self, profile_name="default"):
        self.hooks: dict = {}
        self.commands: dict = {}
        self.profile_name = profile_name

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        self.commands[name] = fn

    def register_cli_command(self, *a, **k):
        pass

    def fire(self, hook_name, **kwargs):
        fn = self.hooks.get(hook_name)
        return fn(**kwargs) if fn else None


@pytest.fixture(autouse=True)
def isolated_db():
    import hermes_telemetry.db as db_mod

    db_mod._local.conn = None
    _init_mod._pricing_snapshot_seen.clear()
    yield
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


# --- core_pricing.resolve -------------------------------------------------


def test_core_pricing_resolve_normalizes(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing

    class _Entry:
        input_cost_per_million = Decimal("3.00")
        output_cost_per_million = Decimal("15.00")
        cache_read_cost_per_million = None
        cache_write_cost_per_million = Decimal("3.75")
        request_cost = None
        source = "official_docs_snapshot"
        source_url = "https://example/pricing"
        pricing_version = "2026-07-01"
        fetched_at = datetime(2026, 7, 1, tzinfo=timezone.utc)

    fake_mod = types.ModuleType("agent.usage_pricing")
    fake_mod.get_pricing_entry = lambda model, provider=None, base_url=None, api_key="": _Entry()
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake_mod)

    snap = core_pricing.resolve("claude-sonnet-4-6", provider="anthropic")
    assert snap["input_cost_per_million"] == 3.0
    assert snap["output_cost_per_million"] == 15.0
    assert snap["cache_read_cost_per_million"] is None
    assert snap["cache_write_cost_per_million"] == 3.75
    assert snap["request_cost"] is None
    assert snap["source"] == "official_docs_snapshot"
    assert snap["source_url"] == "https://example/pricing"
    assert snap["pricing_version"] == "2026-07-01"
    assert snap["fetched_at"] == "2026-07-01T00:00:00+00:00"


def test_core_pricing_resolve_none_when_core_absent(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing

    # Guarantee no `agent` module is importable, regardless of test order.
    monkeypatch.setitem(sys.modules, "agent", None)
    assert core_pricing.resolve("whatever", provider="p") is None


def test_core_pricing_resolve_none_when_entry_none(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing

    fake_mod = types.ModuleType("agent.usage_pricing")
    fake_mod.get_pricing_entry = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake_mod)

    assert core_pricing.resolve("m", provider="p") is None


def test_core_pricing_resolve_failopen(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing

    def _boom(*a, **k):
        raise RuntimeError("core exploded")

    fake_mod = types.ModuleType("agent.usage_pricing")
    fake_mod.get_pricing_entry = _boom
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.usage_pricing", fake_mod)

    assert core_pricing.resolve("m", provider="p") is None


# --- canonical_model_name --------------------------------------------------


def test_canonical_model_name_strips_trailing_date():
    import hermes_telemetry.core_pricing as core_pricing

    assert (
        core_pricing.canonical_model_name("deepseek/deepseek-v4-pro-20260423")
        == "deepseek/deepseek-v4-pro"
    )


def test_canonical_model_name_strips_date_before_free_suffix():
    import hermes_telemetry.core_pricing as core_pricing

    assert core_pricing.canonical_model_name("tencent/hy3-20260706:free") == "tencent/hy3:free"


def test_canonical_model_name_unchanged_without_date():
    import hermes_telemetry.core_pricing as core_pricing

    assert (
        core_pricing.canonical_model_name("deepseek/deepseek-v4-pro") == "deepseek/deepseek-v4-pro"
    )
    assert core_pricing.canonical_model_name("gpt-4o") == "gpt-4o"


def test_canonical_model_name_ignores_non_date_digits():
    import hermes_telemetry.core_pricing as core_pricing

    # 7 digits (not 8) and a date not anchored to end/:free must NOT be stripped.
    assert core_pricing.canonical_model_name("model-1234567") == "model-1234567"
    assert core_pricing.canonical_model_name("foo-20260423-bar") == "foo-20260423-bar"


def test_canonical_model_name_strips_any_trailing_8_digits():
    import hermes_telemetry.core_pricing as core_pricing

    # Accepted heuristic: any trailing 8-digit token is stripped, even if not a
    # real date. Safe because canonicalization runs ONLY as a fallback after a
    # direct resolve() miss — a resolvable id never reaches this path.
    assert core_pricing.canonical_model_name("build-12345678") == "build"


# --- post_api_request capture --------------------------------------------


def _fake_resolve_factory(counter=None, snap=None):
    def _resolve(model, provider="", base_url=""):
        if counter is not None:
            counter["n"] += 1
        return (
            snap
            if snap is not None
            else {
                "input_cost_per_million": 3.0,
                "output_cost_per_million": 15.0,
                "cache_read_cost_per_million": None,
                "cache_write_cost_per_million": None,
                "request_cost": None,
                "source": "official_docs_snapshot",
                "source_url": None,
                "pricing_version": "2026-07-01",
                "fetched_at": None,
            }
        )

    return _resolve


def test_post_api_request_records_snapshot(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing
    import hermes_telemetry.db as db

    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve_factory())
    ctx = MockPluginContext()
    _init_mod.register(ctx)
    ctx.fire(
        "post_api_request",
        session_id="s1",
        model="claude-sonnet-4-6",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_mode="messages",
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    row = db.get_latest_pricing_snapshot("anthropic", "claude-sonnet-4-6")
    assert row is not None
    assert row["input_cost_per_million"] == 3.0
    assert row["pricing_version"] == "2026-07-01"
    assert row["base_url"] == "https://api.anthropic.com"
    assert row["api_mode"] == "messages"


def test_post_api_request_snapshot_is_throttled(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing

    counter = {"n": 0}
    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve_factory(counter))
    ctx = MockPluginContext()
    _init_mod.register(ctx)
    for _ in range(3):
        ctx.fire(
            "post_api_request",
            session_id="s",
            model="m",
            provider="p",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
    assert counter["n"] == 1  # resolve throttled after first (provider, model)


def test_post_api_request_snapshot_failopen(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing
    import hermes_telemetry.db as db

    def _boom(model, provider="", base_url=""):
        raise RuntimeError("core exploded")

    monkeypatch.setattr(core_pricing, "resolve", _boom)
    ctx = MockPluginContext()
    _init_mod.register(ctx)
    # Must not raise, and the llm_calls row must still be recorded.
    ctx.fire(
        "post_api_request",
        session_id="s",
        model="m",
        provider="p",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    assert db.get_latest_pricing_snapshot("p", "m") is None
    assert db.get_run("s") is not None  # the call itself was still recorded


def test_post_api_request_no_snapshot_when_resolve_none(monkeypatch):
    import hermes_telemetry.core_pricing as core_pricing
    import hermes_telemetry.db as db

    monkeypatch.setattr(core_pricing, "resolve", lambda *a, **k: None)
    ctx = MockPluginContext()
    _init_mod.register(ctx)
    ctx.fire(
        "post_api_request",
        session_id="s",
        model="m",
        provider="p",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    assert db.get_latest_pricing_snapshot("p", "m") is None
