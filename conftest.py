"""pytest conftest — makes the plugin importable as 'hermes_telemetry'.

The plugin directory is named hermes-telemetry (hyphen, for Hermes discovery)
but Python package names use underscores. We register a module alias so the
tests can use `import hermes_telemetry.db` etc. without a real install.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent

# Register 'hermes_telemetry' as a package pointing at this directory.
pkg = types.ModuleType("hermes_telemetry")
pkg.__path__ = [str(ROOT)]  # allows submodule discovery
pkg.__package__ = "hermes_telemetry"
pkg.__file__ = str(ROOT / "__init__.py")
sys.modules["hermes_telemetry"] = pkg
