"""Integration test: post_api_request correctly attributes MoA aggregator calls.

Hermes' MoA virtual provider fires post_api_request with provider="moa" and
model="<preset>", while usage/response_model belong to the aggregator. The
plugin must resolve the preset (monkeypatched here — the real resolver reads
Hermes' live config via hermes_cli) and record the call under the aggregator's
real provider/model, tagged with the preset name.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
import hermes_telemetry as _init_mod

_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_spec.loader.exec_module(_init_mod)


class MockPluginContext:
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
def isolated_db():
    import hermes_telemetry.db as db_mod

    db_mod._local.conn = None
    yield
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


def _fire_moa_call(ctx, session_id, *, response_model=None):
    ctx.fire("on_session_start", session_id=session_id, model="default", platform="cli")
    ctx.fire(
        "post_api_request",
        session_id=session_id,
        model="default",  # MoA reports the PRESET name here
        provider="moa",  # the virtual provider
        response_model=response_model,  # aggregator's real id (or None)
        api_duration=2.0,
        api_call_count=0,
        assistant_content_chars=400,
        usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
    )


def test_moa_call_attributed_to_aggregator(monkeypatch):
    """provider='moa' + response_model=aggregator id → row stored under the
    aggregator's real provider, model, and tagged with the preset name."""
    import hermes_telemetry.db as db
    import hermes_telemetry.moa as moa

    monkeypatch.setattr(
        moa,
        "_resolve_preset_via_hermes",
        lambda name: {
            "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
        },
    )

    ctx = MockPluginContext()
    _init_mod.register(ctx)
    _fire_moa_call(ctx, "moa-sess-1", response_model="anthropic/claude-opus-4.8")

    conn = db._get_conn()
    row = conn.execute(
        "SELECT provider, model, moa_preset FROM llm_calls WHERE session_id='moa-sess-1'"
    ).fetchone()
    assert row["provider"] == "openrouter"
    assert row["model"] == "anthropic/claude-opus-4.8"
    assert row["moa_preset"] == "default"

    run = conn.execute(
        "SELECT provider, moa_calls FROM runs WHERE session_id='moa-sess-1'"
    ).fetchone()
    assert run["provider"] == "openrouter"
    assert run["moa_calls"] == 1


def test_moa_call_falls_back_to_aggregator_model_when_response_model_missing(monkeypatch):
    """If the response omits its model id, the configured aggregator model is
    used — never the preset name."""
    import hermes_telemetry.db as db
    import hermes_telemetry.moa as moa

    monkeypatch.setattr(
        moa,
        "_resolve_preset_via_hermes",
        lambda name: {"aggregator": {"provider": "nous", "model": "hermes-4-405b"}},
    )

    ctx = MockPluginContext()
    _init_mod.register(ctx)
    _fire_moa_call(ctx, "moa-sess-2", response_model=None)

    row = (
        db._get_conn()
        .execute("SELECT provider, model FROM llm_calls WHERE session_id='moa-sess-2'")
        .fetchone()
    )
    assert row["provider"] == "nous"
    assert row["model"] == "hermes-4-405b"


def test_moa_call_unresolvable_preset_falls_back_gracefully(monkeypatch):
    """If the preset can't be resolved (no hermes_cli / unknown preset), the
    call is still recorded — under provider 'moa' — and tagged with the preset
    name, rather than crashing or being dropped."""
    import hermes_telemetry.db as db
    import hermes_telemetry.moa as moa

    monkeypatch.setattr(moa, "_resolve_preset_via_hermes", lambda name: None)

    ctx = MockPluginContext()
    _init_mod.register(ctx)
    _fire_moa_call(ctx, "moa-sess-3", response_model=None)

    row = (
        db._get_conn()
        .execute("SELECT provider, model, moa_preset FROM llm_calls WHERE session_id='moa-sess-3'")
        .fetchone()
    )
    # Falls back to the raw hook values (current pre-integration behavior) but
    # still records the preset marker so the blind spot stays visible.
    assert row["provider"] == "moa"
    assert row["model"] == "default"
    assert row["moa_preset"] == "default"
