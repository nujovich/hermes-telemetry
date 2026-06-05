"""Tests for pricing hot-reload after /setup pricing auto (CAMBIO 4).

The running gateway caches the parsed pricing.yaml in pricing._custom_pricing.
After writing new prices, the flow must call pricing.reload_custom_pricing()
so estimate_cost() reflects them WITHOUT a gateway restart.
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

import hermes_telemetry.pricing as pricing
import hermes_telemetry.setup as setup
import pytest


@pytest.fixture(autouse=True)
def reset_pricing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telemetry").mkdir(parents=True, exist_ok=True)
    pricing._custom_pricing = None
    pricing._warned_unknown.clear()
    yield
    pricing._custom_pricing = None
    pricing._warned_unknown.clear()


def _write_yaml(tmp_path, body: str):
    (tmp_path / "telemetry" / "pricing.yaml").write_text(textwrap.dedent(body))


def _price(model: str) -> float:
    return pricing.estimate_cost({"input_tokens": 1_000_000}, model)


def test_cache_is_stale_until_reload(tmp_path):
    """Baseline: estimate_cost caches the YAML; an on-disk change is invisible
    until reload_custom_pricing() is called. This is exactly why a restart used
    to be required."""
    _write_yaml(
        tmp_path,
        """
        models:
          "hot/model":
            input: 1.0
            output: 1.0
        """,
    )
    pricing.reload_custom_pricing()
    assert _price("hot/model") == 1.0  # cached now

    # New price written to disk while the process keeps running.
    _write_yaml(
        tmp_path,
        """
        models:
          "hot/model":
            input: 9.0
            output: 9.0
        """,
    )
    # Still stale — proves the cache is real (the bug this change fixes).
    assert _price("hot/model") == 1.0

    # Reload picks up the new price with no restart.
    pricing.reload_custom_pricing()
    assert _price("hot/model") == 9.0


def test_setup_pricing_auto_hot_reloads_without_restart(tmp_path):
    """/setup pricing auto must hot-reload the cache itself: estimate_cost
    reflects the freshly-written price with NO manual reload in between."""
    # Old price is cached in-process.
    _write_yaml(
        tmp_path,
        """
        models:
          "hot/model":
            input: 1.0
            output: 1.0
        """,
    )
    pricing.reload_custom_pricing()
    assert _price("hot/model") == 1.0

    # Run the real /setup pricing auto flow; the fetch returns a new price.
    with patch.object(
        setup,
        "_fetch_openrouter_models",
        return_value={"hot/model": {"input": 7.0, "output": 7.0}},
    ):
        out = setup.handle_command("pricing auto")

    # Message no longer demands a restart.
    assert "restart" not in out.lower() or "no gateway restart" in out.lower()

    # No manual reload here — setup must have hot-reloaded the cache.
    assert _price("hot/model") == 7.0


def test_setup_pricing_minimal_hot_reloads(tmp_path):
    """/setup pricing minimal also refreshes the in-process cache."""
    # Cache a stale custom price for a built-in model.
    _write_yaml(
        tmp_path,
        """
        models:
          "gpt-4o":
            input: 999.0
            output: 999.0
        """,
    )
    pricing.reload_custom_pricing()
    assert _price("gpt-4o") == 999.0

    setup.handle_command("pricing minimal")

    # minimal rewrites with the built-in seed (gpt-4o input = 2.50); the cache
    # must reflect that immediately, not the stale 999.0.
    assert _price("gpt-4o") == 2.50
