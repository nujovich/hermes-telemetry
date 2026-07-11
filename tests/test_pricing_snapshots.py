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
