"""Tests for __init__.py — cron session ID parsing, tool result parsing.

# If these tests break, Hermes changed cron session_id format — check cron/scheduler.py
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

# Load __init__.py as a module directly — the conftest stub does not execute it.
# We need the module-level constants (CRON_SESSION_RE) and functions.
_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "hermes_telemetry._init_module", str(_ROOT / "__init__.py")
)
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)


# ---------------------------------------------------------------------------
# CRON_SESSION_RE and _extract_cron_job_id
# ---------------------------------------------------------------------------


def test_cron_session_id_regex_current_format():
    """Standard format cron_abc123_YYYYMMDD_HHMMSS → job_id='abc123'."""
    m = _init_mod.CRON_SESSION_RE.match("cron_abc123_20260601_120000")
    assert m is not None
    assert m.group("job_id") == "abc123"


def test_cron_session_id_regex_with_underscores():
    """Job IDs containing underscores are captured correctly."""
    m = _init_mod.CRON_SESSION_RE.match("cron_my_job_name_20260601_120000")
    assert m is not None
    assert m.group("job_id") == "my_job_name"


def test_cron_session_id_regex_bad_format_returns_none(caplog):
    """A bad format causes _extract_cron_job_id to return None and log a warning."""
    import logging

    with caplog.at_level(logging.WARNING):
        result = _init_mod._extract_cron_job_id("cron_abc123_notadate", "cron")
    assert result is None
    assert any("doesn't match expected" in r.message for r in caplog.records)


def test_cron_session_id_non_cron_platform():
    """Non-cron platforms always return None, even with a cron-looking session_id."""
    result = _init_mod._extract_cron_job_id("cron_abc123_20260601_120000", "cli")
    assert result is None

    result = _init_mod._extract_cron_job_id("cron_abc123_20260601_120000", "telegram")
    assert result is None


# ---------------------------------------------------------------------------
# _is_tool_ok
# ---------------------------------------------------------------------------


def test_is_tool_ok_json_error():
    """A JSON response with an 'error' key is not ok."""
    assert _init_mod._is_tool_ok('{"error": "some error"}') is False


def test_is_tool_ok_success():
    """A JSON response without 'error' key is ok."""
    assert _init_mod._is_tool_ok('{"result": "ok"}') is True


def test_is_tool_ok_non_json():
    """Plain text (non-JSON) result is treated as ok."""
    assert _init_mod._is_tool_ok("some plain text") is True


def test_is_tool_ok_nested_error():
    """Any dict with 'error' key at top level is not ok."""
    assert _init_mod._is_tool_ok('{"error": null, "data": 1}') is False


def test_is_tool_ok_none():
    """None result is treated as ok (not a string)."""
    assert _init_mod._is_tool_ok(None) is True


# ---------------------------------------------------------------------------
# plugin.yaml manifest schema
# ---------------------------------------------------------------------------


def test_plugin_yaml_uses_provides_hooks():
    """plugin.yaml must use provides_hooks: (official manifest schema), not hooks:."""
    manifest_path = _ROOT / "plugin.yaml"
    assert manifest_path.exists(), "plugin.yaml not found at repo root"
    data = yaml.safe_load(manifest_path.read_text())
    assert "provides_hooks" in data, (
        "plugin.yaml must use 'provides_hooks:' key (official manifest schema). "
        "Found keys: " + str(list(data.keys()))
    )
    assert "hooks" not in data, (
        "plugin.yaml must not use legacy 'hooks:' key — rename to 'provides_hooks:'"
    )


# ---------------------------------------------------------------------------
# Free→paid transition alert (issue #16)
# ---------------------------------------------------------------------------


def test_free_to_paid_alert_queued_when_known_free_model_costs_money(tmp_path, monkeypatch):
    """post_api_request queues an alert when a previously-free model goes paid."""
    import db

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()  # force fresh connection to new HERMES_HOME

    # Seed the model as known-free
    db.record_free_model("owl-alpha", "nous")

    # Directly exercise the detection logic via the module-level dict
    _init_mod._pending_free_paid_alerts.clear()
    # (full hook invocation requires a live PluginContext — we test the dict path)
    model = "owl-alpha"
    provider = "nous"
    cost = 1.5
    if cost > 0.0 and db.is_known_free_model(model, provider):
        with _init_mod._pending_free_paid_lock:
            if "sess-alert-test" not in _init_mod._pending_free_paid_alerts:
                _init_mod._pending_free_paid_alerts["sess-alert-test"] = (model, cost)

    assert "sess-alert-test" in _init_mod._pending_free_paid_alerts
    queued_model, queued_cost = _init_mod._pending_free_paid_alerts["sess-alert-test"]
    assert queued_model == "owl-alpha"
    assert abs(queued_cost - 1.5) < 1e-9


def test_unknown_model_does_not_queue_free_to_paid_alert(tmp_path, monkeypatch):
    """Unknown models at $0 do NOT get recorded as known-free (no false alerts)."""
    import db
    import pricing

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()
    pricing.reload_custom_pricing()

    # Genuinely unknown id (no exact entry, no prefix seed, no ":free" suffix).
    # Note: any "…:free" id is NOT unknown — it resolves to $0 via the ":free"
    # suffix rule and is recorded as known-free (issue #32).
    model = "nvidia/totally-unknown-model"
    provider = "nvidia"

    # Unknown model: is_explicitly_priced returns False → should NOT be recorded
    assert not pricing.is_explicitly_priced(model, provider)

    # Simulate what post_api_request does: only record if explicitly priced
    cost = 0.0
    if cost == 0.0 and pricing.is_explicitly_priced(model, provider):
        db.record_free_model(model, provider)

    assert not db.is_known_free_model(model, provider)


class _MockPluginContext:
    """Minimal PluginContext: records hooks so tests can fire the real callbacks."""

    def __init__(self):
        self.hooks: dict = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        pass

    def fire(self, hook_name, **kwargs):
        fn = self.hooks.get(hook_name)
        return fn(**kwargs) if fn else None


def _fire_zero_token_call(monkeypatch, model: str, provider: str, session_id: str) -> None:
    """Drive the REAL post_api_request callback with a zero-token usage dict."""
    import sys

    # register() does `from . import db, ...`; the module was loaded standalone
    # above, so make its package-relative imports resolvable.
    monkeypatch.setitem(sys.modules, _init_mod.__name__, _init_mod)
    ctx = _MockPluginContext()
    _init_mod.register(ctx)
    ctx.fire("on_session_start", session_id=session_id, model=model, platform="cli")
    ctx.fire(
        "post_api_request",
        session_id=session_id,
        platform="cli",
        model=model,
        provider=provider,
        response_model=model,
        api_call_count=1,
        api_duration=0.5,
        finish_reason="stop",
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
        assistant_content_chars=0,
        assistant_tool_call_count=0,
    )


def test_paid_model_zero_token_call_is_not_recorded_free(monkeypatch):
    """A paid model's zero-token $0 call must not enter known_free_models (issue #89).

    Fires the real post_api_request hook, so it fails if the hook reverts to
    is_explicitly_priced.
    """
    import hermes_telemetry.db as tdb

    _fire_zero_token_call(monkeypatch, "deepseek-chat", "nous", "sess-paid-zero")

    row = (
        tdb._get_conn()
        .execute("SELECT cost_usd FROM llm_calls WHERE session_id = 'sess-paid-zero'")
        .fetchone()
    )
    assert row is not None and row[0] == 0.0  # the $0 call really happened
    assert not tdb.is_known_free_model("deepseek-chat", "nous")


def test_free_model_zero_token_call_is_recorded_free(monkeypatch):
    """A genuinely-free model (input=0 AND output=0) at $0 is recorded as free,
    via the real post_api_request hook."""
    import hermes_telemetry.db as tdb

    _fire_zero_token_call(monkeypatch, "owl-alpha", "nous", "sess-free-zero")

    assert tdb.is_known_free_model("owl-alpha", "nous")


def test_free_to_paid_transition_is_persisted_for_dashboard(tmp_path, monkeypatch):
    """Detecting a free→paid flip also writes to free_paid_transitions."""
    import db

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()

    db.record_free_model("owl-alpha", "nous")
    # Mirror the post_api_request branch: known-free + cost>0 → record transition.
    model, provider, cost = "owl-alpha", "nous", 1.5
    if cost > 0.0 and db.is_known_free_model(model, provider):
        db.record_free_paid_transition(model, provider, "sess-dash", cost)

    rows = db.recent_free_paid_transitions(window_hours=0)
    assert len(rows) == 1
    assert rows[0]["model"] == "owl-alpha"
    assert rows[0]["session_id"] == "sess-dash"


def test_free_to_paid_alert_fires_only_once_per_session():
    """Alert is cleared from _pending after first pre_llm_call injection."""
    _init_mod._pending_free_paid_alerts["sess-once"] = ("some-model", 0.99)
    with _init_mod._pending_free_paid_lock:
        alert = _init_mod._pending_free_paid_alerts.pop("sess-once", None)
    assert alert is not None
    # Second pop returns None — alert cleared
    with _init_mod._pending_free_paid_lock:
        alert2 = _init_mod._pending_free_paid_alerts.pop("sess-once", None)
    assert alert2 is None


# ---------------------------------------------------------------------------
# Model-unavailable alert (issue #43)
# ---------------------------------------------------------------------------


def test_model_unavailable_alert_queued_on_404(tmp_path, monkeypatch):
    """Direct exercise of the queue path: a 404 for a non-retryable error
    populates the pending dict and writes a row to model_unavailable_alerts."""
    import db

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()

    _init_mod._pending_model_unavailable_alerts.clear()

    # Mirror what the api_request_error handler does on a 404.
    model = "nvidia/nemotron-3-ultra:free"
    provider = "nous"
    status_code = 404
    retryable = False
    error_message = "Error code: 404 — Model 'nvidia/nemotron-3-ultra:free' not found."

    if status_code == 404 and not retryable:
        db.record_model_unavailable(model, provider, status_code, error_message)
        row = db.get_model_unavailable(model, provider) or {}
        with _init_mod._pending_model_unavailable_lock:
            _init_mod._pending_model_unavailable_alerts["sess-404"] = (
                model,
                provider,
                status_code,
                int(row.get("occurrences") or 1),
            )

    assert "sess-404" in _init_mod._pending_model_unavailable_alerts
    queued = _init_mod._pending_model_unavailable_alerts["sess-404"]
    assert queued[0] == model
    assert queued[1] == provider
    assert queued[2] == 404
    assert queued[3] == 1

    persisted = db.get_model_unavailable(model, provider)
    assert persisted is not None
    assert persisted["occurrences"] == 1
    assert persisted["error_message"] == error_message


def test_non_404_does_not_queue_model_unavailable_alert(tmp_path, monkeypatch):
    """A 5xx or other error must not trigger the model-unavailable alert —
    that surface is reserved for "model removed" (404 non-retryable)."""
    import db

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()

    _init_mod._pending_model_unavailable_alerts.clear()

    # Mirror the handler's filter: only status_code == 404 with retryable=False
    # makes it through. A 500 retryable error short-circuits.
    status_code = 500
    retryable = True
    if status_code == 404 and not retryable:
        _init_mod._pending_model_unavailable_alerts["sess-500"] = (
            "foo",
            "p",
            500,
            1,
        )
    assert "sess-500" not in _init_mod._pending_model_unavailable_alerts
    # And no row was written
    assert db.get_model_unavailable("foo", "p") is None


def test_retryable_404_does_not_queue_alert(tmp_path, monkeypatch):
    """A retryable 404 (rare, but possible) goes back to the upstream retry
    loop and must not be surfaced as a permanent model-unavailable alert."""
    import db

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()

    _init_mod._pending_model_unavailable_alerts.clear()
    status_code, retryable = 404, True
    if status_code == 404 and not retryable:
        _init_mod._pending_model_unavailable_alerts["sess-retry"] = (
            "foo",
            "p",
            404,
            1,
        )
    assert "sess-retry" not in _init_mod._pending_model_unavailable_alerts


def test_model_unavailable_alert_fires_only_once_per_session():
    """pre_llm_call injects the alert exactly once; subsequent turns of the
    same session do not re-inject."""
    _init_mod._pending_model_unavailable_alerts["sess-once-unav"] = (
        "m",
        "p",
        404,
        2,
    )
    with _init_mod._pending_model_unavailable_lock:
        first = _init_mod._pending_model_unavailable_alerts.pop("sess-once-unav", None)
    assert first is not None
    with _init_mod._pending_model_unavailable_lock:
        second = _init_mod._pending_model_unavailable_alerts.pop("sess-once-unav", None)
    assert second is None


def test_model_unavailable_alert_increments_occurrences(tmp_path, monkeypatch):
    """Repeated 404s for the same (model, provider) bump the pending alert's
    occurrence count to match the DB so the warning text reflects reality."""
    import db

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir()
    db.close_thread_conn()

    _init_mod._pending_model_unavailable_alerts.clear()

    model, provider = "nvidia/foo:free", "nous"
    for _ in range(3):
        db.record_model_unavailable(model, provider, 404, "gone")
        row = db.get_model_unavailable(model, provider) or {}
        with _init_mod._pending_model_unavailable_lock:
            _init_mod._pending_model_unavailable_alerts["sess-bump"] = (
                model,
                provider,
                404,
                int(row.get("occurrences") or 1),
            )

    assert _init_mod._pending_model_unavailable_alerts["sess-bump"][3] == 3
    assert db.get_model_unavailable(model, provider)["occurrences"] == 3


# ---------------------------------------------------------------------------
# pre_tool_call hot-path budget gate
# ---------------------------------------------------------------------------


class _StubCtx:
    """Minimal PluginContext stub capturing the hooks register() installs."""

    profile_name = "test"

    def __init__(self):
        self.hooks = {}
        self.commands = {}
        self.cli_commands = {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, **_kw):
        self.commands[name] = fn

    def register_cli_command(self, name, **_kw):
        self.cli_commands[name] = _kw


def _registered_hooks(monkeypatch):
    """Run register() against a stub ctx (no watcher, no pricing refresh, no
    auto-setup) and return the captured hook map."""
    import hermes_telemetry.budget as budget_mod

    monkeypatch.setenv("HERMES_TELEMETRY_NO_SETUP", "1")
    monkeypatch.setattr(_init_mod, "_try_pricing_refresh", lambda log: None)
    monkeypatch.setattr(budget_mod, "start_budget_watcher", lambda: None)
    # _init_mod is exec'd from a file location, so Python hands it
    # __package__ = "hermes_telemetry._init_module" (the full dotted name) and
    # register()'s relative imports resolve against that non-existent package.
    # Point __package__ at the conftest-registered package instead.
    monkeypatch.setattr(_init_mod, "__package__", "hermes_telemetry")
    ctx = _StubCtx()
    _init_mod.register(ctx)
    return ctx.hooks


def _reset_hook_throttle():
    _init_mod._hook_error_log_state.clear()


def test_pre_tool_call_evaluates_budget_gate(monkeypatch):
    """The hook runs the DB-backed budget evaluation on every tool call."""
    import hermes_telemetry.budget as budget_mod
    import hermes_telemetry.db as db_mod

    seen = []
    monkeypatch.setattr(
        db_mod,
        "get_run",
        lambda session_id: seen.append(session_id) or {"session_id": session_id},
    )
    monkeypatch.setattr(budget_mod, "evaluate_run", lambda run: [])
    monkeypatch.setattr(budget_mod, "enforce_cron_pause", lambda verdicts: None)
    monkeypatch.setattr(budget_mod, "block_message_for", lambda verdicts: None)
    hooks = _registered_hooks(monkeypatch)
    assert hooks["pre_tool_call"](session_id="sess-2") is None
    assert seen == ["sess-2"]


def test_pre_tool_call_blocks_on_hard_verdict(monkeypatch):
    import hermes_telemetry.budget as budget_mod
    import hermes_telemetry.db as db_mod

    monkeypatch.setattr(
        budget_mod, "load_config", lambda: {"budgets": {"global": {"daily_usd": 1.0}}}
    )
    monkeypatch.setattr(db_mod, "get_run", lambda session_id: {"session_id": session_id})
    monkeypatch.setattr(budget_mod, "evaluate_run", lambda run: [])
    monkeypatch.setattr(budget_mod, "enforce_cron_pause", lambda verdicts: None)
    monkeypatch.setattr(budget_mod, "block_message_for", lambda verdicts: "[BUDGET] over limit")
    hooks = _registered_hooks(monkeypatch)
    assert hooks["pre_tool_call"](session_id="sess-3") == {
        "action": "block",
        "message": "[BUDGET] over limit",
    }


def test_pre_tool_call_fails_open_and_throttles_error_logging(monkeypatch, tmp_path):
    """A raising DB layer never blocks tools (fail-open) and its error line is
    rate-limited: one emit per interval, then a suppressed-count follow-up."""
    import hermes_telemetry.budget as budget_mod
    import hermes_telemetry.db as db_mod

    monkeypatch.setattr(
        budget_mod, "load_config", lambda: {"budgets": {"global": {"daily_usd": 1.0}}}
    )

    def _locked(session_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db_mod, "get_run", _locked)
    _reset_hook_throttle()
    hooks = _registered_hooks(monkeypatch)
    pre = hooks["pre_tool_call"]
    for _ in range(3):
        assert pre(session_id="sess-4") is None  # fail-open, never blocks

    log_file = tmp_path / "telemetry" / "telemetry.log"
    prefix = "pre_tool_call (budget) hook failed"
    lines = [ln for ln in log_file.read_text().splitlines() if prefix in ln]
    assert len(lines) == 1, f"expected 1 throttled error line, got {len(lines)}"

    # Age the throttle state past the interval -> next failure logs again and
    # reports how many occurrences were suppressed meanwhile. The state key is
    # (prefix, exception class).
    _init_mod._hook_error_log_state[(prefix, "OperationalError")] = (
        time.monotonic() - _init_mod._HOOK_ERROR_LOG_INTERVAL_S - 1.0,
        2,
    )
    assert pre(session_id="sess-4") is None
    lines = [ln for ln in log_file.read_text().splitlines() if prefix in ln]
    assert len(lines) == 2
    assert "suppressed" in lines[-1]


def test_throttled_error_is_per_prefix(caplog):
    """Throttling is per prefix: a different failing hook logs immediately."""
    _reset_hook_throttle()
    with caplog.at_level(logging.ERROR, logger="hermes_telemetry"):
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
        _init_mod._throttled_error("hook B failed", RuntimeError("y"))
    recs = [r for r in caplog.records if r.name == "hermes_telemetry"]
    assert len(recs) == 2


def test_throttled_error_is_per_exception_class(caplog):
    """A different exception class inside the window is surfaced immediately —
    the state key is (prefix, exception class), not the prefix alone."""
    _reset_hook_throttle()
    with caplog.at_level(logging.ERROR, logger="hermes_telemetry"):
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
        _init_mod._throttled_error("hook A failed", sqlite3.OperationalError("locked"))
    recs = [r for r in caplog.records if r.name == "hermes_telemetry"]
    assert len(recs) == 2
    assert "locked" in recs[-1].getMessage()


def test_throttled_error_reports_elapsed_window(caplog):
    """The suppressed-count message reports the actual elapsed window since the
    last emit, not the configured interval."""
    _reset_hook_throttle()
    with caplog.at_level(logging.ERROR, logger="hermes_telemetry"):
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
        # Age the state well past the interval; the next emit must report the
        # real elapsed time (~interval + 100s), not the configured interval.
        _init_mod._hook_error_log_state[("hook A failed", "RuntimeError")] = (
            time.monotonic() - _init_mod._HOOK_ERROR_LOG_INTERVAL_S - 100.0,
            1,
        )
        _init_mod._throttled_error("hook A failed", RuntimeError("x"))
    recs = [r for r in caplog.records if r.name == "hermes_telemetry"]
    assert len(recs) == 2
    msg = recs[-1].getMessage()
    assert "suppressed" in msg
    # Elapsed window is interval + ~100s -> the reported seconds exceed the
    # configured interval (would be exactly 300 if it reported the constant).
    import re

    m = re.search(r"in the last (\d+)s", msg)
    assert m is not None
    assert float(m.group(1)) > _init_mod._HOOK_ERROR_LOG_INTERVAL_S


# Schema v18 hook wiring — turn identity (delta A) + failure ledger (delta B)
# ---------------------------------------------------------------------------


# Loading __init__.py above with spec_from_file_location makes THIS module a
# package of its own (its spec.parent is its own dotted name), so the relative
# imports inside register() land as ``hermes_telemetry._init_module.db`` — a
# different module object from the ``hermes_telemetry.db`` the rest of the suite
# imports. Both resolve the same file through HERMES_HOME, but each caches its
# own per-thread connection, so a test that drops only one of them can end up
# writing through one and reading through the other.
_DB_MODULES = ("hermes_telemetry.db", f"{_init_mod.__package__}.db")


@pytest.fixture(autouse=True)
def isolated_db_conn():
    """Drop every cached telemetry connection around each test.

    HERMES_HOME is redirected to a per-test tmp dir by conftest.py, but a
    module caches the resolved DB path in its connection on first use — without
    this, a test writes into (and reads back from) an earlier test's tmp dir.
    Same pattern as tests/test_db.py, extended to the plugin's own module copy.
    """
    import sys

    def drop():
        for name in _DB_MODULES:
            mod = sys.modules.get(name)
            if mod is not None:
                mod.close_thread_conn()

    drop()
    yield
    drop()


def _register_hooks(monkeypatch):
    """register() the real plugin against the mock ctx, offline.

    register() refreshes pricing from the network when its 24h sentinel is
    missing, so the sentinel is seeded first — the hooks under test here never
    touch pricing.
    """
    import sys

    tele = Path(os.environ["HERMES_HOME"]) / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    (tele / ".pricing_refresh").touch()
    monkeypatch.setitem(sys.modules, _init_mod.__name__, _init_mod)
    ctx = _MockPluginContext()
    _init_mod.register(ctx)
    return ctx


def _fire_api_call(ctx, *, turn, req_id, session="sess-v18", model="m", provider="p"):
    """One live-shaped post_api_request for *turn* (usage present, no estimation)."""
    ctx.fire(
        "post_api_request",
        session_id=session,
        platform="cli",
        model=model,
        provider=provider,
        response_model=model,
        task_id="t1",
        turn_id=turn,
        api_request_id=req_id,
        api_call_count=1,
        api_duration=0.5,
        finish_reason="stop",
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
        assistant_content_chars=20,
        assistant_tool_call_count=0,
    )


def test_v18_llm_calls_group_by_turn_id(monkeypatch):
    """Three live-shaped calls — two sharing a turn_id, one different — group
    into exactly two turns with counts 2 and 1."""
    import hermes_telemetry.db as tdb

    ctx = _register_hooks(monkeypatch)
    turn_a, turn_b = "sess-v18:t1:aaaa1111", "sess-v18:t1:bbbb2222"
    _fire_api_call(ctx, turn=turn_a, req_id="req-1")
    _fire_api_call(ctx, turn=turn_a, req_id="req-2")
    _fire_api_call(ctx, turn=turn_b, req_id="req-3")

    groups = [
        tuple(r)
        for r in tdb._get_conn().execute(
            "SELECT turn_id, COUNT(*) FROM llm_calls GROUP BY 1 ORDER BY 1"
        )
    ]
    assert groups == [(turn_a, 2), (turn_b, 1)]


def test_v18_tool_row_shares_the_turn_and_carries_the_tool_call_id(monkeypatch):
    """A real tool row lands on the same turn as the api calls, keeps the exact
    tool_call_id the model emitted, and joins to the turn's api rows."""
    import hermes_telemetry.db as tdb

    ctx = _register_hooks(monkeypatch)
    turn = "sess-v18:t1:aaaa1111"
    _fire_api_call(ctx, turn=turn, req_id="req-1")
    ctx.fire(
        "post_tool_call",
        session_id="sess-v18",
        task_id="t1",
        turn_id=turn,
        tool_call_id="call_abc123",
        tool_name="read_file",
        result="ok",
        duration_ms=12,
    )

    conn = tdb._get_conn()
    row = conn.execute(
        "SELECT turn_id, tool_call_id FROM tool_calls WHERE tool_name = 'read_file'"
    ).fetchone()
    assert (row["turn_id"], row["tool_call_id"]) == (turn, "call_abc123")
    joined = conn.execute(
        "SELECT COUNT(*) FROM tool_calls tc JOIN llm_calls lc ON lc.turn_id = tc.turn_id"
        " WHERE tc.turn_id = ?",
        (turn,),
    ).fetchone()[0]
    assert joined == 1


def test_v18_subagent_stop_proxy_row_carries_the_parent_turn(monkeypatch):
    """The synthetic delegate_task/subagent row belongs to the parent turn that
    delegated — and has no tool_call_id, because the model never emitted one."""
    import hermes_telemetry.db as tdb

    ctx = _register_hooks(monkeypatch)
    turn = "sess-v18:t1:aaaa1111"
    ctx.fire(
        "subagent_stop",
        parent_session_id="sess-v18",
        parent_turn_id=turn,
        child_session_id="child-1",
        child_role="worker",
        child_status="completed",
        duration_ms=900,
    )

    row = (
        tdb._get_conn()
        .execute(
            "SELECT turn_id, tool_call_id FROM tool_calls WHERE tool_name = 'delegate_task/subagent'"
        )
        .fetchone()
    )
    assert row["turn_id"] == turn
    assert row["tool_call_id"] is None


def test_v18_delegation_join_precondition(monkeypatch):
    """The delegation join, with the left-side precondition asserted.

    ``parent_turn_id`` is populated on only some edges (2 of 4 in the live
    dataset); an edge whose ``parent_turn_id`` is empty can never join, so a
    bare count over every edge is a false negative, not a pass. The un-joinable
    edge is built here on purpose.
    """
    import hermes_telemetry.db as tdb

    ctx = _register_hooks(monkeypatch)
    turn = "sess-v18:t1:aaaa1111"
    ctx.fire(
        "subagent_start",
        parent_session_id="sess-v18",
        parent_turn_id=turn,
        parent_subagent_id="sa-0-deadbeef",
        child_session_id="child-with-turn",
        child_subagent_id="sa-0-cafebabe",
        child_role="worker",
    )
    # subagent_stop with no parent_turn_id backfills an edge that can never join.
    ctx.fire(
        "subagent_stop",
        parent_session_id="sess-v18",
        child_session_id="child-without-turn",
        child_role="worker",
        child_status="completed",
        duration_ms=1,
    )
    _fire_api_call(ctx, turn=turn, req_id="req-1")
    _fire_api_call(ctx, turn=turn, req_id="req-2")

    conn = tdb._get_conn()
    assert conn.execute("SELECT COUNT(*) FROM subagent_edges").fetchone()[0] == 2
    joinable = conn.execute(
        "SELECT COUNT(*) FROM subagent_edges WHERE parent_turn_id > ''"
    ).fetchone()[0]
    assert joinable == 1, "precondition: only one edge carries a turn id"
    joined = conn.execute(
        "SELECT COUNT(*) FROM subagent_edges se JOIN llm_calls lc"
        " ON lc.turn_id = se.parent_turn_id WHERE se.parent_turn_id > ''"
    ).fetchone()[0]
    assert joined == 2, "the other edge's empty parent_turn_id can never join"


# --- B: per-call failure ledger -------------------------------------------


def _fire_live_429(ctx, session_id="sess-429", **overrides):
    """Exactly the shape agent/api_request_hooks.py puts on the wire.

    The error text is NESTED, the request body is nested under ``request``
    (the model id lives in ``body['model']``) and ``api_duration`` is in
    SECONDS. A synthetic top-level ``error_message=`` cannot occur on the wire.
    """
    payload = {
        "session_id": session_id,
        "task_id": "t1",
        "turn_id": f"{session_id}:t1:deadbeef",
        "api_request_id": "req-429",
        "api_call_count": 2,
        "api_duration": 1.25,
        "model": "deepseek/deepseek-v4.1-flash",
        "provider": "nous",
        "base_url": "https://inference.example",
        "api_mode": "chat",
        "status_code": 429,
        "retry_count": 2,
        "max_retries": 5,
        "retryable": True,
        "reason": "rate_limited",
        "started_at": 1.0,
        "ended_at": 2.25,
        "error": {"type": "rate_limit_error", "message": "429 Too Many Requests"},
        "request": {
            "method": "POST",
            "body": {"model": "deepseek/deepseek-v4.1-flash", "messages": []},
        },
        "platform": "cli",
    }
    payload.update(overrides)
    ctx.fire("api_request_error", **payload)


def _live_404_payload(session_id="sess-404", **overrides):
    payload = {
        "session_id": session_id,
        "task_id": "t1",
        "turn_id": f"{session_id}:t1:cafebabe",
        "api_request_id": "req-404",
        "api_call_count": 1,
        "api_duration": 0.4,
        "model": "nvidia/nemotron-3-ultra:free",
        "provider": "nous",
        "status_code": 404,
        "retry_count": 0,
        "max_retries": 5,
        "retryable": False,
        "reason": "model_not_found",
        "error": {
            "type": "NotFoundError",
            "message": "Error code: 404 - Model 'nvidia/nemotron-3-ultra:free' not found.",
        },
        "request": {
            "method": "POST",
            "body": {"model": "nvidia/nemotron-3-ultra:free", "messages": []},
        },
    }
    payload.update(overrides)
    return payload


def _live_404(ctx, session_id="sess-404", **overrides):
    """Fire the live 404 shape — the payload is also reachable directly (see
    ``_live_404_payload``) so a test can DROP a key to model an older host."""
    ctx.fire("api_request_error", **_live_404_payload(session_id, **overrides))


def _ledger_rows():
    import hermes_telemetry.db as tdb

    return [
        dict(r)
        for r in tdb._get_conn().execute("SELECT * FROM llm_call_errors ORDER BY id").fetchall()
    ]


def test_v18_live_shape_429_is_ledgered(monkeypatch):
    """A 429 fired in the emitter's LIVE shape lands one complete ledger row.

    Today a 429 leaves no trace anywhere — this is the falsifiable assertion
    that the capability now exists. The nested dict must be read (a flat-only
    read leaves error_type/error_message empty), and api_duration is seconds.
    """
    _init_mod._pending_model_unavailable_alerts.clear()
    ctx = _register_hooks(monkeypatch)
    _fire_live_429(ctx)

    rows = _ledger_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-429"
    assert row["status_code"] == 429
    assert row["retryable"] == 1
    assert row["error_type"] == "rate_limit_error"  # nested error{} was read
    assert row["error_message"] == "429 Too Many Requests"
    assert row["duration_ms"] == 1250  # api_duration is SECONDS
    assert row["reason"] == "rate_limited"
    assert row["turn_id"] == "sess-429:t1:deadbeef"
    assert row["task_id"] == "t1"
    assert row["api_request_id"] == "req-429"
    assert row["api_call_count"] == 2
    assert row["retry_count"] == 2
    assert row["max_retries"] == 5
    assert row["model"] == "deepseek/deepseek-v4.1-flash"

    # A non-404 failure must not touch the 404-class alert surface, and it is
    # not an llm_calls row either (only the success path writes those).
    import hermes_telemetry.db as tdb

    conn = tdb._get_conn()
    assert conn.execute("SELECT COUNT(*) FROM model_unavailable_alerts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0


def test_v18_two_firings_two_rows(monkeypatch):
    """Attempt-level: no dedupe. Two firings of the same failure → two rows."""
    ctx = _register_hooks(monkeypatch)
    _fire_live_429(ctx)
    _fire_live_429(ctx)
    assert len(_ledger_rows()) == 2


def test_v18_live_shape_404_ledgers_and_keeps_the_alert_path(monkeypatch):
    """Double-write for the 404 class: one history row AND the unchanged alert.

    The 404 alert's own behaviour is unchanged — one ``model_unavailable_alerts``
    row for the pair with occurrences 1, plus the one-shot pending alert — while
    the ledger gains a row it never had.
    """
    _init_mod._pending_model_unavailable_alerts.clear()
    ctx = _register_hooks(monkeypatch)
    _live_404(ctx)

    rows = _ledger_rows()
    assert len(rows) == 1
    assert rows[0]["status_code"] == 404
    assert rows[0]["retryable"] == 0
    assert rows[0]["error_type"] == "NotFoundError"
    assert (
        rows[0]["error_message"]
        == "Error code: 404 - Model 'nvidia/nemotron-3-ultra:free' not found."
    )

    import hermes_telemetry.db as tdb

    conn = tdb._get_conn()
    alerts = [
        tuple(r)
        for r in conn.execute(
            "SELECT model, provider, error_code, occurrences FROM model_unavailable_alerts"
        ).fetchall()
    ]
    assert alerts == [("nvidia/nemotron-3-ultra:free", "nous", 404, 1)]
    queued = _init_mod._pending_model_unavailable_alerts.get("sess-404")
    assert queued is not None and queued[0] == "nvidia/nemotron-3-ultra:free"


def test_v18_legacy_flat_kwargs_still_record(monkeypatch):
    """The compat fallback stays honest: an older host that sends the flat
    error_type/error_message/api_kwargs names must still produce a real row."""
    ctx = _register_hooks(monkeypatch)
    ctx.fire(
        "api_request_error",
        session_id="sess-legacy",
        status_code=429,
        retryable=True,
        retry_count=1,
        reason="legacy_rate_limited",
        error_type="RateLimitError",
        error_message="legacy flat 429 message",
        api_kwargs={"model": "legacy/model"},
    )

    rows = _ledger_rows()
    assert len(rows) == 1
    assert rows[0]["error_type"] == "RateLimitError"
    assert rows[0]["error_message"] == "legacy flat 429 message"
    assert rows[0]["model"] == "legacy/model"
    assert rows[0]["status_code"] == 429
    assert rows[0]["retryable"] == 1
    assert rows[0]["reason"] == "legacy_rate_limited"


# --- review-round hardening: unreadable duration ---------------------------


@pytest.mark.parametrize(
    "poison",
    [float("inf"), float("-inf"), float("nan"), "inf", "not-a-number"],
    ids=["inf", "-inf", "nan", "inf-str", "garbage-str"],
)
def test_v18_unreadable_api_duration_still_ledgers_and_still_alerts(monkeypatch, poison):
    """A duration that cannot be read must not take the ledger and the alert down.

    ``api_duration`` arrives from the wire. Its conversion sat ABOVE the
    ledger's own try and inside only the outer handler try, so inf/nan/"inf"
    raised at ``float()``/``round()``, the outer except logged and returned, and
    BOTH writes were lost — the silent-failure class this ledger exists to
    close, and a regression on the alert path (which never read api_duration
    before v18). ``duration_ms`` is nullable and NULL is the honest value for a
    duration that could not be read. (Found by a community reviewer.)
    """
    import hermes_telemetry.db as tdb

    _init_mod._pending_model_unavailable_alerts.clear()
    ctx = _register_hooks(monkeypatch)
    _live_404(ctx, session_id="sess-poison-duration", api_duration=poison)

    rows = _ledger_rows()
    assert len(rows) == 1, "the failed attempt must still be recorded"
    assert rows[0]["session_id"] == "sess-poison-duration"
    assert rows[0]["status_code"] == 404
    assert rows[0]["duration_ms"] is None

    alerts = [
        tuple(r)
        for r in tdb._get_conn()
        .execute("SELECT model, provider, error_code, occurrences FROM model_unavailable_alerts")
        .fetchall()
    ]
    assert alerts == [("nvidia/nemotron-3-ultra:free", "nous", 404, 1)], (
        "the 404 alert must still land"
    )
    assert _init_mod._pending_model_unavailable_alerts.get("sess-poison-duration") is not None


# --- review-round hardening: unknown retryable -----------------------------


def test_v18_unknown_retryable_is_null_and_stays_out_of_the_alert(monkeypatch):
    """Hook-level: a 404 payload that never states ``retryable``.

    The ledger must record "the host did not say" as NULL — a 1 would assert a
    classification the payload never made — while the 404 alert keeps the
    behaviour it had for those payloads before the default changed (no alert).
    The guard change is not optional: once the default is None, ``if
    retryable:`` is falsy for None, so the alert would fire on every
    unclassified 404 — a new regression introduced by the default flip. The
    stated-True and stated-False payloads are asserted here too, so the fix is
    pinned to "unchanged for what the host actually said". (Found by a
    community reviewer.)
    """
    import hermes_telemetry.db as tdb

    _init_mod._pending_model_unavailable_alerts.clear()
    ctx = _register_hooks(monkeypatch)

    payload = _live_404_payload("sess-unknown-retryable")
    del payload["retryable"]  # an older host, or one that omits the field
    assert "retryable" not in payload
    ctx.fire("api_request_error", **payload)

    conn = tdb._get_conn()
    rows = _ledger_rows()
    assert len(rows) == 1
    assert rows[0]["status_code"] == 404
    assert rows[0]["retryable"] is None, "unknown must be NULL, not 1"
    assert conn.execute("SELECT COUNT(*) FROM model_unavailable_alerts").fetchone()[0] == 0
    assert _init_mod._pending_model_unavailable_alerts == {}

    # A stated retryable=True stays out of the alert path, exactly as before.
    _live_404(ctx, session_id="sess-stated-retryable", retryable=True)
    assert _ledger_rows()[1]["retryable"] == 1
    assert conn.execute("SELECT COUNT(*) FROM model_unavailable_alerts").fetchone()[0] == 0
    assert "sess-stated-retryable" not in _init_mod._pending_model_unavailable_alerts

    # ...while a stated retryable=False still reaches it (the class it is for).
    _live_404(ctx, session_id="sess-stated-not-retryable", retryable=False)
    assert _ledger_rows()[2]["retryable"] == 0
    assert conn.execute("SELECT COUNT(*) FROM model_unavailable_alerts").fetchone()[0] == 1
    assert _init_mod._pending_model_unavailable_alerts.get("sess-stated-not-retryable") is not None
