"""Regression tests for issue #160: an unreadable ``api_duration`` must cost the
latency field, never the ``llm_calls`` row.

Upstream (agent/turn_response_intake.py::_fire_post_api_request_hook) forwards
``api_duration`` as an untyped value (elapsed seconds, a float in practice), so
the hook treats it as untrusted, like the ``api_request_error`` path does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "hermes_telemetry._init_module_duration", str(_ROOT / "__init__.py")
)
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)


class _Ctx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, *a, **k):
        pass

    def fire(self, name, **kw):
        fn = self.hooks.get(name)
        return fn(**kw) if fn else None


@pytest.fixture(autouse=True)
def _fresh_db_conn():
    """Drop every cached per-thread telemetry connection around each test (the
    plugin and the test module may hold separate copies of ``db``)."""
    names = ("hermes_telemetry.db", f"{_init_mod.__package__}.db")

    def drop():
        for name in names:
            mod = sys.modules.get(name)
            if mod is not None:
                mod.close_thread_conn()

    drop()
    yield
    drop()


def _fire(monkeypatch, api_duration, session_id):
    import os

    tele = Path(os.environ["HERMES_HOME"]) / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    (tele / ".pricing_refresh").touch()  # keep register() offline
    monkeypatch.setitem(sys.modules, _init_mod.__name__, _init_mod)
    ctx = _Ctx()
    _init_mod.register(ctx)
    ctx.fire("on_session_start", session_id=session_id, model="m", platform="cli")
    ctx.fire(
        "post_api_request",
        session_id=session_id,
        platform="cli",
        model="deepseek-chat",
        provider="nous",
        response_model="deepseek-chat",
        api_call_count=1,
        api_duration=api_duration,
        finish_reason="stop",
        usage={
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
        assistant_content_chars=20,
        assistant_tool_call_count=0,
    )


def _rows(session_id):
    import hermes_telemetry.db as tdb

    return (
        tdb._get_conn()
        .execute(
            "SELECT cost_usd, tokens_in, tokens_out, latency_ms FROM llm_calls WHERE session_id = ?",
            (session_id,),
        )
        .fetchall()
    )


@pytest.mark.parametrize(
    "poison",
    [float("nan"), float("inf"), float("-inf"), None, "abc", "inf", 1e308],
    ids=["nan", "inf", "-inf", "none", "garbage-str", "inf-str", "overflow"],
)
def test_unreadable_api_duration_keeps_the_row(monkeypatch, request, poison):
    sid = "sess-dur-" + request.node.callspec.id
    _fire(monkeypatch, poison, sid)
    rows = _rows(sid)
    assert len(rows) == 1, "the llm_calls row must survive an unreadable duration"
    assert rows[0]["tokens_in"] == 1000
    assert rows[0]["tokens_out"] == 500
    assert rows[0]["cost_usd"] > 0, "cost must stay intact"
    assert rows[0]["latency_ms"] is None, "NULL is the honest value for an unreadable duration"


def test_readable_api_duration_still_recorded_in_ms(monkeypatch):
    sid = "sess-dur-ok"
    _fire(monkeypatch, 1.25, sid)
    assert _rows(sid)[0]["latency_ms"] == 1250


@pytest.mark.parametrize(
    "bad",
    [1e16, 1e300, -1.0, 86400.001],
    ids=["1e16", "1e300", "negative", "just-over-24h"],
)
def test_out_of_range_api_duration_stores_null_and_keeps_the_row(monkeypatch, request, bad):
    sid = "sess-dur-range-" + request.node.callspec.id
    _fire(monkeypatch, bad, sid)
    rows = _rows(sid)
    assert len(rows) == 1, "a finite but absurd duration must not drop the row"
    assert rows[0]["cost_usd"] > 0, "cost must stay intact"
    assert rows[0]["latency_ms"] is None


@pytest.mark.parametrize(
    ("ok", "expected"), [(0.0, 0), (86400.0, 86_400_000)], ids=["zero", "24h-boundary"]
)
def test_in_range_boundaries_are_kept(monkeypatch, request, ok, expected):
    sid = "sess-dur-bound-" + request.node.callspec.id
    _fire(monkeypatch, ok, sid)
    assert _rows(sid)[0]["latency_ms"] == expected
