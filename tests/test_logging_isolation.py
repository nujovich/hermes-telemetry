"""Regression: the plugin's logger setup must not leak across tests.

``register()`` calls ``_setup_log_file()``, which adds a ``FileHandler`` and sets
``propagate = False`` on the process-global ``hermes_telemetry`` logger (so the
plugin's own logs go to its file instead of spamming the host's root logger).

That is a global side effect. Without restoration it leaks between tests: once
any test loads the plugin, ``propagate`` stays ``False`` for the rest of the
pytest process, so ``caplog`` (which captures at the root logger) stops seeing
``hermes_telemetry.*`` records — silently breaking every later test that asserts
on a captured warning (e.g. the pricing warning tests). An autouse fixture in
conftest.py must snapshot and restore the logger's mutable state around each
test. These two tests, run in order, prove it does.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

_ROOT = Path(__file__).parent.parent
import hermes_telemetry as _init_mod

# conftest registers `hermes_telemetry` as a bare shim; exec the real __init__.py
# onto it so `register` is available (same idiom as test_moa_integration.py).
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_spec.loader.exec_module(_init_mod)


class _NullCtx:
    def register_hook(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def register_command(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


def test_register_disables_propagation_within_the_test() -> None:
    """register() flips the plugin logger to propagate=False (documented side
    effect). The clean precondition also proves no earlier test leaked in."""
    lg = logging.getLogger("hermes_telemetry")
    assert lg.propagate is True
    _init_mod.register(_NullCtx())
    assert lg.propagate is False


def test_plugin_logger_state_restored_after_register() -> None:
    """Sentinel: the autouse restore fixture must undo the previous test's
    mutation. Without it, propagate is still False here and this fails."""
    assert logging.getLogger("hermes_telemetry").propagate is True
