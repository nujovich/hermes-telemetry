"""Observation vs enforcement mode — upgrade-safe defaults and opt-in observe."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import hermes_telemetry as _init_mod
import hermes_telemetry.budget as budget
import hermes_telemetry.db as db
import pytest
import yaml

_ROOT = Path(__file__).parent.parent

# Exec the real __init__.py onto the hermes_telemetry package so register()'s
# relative imports resolve (same idiom as test_moa_integration.py).
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_spec.loader.exec_module(_init_mod)


class MockPluginContext:
    def __init__(self, settings=None):
        self.hooks: dict = {}
        self.commands: dict = {}
        self._settings = settings or {}

    def get_config(self, key: str, default=None):
        return self._settings.get(key, default)

    def register_hook(self, name: str, fn) -> None:
        self.hooks[name] = fn

    def register_command(self, name: str, fn, description="", args_hint="") -> None:
        self.commands[name] = fn

    def fire(self, hook_name: str, **kwargs):
        fn = self.hooks.get(hook_name)
        return fn(**kwargs) if fn else None


class MockPluginContextNoConfig:
    """Older Hermes hosts expose no get_config API."""

    def __init__(self):
        self.hooks: dict = {}
        self.commands: dict = {}

    def register_hook(self, name: str, fn) -> None:
        self.hooks[name] = fn

    def register_command(self, name: str, fn, description="", args_hint="") -> None:
        self.commands[name] = fn

    def fire(self, hook_name: str, **kwargs):
        fn = self.hooks.get(hook_name)
        return fn(**kwargs) if fn else None


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("HERMES_TELEMETRY_NO_SETUP", "1")
    db._local.conn = None
    budget.reload_config()
    budget._enforcement_mode = budget._DEFAULT_MODE
    # Allow start_budget_watcher to be re-entered across tests without leaking
    # a real Observer into the suite (watchdog may be absent — that's fine).
    budget.stop_budget_watcher()
    yield
    budget.stop_budget_watcher()
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None
    budget.reload_config()
    budget._enforcement_mode = budget._DEFAULT_MODE


def _write_budget(tmp_path: Path, body: str) -> None:
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    (tele / "budget.yaml").write_text(textwrap.dedent(body))
    budget.reload_config()


def _seed(session_id: str, cost: float) -> None:
    db.start_run(session_id, model="claude-sonnet-4-6", platform="cli")
    db.record_llm_call(
        session_id,
        ts=db._utcnow(),
        model="claude-sonnet-4-6",
        provider="test",
        tokens_in=0,
        tokens_out=0,
        cost_usd=cost,
        latency_ms=0,
    )


def test_manifest_defaults_mode_to_enforce():
    """Guardrails stay on unless the operator opts into observe."""
    data = yaml.safe_load((_ROOT / "plugin.yaml").read_text())
    mode = data["config_schema"]["mode"]
    assert mode["default"] == "enforce"
    assert mode["choices"] == ["observe", "enforce"]


def test_upgrade_path_existing_budget_no_mode_still_blocks(tmp_path, monkeypatch):
    """Existing budget.yaml + no mode setting must still register pre_tool_call
    and block at the hard cap (the #112 review upgrade path)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("upgrade-sess", 6.00)

    # Older Hermes hosts have no get_config at all.
    ctx = MockPluginContextNoConfig()
    assert not hasattr(ctx, "get_config")

    _init_mod.register(ctx)

    assert "pre_tool_call" in ctx.hooks
    assert budget.get_enforcement_mode() == "enforce"
    result = ctx.fire("pre_tool_call", session_id="upgrade-sess")
    assert isinstance(result, dict)
    assert result.get("action") == "block"
    assert "blocked" in (result.get("message") or "").lower()


def test_default_get_config_still_enforces(tmp_path, monkeypatch):
    """get_config present but mode unset → default enforce."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 1.00\n")
    _seed("default-sess", 2.00)

    ctx = MockPluginContext(settings={})  # get_config returns default=
    _init_mod.register(ctx)
    assert "pre_tool_call" in ctx.hooks
    result = ctx.fire("pre_tool_call", session_id="default-sess")
    assert result and result.get("action") == "block"


def test_observe_mode_skips_pre_tool_call_but_keeps_watcher(tmp_path, monkeypatch):
    """observe is opt-in: no tool gate, but the budget watcher still starts."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 1.00\n")
    _seed("observe-sess", 2.00)

    started = []
    monkeypatch.setattr(budget, "start_budget_watcher", lambda: started.append(True))

    ctx = MockPluginContext(settings={"mode": "observe"})
    _init_mod.register(ctx)

    assert "pre_tool_call" not in ctx.hooks
    assert started == [True]
    assert budget.get_enforcement_mode() == "observe"
    # /budget must say limits are not enforced when budget.yaml is present
    status = budget.handle("")
    assert "NOT enforced" in status


def test_invalid_mode_falls_back_to_enforce(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = MockPluginContext(settings={"mode": "wat"})
    _init_mod.register(ctx)
    assert "pre_tool_call" in ctx.hooks
    assert budget.get_enforcement_mode() == "enforce"
