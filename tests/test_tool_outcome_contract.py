"""Contract tests for authoritative Hermes post_tool_call status."""

import importlib.util
from pathlib import Path

import hermes_telemetry.budget as budget
import hermes_telemetry.db as db

_ROOT = Path(__file__).parent.parent
_SPEC = importlib.util.spec_from_file_location("hermes_telemetry", _ROOT / "__init__.py")
_PLUGIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PLUGIN)


class MockPluginContext:
    def __init__(self) -> None:
        self.hooks = {}

    def register_hook(self, name, fn) -> None:
        self.hooks[name] = fn

    def register_command(self, *_args, **_kwargs) -> None:
        pass

    def register_cli_command(self, *_args, **_kwargs) -> None:
        pass


def _post_tool_call(monkeypatch):
    monkeypatch.setattr(budget, "start_budget_watcher", lambda: None)
    monkeypatch.setattr(_PLUGIN, "_try_pricing_refresh", lambda _log: None)
    ctx = MockPluginContext()
    _PLUGIN.register(ctx)
    return ctx.hooks["post_tool_call"]


def test_runtime_ok_overrides_terminal_error_null(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="terminal",
        result='{"exit_code": 0, "error": null}',
        duration_ms=12,
        session_id="session-ok",
        status="ok",
    )

    assert [call["ok"] for call in calls] == [True]


def test_runtime_error_records_failure_even_with_error_null(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="terminal",
        result='{"exit_code": 1, "error": null}',
        duration_ms=12,
        session_id="session-error",
        status="error",
    )

    assert [call["ok"] for call in calls] == [False]


def test_runtime_error_records_failure_with_error_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="read_file",
        result='{"error": "permission denied"}',
        duration_ms=12,
        session_id="session-error-payload",
        status="error",
    )

    assert [call["ok"] for call in calls] == [False]


def test_runtime_ok_overrides_error_text_in_result(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="read_file",
        result='{"error": "text that runtime classified as ok"}',
        duration_ms=12,
        session_id="session-ok-text",
        status="ok",
    )

    assert [call["ok"] for call in calls] == [True]


def test_blocked_status_does_not_write_reliability_row(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="terminal",
        result='{"error": "blocked by middleware"}',
        duration_ms=0,
        session_id="session-blocked",
        status="blocked",
    )

    assert calls == []


def test_missing_status_does_not_write_reliability_row(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="terminal",
        result='{"exit_code": 0, "error": null}',
        duration_ms=12,
        session_id="session-missing",
    )

    assert calls == []


def test_unknown_status_does_not_write_reliability_row(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "record_tool_call", lambda **kwargs: calls.append(kwargs))
    callback = _post_tool_call(monkeypatch)

    callback(
        tool_name="terminal",
        result='{"exit_code": 0, "error": null}',
        duration_ms=12,
        session_id="session-unknown",
        status="future-status",
    )

    assert calls == []
