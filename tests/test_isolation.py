"""Isolation contract — the test suite must never read or write the developer's
real ~/.hermes. HERMES_HOME is the single source of truth for locating Hermes
files; no code path may fall back to Path.home()/.hermes when HERMES_HOME is set.

These tests fail-closed: they plant a POISONED file at Path.home()/.hermes and
assert it is never consulted. They deliberately patch Path.home() directly, so
they catch escapes even though the conftest baseline also pins HOME as a safety
net (otherwise the net would mask the very bug we want to detect).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import hermes_telemetry.budget as budget
import hermes_telemetry.db as db


def test_path_helpers_resolve_under_hermes_home(tmp_path, monkeypatch):
    """The path helpers must derive from HERMES_HOME, not Path.home()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert budget._budget_path() == tmp_path / "telemetry" / "budget.yaml"
    assert budget._pricing_path() == tmp_path / "telemetry" / "pricing.yaml"


def test_budget_status_ignores_real_home_pricing(tmp_path, monkeypatch):
    """Regression for the Path.home() escape in _status_block: the estimated-
    price warning must read pricing.yaml from HERMES_HOME, never Path.home().

    HERMES_HOME gets a clean pricing.yaml (no estimated-price models); a decoy
    "real home" gets a poisoned one (with estimated-price models). If any code
    reads Path.home(), the warning surfaces and this test fails.
    """
    # HERMES_HOME → clean pricing.yaml
    hh = tmp_path / "hermes_home" / "telemetry"
    hh.mkdir(parents=True)
    (hh / "pricing.yaml").write_text("models: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))

    # Decoy "real home" → poisoned pricing.yaml (has estimated-price models)
    decoy_home = tmp_path / "decoy_home"
    decoy_tele = decoy_home / ".hermes" / "telemetry"
    decoy_tele.mkdir(parents=True)
    (decoy_tele / "pricing.yaml").write_text(
        textwrap.dedent(
            """
            models: {}
            _meta:
              estimated_price_models:
              - poison/should-never-be-read
            """
        )
    )
    monkeypatch.setattr(Path, "home", lambda: decoy_home)

    db._local.conn = None
    budget.reload_config()
    out = budget._status_block()

    # If _status_block read Path.home() (the decoy), the estimated-price warning
    # would appear. With HERMES_HOME clean, it must not.
    assert "estimated pricing" not in out, (
        "budget._status_block() read pricing.yaml from Path.home() instead of "
        "HERMES_HOME — real-home isolation is broken"
    )
