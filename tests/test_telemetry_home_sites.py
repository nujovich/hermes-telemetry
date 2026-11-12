"""Regression tests for issue #159: every telemetry-file site honors HERMES_TELEMETRY_HOME.

Each test points HERMES_TELEMETRY_HOME at a directory DIFFERENT from HERMES_HOME
and asserts the site resolves to the telemetry home (precedence documented in
paths.py: HERMES_TELEMETRY_HOME > HERMES_HOME > ~/.hermes).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
import hermes_telemetry as _pkg

_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_spec.loader.exec_module(_pkg)


@pytest.fixture
def split_homes(tmp_path, monkeypatch):
    """HERMES_TELEMETRY_HOME and HERMES_HOME point at different dirs."""
    tele_root = tmp_path / "shared"
    profile_home = tmp_path / "profile"
    tele_root.mkdir()
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tele_root))
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    return tele_root / "telemetry", profile_home / "telemetry"


def test_stats_providers_reads_pricing_from_telemetry_home(split_homes):
    import hermes_telemetry.db as db
    import hermes_telemetry.stats as stats_mod

    tele, decoy = split_homes
    tele.mkdir(parents=True)
    decoy.mkdir(parents=True)
    (tele / "pricing.yaml").write_text("_meta:\n  estimated_price_models: [shared/model]\n")
    (decoy / "pricing.yaml").write_text("_meta:\n  estimated_price_models: [decoy/model]\n")

    db._local.conn = None
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", db._utcnow(), "m", "anthropic", 10, 5, 0.01, 100, estimated=False)

    out = stats_mod.handle("providers")
    assert "shared/model" in out
    assert "decoy/model" not in out


def _load_plugin_api(monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.syspath_prepend(str(_ROOT))
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_telemetry_paths", _ROOT / "dashboard" / "plugin_api.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_api_budget_path_matches_db_dir(split_homes, monkeypatch):
    tele, _decoy = split_homes
    api = _load_plugin_api(monkeypatch)
    assert api._db_path().parent == tele
    assert api._budget_path() == tele / "budget.yaml"


def test_plugin_api_budget_path_falls_back_to_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_TELEMETRY_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    api = _load_plugin_api(monkeypatch)
    assert api._budget_path() == tmp_path / "telemetry" / "budget.yaml"


def test_serve_budget_dir_matches_db_dir(split_homes, monkeypatch):
    """serve.py already derives budget.yaml from DB_PATH.parent — pin it."""
    tele, _decoy = split_homes
    monkeypatch.syspath_prepend(str(_ROOT / "dashboard"))
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_serve_paths", _ROOT / "dashboard" / "serve.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DB_PATH.parent == tele


def test_setup_log_file_lands_in_telemetry_home(split_homes):
    tele, decoy = split_homes
    lg = logging.getLogger("hermes_telemetry")
    before = lg.handlers[:]
    _pkg._setup_log_file()
    added = [h for h in lg.handlers if h not in before]
    try:
        assert (tele / "telemetry.log").exists()
        assert not (decoy / "telemetry.log").exists()
    finally:
        for h in added:
            h.close()


def test_db_path_info_reports_the_real_db_path(split_homes):
    import hermes_telemetry.paths as paths

    tele, _decoy = split_homes
    assert _pkg._db_path_info() == str(tele / "telemetry.db")
    assert _pkg._db_path_info() == str(paths.get_telemetry_home() / "telemetry.db")


def test_pricing_refresh_sentinel_uses_telemetry_home(split_homes, monkeypatch):
    """Fresh sentinel in the telemetry home suppresses the remote refresh; a decoy
    sentinel in HERMES_HOME must not."""
    import hermes_telemetry.pricing_refresh as pr

    tele, decoy = split_homes
    tele.mkdir(parents=True)
    decoy.mkdir(parents=True)
    (tele / ".pricing_refresh").touch()

    called = []
    monkeypatch.setattr(pr, "refresh_pricing", lambda *a, **k: called.append(1) or ([], []))
    _pkg._try_pricing_refresh(logging.getLogger("test"))
    assert called == [], "fresh sentinel in the telemetry home must throttle the refresh"

    (tele / ".pricing_refresh").unlink()
    (decoy / ".pricing_refresh").touch()
    _pkg._try_pricing_refresh(logging.getLogger("test"))
    assert called == [1], "a decoy sentinel in HERMES_HOME must be ignored"
    assert (tele / ".pricing_refresh").exists()
