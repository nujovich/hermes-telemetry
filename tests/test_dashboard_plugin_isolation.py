"""Guard: the standalone dashboard and the Hermes dashboard plugin share no code.

These two surfaces co-locate in ``dashboard/`` because the Hermes loader
requires plugin files at ``<plugin_root>/dashboard/manifest.json``. They
must remain code-isolated even though they live in the same directory:

- Standalone:  ``dashboard/serve.py`` + ``dashboard/index.html``
- Plugin:      ``dashboard/manifest.json`` + ``dashboard/plugin_api.py``
               + ``dashboard/dist/index.js``

These tests fail if a file from one surface imports a symbol from the other.
The plugin file set is identified by filename — the loader contract pins it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASH_DIR = REPO_ROOT / "dashboard"

# Filenames that belong to the plugin surface (loaded by Hermes' dashboard).
PLUGIN_FILES = {"plugin_api.py"}
# Filenames that belong to the standalone surface (stdlib HTTP server).
STANDALONE_FILES = {"serve.py"}


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


def test_plugin_api_is_self_contained():
    """plugin_api.py must not import serve.py (in any form)."""
    api = DASH_DIR / "plugin_api.py"
    imports = _module_imports(api)
    forbidden = {"serve", "dashboard.serve", "dashboard"}
    leak = imports & forbidden
    assert not leak, f"plugin_api.py leaks standalone imports: {leak}"


def test_standalone_serve_does_not_import_plugin_api():
    serve = DASH_DIR / "serve.py"
    imports = _module_imports(serve)
    forbidden = {"plugin_api", "dashboard.plugin_api"}
    leak = imports & forbidden
    assert not leak, f"serve.py leaks plugin imports: {leak}"


def test_no_shared_helpers_between_surfaces():
    """No third Python file in dashboard/ may be imported by both surfaces.

    This prevents a slow drift toward a shared 'utils' that re-couples them.
    """
    third_party = [
        p
        for p in DASH_DIR.glob("*.py")
        if p.name not in PLUGIN_FILES and p.name not in STANDALONE_FILES
    ]
    plugin_imports = _module_imports(DASH_DIR / "plugin_api.py")
    serve_imports = _module_imports(DASH_DIR / "serve.py")
    for helper in third_party:
        mod_name = helper.stem
        assert not (mod_name in plugin_imports and mod_name in serve_imports), (
            f"{helper.name} would re-couple plugin and standalone surfaces"
        )


def test_plugin_manifest_is_valid():
    import json

    manifest = json.loads((DASH_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "hermes-telemetry"
    assert manifest["entry"] == "dist/index.js"
    assert manifest["api"] == "plugin_api.py"
    # `api` must be a relative path inside dashboard/ — see
    # hermes_cli/web_server.py::_safe_plugin_api_relpath (GHSA-5qr3-c538-wm9j).
    assert not manifest["api"].startswith("/") and ".." not in manifest["api"]
    assert isinstance(manifest["slots"], list) and manifest["slots"]


def test_plugin_version_matches_package():
    import json

    manifest = json.loads((DASH_DIR / "manifest.json").read_text(encoding="utf-8"))
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
    """plugin_api.py must export `router` with all documented routes.

    FastAPI is a runtime dep of the Hermes dashboard, not of hermes-telemetry,
    so we skip live import when it's unavailable and fall back to an AST
    inspection that guarantees the same contract.
    """
    api_file = DASH_DIR / "plugin_api.py"

    try:
        import fastapi  # noqa: F401
    except ImportError:
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
        for endpoint in (
            '"/summary"',
            '"/health"',
            '"/runs"',
            '"/requests"',
            '"/providers"',
            '"/cron"',
            '"/budget"',
            '"/token-breakdown"',
        ):
            assert endpoint in src, f"plugin_api.py is missing route {endpoint}"
        assert '"/session/{session_id}"' in src
        return

    import importlib.util

    spec_pkg = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_telemetry", api_file
    )
    mod = importlib.util.module_from_spec(spec_pkg)
    spec_pkg.loader.exec_module(mod)
    assert hasattr(mod, "router"), "plugin_api.py must export `router`"
    paths = {r.path for r in mod.router.routes}
    expected = {
        "/summary",
        "/health",
        "/token-breakdown",
        "/runs",
        "/requests",
        "/providers",
        "/cron",
        "/session/{session_id}",
        "/budget",
    }
    missing = expected - paths
    assert not missing, f"plugin_api.py missing routes: {missing}"
