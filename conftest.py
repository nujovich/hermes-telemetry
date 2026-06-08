"""pytest conftest — makes the plugin importable as 'hermes_telemetry'.

The plugin directory is named hermes-telemetry (hyphen, for Hermes discovery)
but Python package names use underscores. We register a module alias so the
tests can use `import hermes_telemetry.db` etc. without a real install.
"""

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
