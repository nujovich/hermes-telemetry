"""Packaging contract tests.

These tests pin the pyproject.toml metadata that a real Hermes install relies on.
The plugin is loaded from source as a directory plugin, so its runtime
dependencies must be declared in [project].dependencies -- otherwise a build
frontend (uv workspace member, pip, etc.) installs only `watchdog` and the
plugin's `import yaml` fails at session start with "No module named 'yaml'".

See https://github.com/nujovich/hermes-telemetry/issues/113
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent


def _project_dependencies() -> str:
    """Return the raw text of the [project].dependencies list in pyproject.toml.

    Deliberately string-based (no tomllib) so the check runs on every Python
    version in the CI matrix (3.8 through 3.12).
    """
    text = (ROOT / "pyproject.toml").read_text()
    return text.split("dependencies = [", 1)[1].split("]", 1)[0]


def test_pyyaml_declared_as_runtime_dependency():
    """pyyaml is imported at runtime (pricing/budget/stats loaders) and must be
    declared so installers pull it in -- issue #113."""
    deps = _project_dependencies()
    assert "pyyaml" in deps
