"""Guard: the dashboard plugin and the standalone dashboard share no code.

These two surfaces are intentionally independent — see CLAUDE.md and the
PR that introduced ``dashboard_plugin/``. This test fails if anyone wires
an import from one into the other.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STANDALONE_DIR = REPO_ROOT / "dashboard"
PLUGIN_DIR = REPO_ROOT / "dashboard_plugin"


def _module_imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _iter_py(root: Path):
    return (p for p in root.rglob("*.py") if p.is_file())


def test_plugin_does_not_import_standalone():
    offenders = []
    for py in _iter_py(PLUGIN_DIR):
        for mod in _module_imports(py):
            if mod.startswith("dashboard") and not mod.startswith("dashboard_plugin"):
                offenders.append((py.relative_to(REPO_ROOT), mod))
    assert not offenders, (
        f"dashboard_plugin must not import from the standalone dashboard: {offenders}"
    )


def test_standalone_does_not_import_plugin():
    offenders = []
    for py in _iter_py(STANDALONE_DIR):
        for mod in _module_imports(py):
            if mod.startswith("dashboard_plugin"):
                offenders.append((py.relative_to(REPO_ROOT), mod))
    assert not offenders, f"dashboard/ must not import from dashboard_plugin/: {offenders}"


def test_plugin_manifest_is_valid():
    import json

    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "hermes-telemetry"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["api"] == "plugin_api.py"
    # `api` must be a relative path inside dashboard/ — see
    # hermes_cli/web_server.py::_safe_plugin_api_relpath (GHSA-5qr3-c538-wm9j).
    assert not manifest["api"].startswith("/") and ".." not in manifest["api"]
    assert isinstance(manifest["slots"], list) and manifest["slots"]


def test_plugin_version_matches_package():
    import json

    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    init_src = (REPO_ROOT / "__init__.py").read_text(encoding="utf-8")
    # Parse __version__ literal without importing the package (avoids side effects).
    for line in init_src.splitlines():
        line = line.strip()
        if line.startswith("__version__"):
            pkg_version = line.split("=", 1)[1].strip().strip("\"'")
            break
    else:
        raise AssertionError("__version__ not found in __init__.py")
    assert manifest["version"] == pkg_version, (
        f"manifest.version={manifest['version']} must match __version__={pkg_version}"
    )


def test_plugin_api_exposes_router():
    """plugin_api.py must export `router` with /summary and /health.

    FastAPI is a runtime dep of the Hermes dashboard, not of hermes-telemetry,
    so we skip live import when it's unavailable and fall back to an AST
    inspection that guarantees the same contract.
    """
    api_file = PLUGIN_DIR / "plugin_api.py"

    try:
        import fastapi  # noqa: F401
    except ImportError:
        # AST fallback: verify the file declares a `router` and routes /summary
        # + /health. This is good enough to prevent regressions; full wiring is
        # exercised inside the Hermes dashboard process.
        tree = ast.parse(api_file.read_text(encoding="utf-8"))
        names = {
            t.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        assert "router" in names, "plugin_api.py must assign a top-level `router`"
        src = api_file.read_text(encoding="utf-8")
        assert '"/summary"' in src and '"/health"' in src
        return

    import importlib.util
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    try:
        import dashboard_plugin  # noqa: F401

        spec_pkg = importlib.util.spec_from_file_location("dashboard_plugin.plugin_api", api_file)
        mod = importlib.util.module_from_spec(spec_pkg)
        spec_pkg.loader.exec_module(mod)
        assert hasattr(mod, "router"), "plugin_api.py must export `router`"
        paths = {r.path for r in mod.router.routes}
        assert "/summary" in paths
        assert "/health" in paths
    finally:
        sys.path.pop(0)
