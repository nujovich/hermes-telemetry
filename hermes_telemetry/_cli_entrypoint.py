"""Standalone entry point for hermes-telemetry CLI.

Registers this directory as the `hermes_telemetry` package so that relative
imports in telemetry_cli.py work, then delegates to main(). Same technique
as conftest.py — handles the hyphen/underscore directory name mismatch.
"""

import os
import sys
import types

_here = os.path.dirname(os.path.abspath(__file__))

if "hermes_telemetry" not in sys.modules:
    _pkg = types.ModuleType("hermes_telemetry")
    _pkg.__path__ = [_here]
    _pkg.__package__ = "hermes_telemetry"
    _pkg.__file__ = os.path.join(_here, "__init__.py")
    sys.modules["hermes_telemetry"] = _pkg

from hermes_telemetry.telemetry_cli import main  # noqa: E402

if __name__ == "__main__":
    main()
