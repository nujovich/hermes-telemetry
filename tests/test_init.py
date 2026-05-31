"""Tests for __init__.py — cron session ID parsing, tool result parsing.

# If these tests break, Hermes changed cron session_id format — check cron/scheduler.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

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
