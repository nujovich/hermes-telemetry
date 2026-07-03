"""pytest conftest — makes the plugin importable as 'hermes_telemetry'.

The plugin directory is named hermes-telemetry (hyphen, for Hermes discovery)
but Python package names use underscores. We register a module alias so the
tests can use `import hermes_telemetry.db` etc. without a real install.
"""

import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent

# Register 'hermes_telemetry' as a package pointing at this directory.
pkg = types.ModuleType("hermes_telemetry")
pkg.__path__ = [str(ROOT)]  # allows submodule discovery
pkg.__package__ = "hermes_telemetry"
pkg.__file__ = str(ROOT / "__init__.py")
sys.modules["hermes_telemetry"] = pkg


@pytest.fixture(autouse=True)
def isolate_hermes_home(tmp_path, monkeypatch):
    """Baseline isolation for every test: point HERMES_HOME at a fresh per-test
    tmp dir so no test ever reads the developer's real ~/.hermes files (DB,
    pricing.yaml, budget.yaml).

    Module-level fixtures may layer on top of this — a later monkeypatch.setenv
    wins (e.g. test_pricing seeds a committed fixture into its own dir), and
    tests that write into `tmp_path/telemetry` find HERMES_HOME already pointed
    there, since they share the same function-scoped tmp_path.

    HERMES_HOME is the primary mechanism. HOME/USERPROFILE are pinned too as a
    safety net: code resolves paths as `os.environ["HERMES_HOME"]` with a
    `Path.home() / ".hermes"` fallback, so even a stray `Path.home()` call (or a
    future regression) can only ever reach this tmp dir — never the developer's
    real ~/.hermes. See tests/test_isolation.py for the enforced contract.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows equivalent of HOME


@pytest.fixture(autouse=True)
def restore_plugin_logger_state():
    """Isolate the process-global ``hermes_telemetry`` logger between tests.

    ``register()`` → ``_setup_log_file()`` adds a ``FileHandler`` and sets
    ``propagate = False`` on the ``hermes_telemetry`` logger (so the plugin's
    logs go to its own file, not the host's root logger). That mutation is
    process-global and would otherwise leak: once any test loads the plugin,
    ``propagate`` stays ``False`` for the rest of the run, so ``caplog`` (which
    captures at the root logger) silently stops seeing ``hermes_telemetry.*``
    records — breaking every later warning-assertion test regardless of run
    order. Snapshot and restore the logger's mutable state around each test.
    See tests/test_logging_isolation.py for the enforced contract.
    """
    lg = logging.getLogger("hermes_telemetry")
    saved_propagate = lg.propagate
    saved_level = lg.level
    saved_handlers = lg.handlers[:]
    yield
    lg.propagate = saved_propagate
    lg.setLevel(saved_level)
    # Close handlers a test added (e.g. register()'s FileHandler) before dropping
    # them, so their open file descriptors aren't orphaned until process exit.
    for handler in lg.handlers:
        if handler not in saved_handlers:
            handler.close()
    lg.handlers[:] = saved_handlers
