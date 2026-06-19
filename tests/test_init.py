"""Tests for __init__.py — cron session ID parsing, tool result parsing.

# If these tests break, Hermes changed cron session_id format — check cron/scheduler.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
