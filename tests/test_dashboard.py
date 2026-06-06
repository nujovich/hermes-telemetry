"""Tests for dashboard/serve.py CLI parsing and bind-warning behaviour."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The dashboard ships as a standalone script at dashboard/serve.py — not under
# the hermes_telemetry package — so import it by file path.
_SERVE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "serve.py"


@pytest.fixture
def serve_module():
    spec = importlib.util.spec_from_file_location("dashboard_serve", _SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_serve"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("dashboard_serve", None)


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults(serve_module):
    host, port = serve_module._parse_args([])
    assert host == "127.0.0.1"
    assert port == 8765


def test_parse_args_custom_port_flag(serve_module):
    host, port = serve_module._parse_args(["--port", "9090"])
    assert host == "127.0.0.1"
    assert port == 9090


def test_parse_args_custom_host_flag(serve_module):
    host, port = serve_module._parse_args(["--host", "0.0.0.0"])
    assert host == "0.0.0.0"
    assert port == 8765


def test_parse_args_host_and_port(serve_module):
    host, port = serve_module._parse_args(["--host", "192.168.1.42", "--port", "9999"])
    assert host == "192.168.1.42"
    assert port == 9999


def test_parse_args_positional_port_back_compat(serve_module):
    """Original usage `serve.py 9090` must keep working."""
    host, port = serve_module._parse_args(["9090"])
    assert host == "127.0.0.1"
    assert port == 9090


def test_parse_args_named_port_wins_over_positional(serve_module):
    """If a user passes both forms, --port takes precedence."""
    host, port = serve_module._parse_args(["--port", "5000", "8765"])
    assert port == 5000


# ---------------------------------------------------------------------------
# _warn_if_exposed
# ---------------------------------------------------------------------------


def test_warn_if_exposed_quiet_for_loopback(serve_module, capsys, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.dashboard"):
        serve_module._warn_if_exposed("127.0.0.1")
        serve_module._warn_if_exposed("localhost")
        serve_module._warn_if_exposed("::1")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_warn_if_exposed_warns_for_wildcard(serve_module, capsys, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.dashboard"):
        serve_module._warn_if_exposed("0.0.0.0")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "NO" in captured.err and "authentication" in captured.err
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_warn_if_exposed_warns_for_specific_lan_ip(serve_module, capsys):
    """Binding to a specific non-loopback IP still warrants the warning."""
    serve_module._warn_if_exposed("192.168.1.42")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "192.168.1.42" in captured.err
