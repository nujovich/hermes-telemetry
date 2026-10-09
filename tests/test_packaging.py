"""Packaging contract tests.

These tests pin the pyproject.toml metadata that a real Hermes install relies on.
The plugin is loaded from source as a directory plugin, so its runtime
dependencies must be declared in [project].dependencies -- otherwise a build
frontend (uv workspace member, pip, etc.) installs only `watchdog` and the
plugin's `import yaml` fails at session start with "No module named 'yaml'".

See https://github.com/nujovich/hermes-telemetry/issues/113
"""

import re
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


def test_runtime_dependencies_have_upper_bounds():
    """Every runtime dependency must carry an upper bound below the next major.

    The plugin installs into Hermes' own environment, so an unbounded `>=` pin
    lets a future breaking major (watchdog 7, pyyaml 7) reach users on a plain
    reinstall with no change on our side. The ceiling is the next unreleased
    major, not the current one, so it does not fight Hermes' resolver."""
    specs = [
        line.split("#", 1)[0].strip().strip(",").strip('"')
        for line in _project_dependencies().splitlines()
    ]
    specs = [s for s in specs if s]
    assert specs, "no runtime dependencies parsed from pyproject.toml"
    for spec in specs:
        assert re.search(r",\s*<\s*\d", spec), f"{spec!r} has no upper bound"


def _tool_setuptools_section() -> str:
    """Return the raw [tool.setuptools*] config block from pyproject.toml."""
    text = (ROOT / "pyproject.toml").read_text()
    start = text.index("[tool.setuptools]")
    end = text.index("[tool.ruff]")
    return text[start:end]


def test_flat_layout_package_is_declared_explicitly():
    """Package discovery must map the flat-layout root to the hermes_telemetry
    package. `packages.find` with include=["hermes_telemetry*"] matched nothing
    because the modules live in the hyphenated hermes-telemetry/ working
    directory (there is no hermes_telemetry/ subpackage), so every build
    frontend aborted with "No distribution was found" -- issue #113."""
    section = _tool_setuptools_section()
    assert 'packages = ["hermes_telemetry"]' in section
    assert 'hermes_telemetry = "."' in section
    # The mapped package directory must actually hold the package initializer.
    assert (ROOT / "__init__.py").is_file()


def test_setup_wizard_does_not_shadow_setuptools():
    """The setup wizard must not be named setup.py: setuptools' PEP 517 backend
    exec's the project's setup.py, so a non-setup setup.py makes the build abort
    with "No distribution was found" regardless of package config -- issue #113."""
    assert not (ROOT / "setup.py").exists()
    assert (ROOT / "setup_wizard.py").is_file()
