"""Profile capture: on_session_start stores ctx.profile_name; pre_llm_call
backfills it (first non-null wins)."""

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)


class MockPluginContext:
    """Minimal stand-in for Hermes PluginContext, with a profile_name."""

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
    yield
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


def test_on_session_start_captures_profile():
    import hermes_telemetry.db as db

    ctx = MockPluginContext(profile_name="coder")
    _init_mod.register(ctx)
    ctx.fire("on_session_start", session_id="cap1", model="m", platform="cli")
    assert db.get_run("cap1")["profile"] == "coder"


def test_pre_llm_call_backfills_profile():
    import hermes_telemetry.db as db

    # Run exists without a profile (e.g. on_session_start never fired for it).
    db.start_run("cap2", "m", "cli")
    assert db.get_run("cap2")["profile"] is None

    ctx = MockPluginContext(profile_name="ops")
    _init_mod.register(ctx)
    ctx.fire("pre_llm_call", session_id="cap2")
    assert db.get_run("cap2")["profile"] == "ops"
