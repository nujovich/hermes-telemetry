"""Exercise the plugin as a Hermes PM workspace member, not as an import stub."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_sync_installs_dependencies_without_building_plugin(tmp_path):
    uv = shutil.which("uv")
    if uv is None or sys.version_info < (3, 11):
        pytest.skip("uv and Python 3.11+ are required for Hermes PM integration")

    member = tmp_path / "plugin-sources" / "hermes-telemetry"
    shutil.copytree(
        ROOT,
        member,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", "*.egg-info", "venv", ".venv"
        ),
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "telemetry-workspace-probe"\nversion = "0"\n'
        'requires-python = ">=3.11"\n'
        "[tool.uv]\npackage = false\n"
        '[tool.uv.workspace]\nmembers = ["plugin-sources/hermes-telemetry"]\n'
    )
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    result = subprocess.run(
        [uv, "sync", "--all-packages", "--python", sys.executable, "--no-default-groups"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = subprocess.run(
        [
            str(tmp_path / ".venv" / "bin" / "python"),
            "-c",
            "import watchdog; print(watchdog.__file__)",
        ],
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    assert "building hermes-telemetry" not in result.stderr.lower()
