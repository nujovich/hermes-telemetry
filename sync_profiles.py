"""Point every Hermes profile at one shared telemetry home.

Each Hermes profile is an independent HERMES_HOME (``~/.hermes`` for the default
profile, ``~/.hermes/profiles/<name>/`` for named ones), with its own
``config.yaml``, ``.env`` and ``plugins/``. Telemetry files (``telemetry.db``,
``pricing.yaml``, ``budget.yaml``) resolve under ``<HERMES_TELEMETRY_HOME>/
telemetry/`` (precedence ``HERMES_TELEMETRY_HOME`` > ``HERMES_HOME`` >
``~/.hermes``). Writing the same ``HERMES_TELEMETRY_HOME`` into every profile's
``.env`` therefore makes all profiles read the same pricing/budget and write to
the same DB.

This module is self-contained (no intra-package imports) so it can be unit
tested against a tmp HERMES_HOME. The ONLY file it ever writes is each profile's
``.env``; ``config.yaml`` is read-only (drift warning), never edited.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

PLUGIN_NAME = "hermes-telemetry"
ENV_KEY = "HERMES_TELEMETRY_HOME"


def default_base_home() -> Path:
    """The home to enumerate profiles from (HERMES_HOME, or ~/.hermes)."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def resolve_target_home() -> Path:
    """Shared home ROOT to write into each profile's .env. Mirrors the
    precedence of paths.get_telemetry_home() (HERMES_TELEMETRY_HOME >
    HERMES_HOME > ~/.hermes) but returns the root (paths appends /telemetry)."""
    override = os.environ.get("HERMES_TELEMETRY_HOME") or os.environ.get("HERMES_HOME")
    return Path(override) if override else Path.home() / ".hermes"


def is_default_profile(base_home: Path) -> bool:
    """True when base_home is NOT a named profile (~/.hermes/profiles/<name>).
    Mirrors Hermes' own path-shape inference of the active profile."""
    return base_home.resolve().parent.name != "profiles"


def iter_profiles(base_home: Path) -> list[tuple[str, Path]]:
    """(name, home) for the default profile + every ~/.hermes/profiles/<name>/.
    'default' first, then named profiles sorted by name."""
    out: list[tuple[str, Path]] = [("default", base_home)]
    profiles_root = base_home / "profiles"
    if profiles_root.is_dir():
        for child in sorted(profiles_root.iterdir()):
            if child.is_dir():
                out.append((child.name, child))
    return out


def _is_assignment(stripped: str, key: str) -> bool:
    if stripped.startswith("#"):
        return False
    return stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}=")


def read_env_var(env_path: Path, key: str) -> str | None:
    """Value of KEY from a line-based .env (last assignment wins), or None."""
    if not env_path.is_file():
        return None
    value: str | None = None
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if _is_assignment(stripped, key):
            value = stripped.split("=", 1)[1]
    return value


def upsert_env_var(env_path: Path, key: str, value: str) -> bool:
    """Idempotently set KEY=value in a line-based .env, keeping every other line
    and comment (an existing `export ` prefix on the key is preserved). Replaces
    the first assignment, drops later duplicates, else appends. Returns True if
    the file changed. Atomic write (tmp + os.replace); creates the file (and
    parents) if absent. Note: line endings are normalized to `\\n`."""
    new_line = f"{key}={value}"
    existing = env_path.read_text().splitlines() if env_path.is_file() else []
    out: list[str] = []
    replaced = False
    changed = False
    for line in existing:
        if _is_assignment(line.strip(), key):
            if not replaced:
                replaced = True
                prefix = "export " if line.strip().startswith("export ") else ""
                candidate = f"{prefix}{new_line}"
                if line != candidate:
                    changed = True
                out.append(candidate)
            else:
                changed = True  # dropping a later duplicate
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
        changed = True
    if not changed:
        return False
    text = "\n".join(out) + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.parent / (env_path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, env_path)
    return True


def plugin_status(home: Path) -> tuple[str, bool]:
    """(enabled_state, installed), read-only. enabled_state is one of
    "enabled" | "not-enabled" | "no-config" | "unreadable". installed = the
    plugin dir/symlink exists under <home>/plugins/ (symlinks followed)."""
    installed = (home / "plugins" / PLUGIN_NAME).exists()
    config_path = home / "config.yaml"
    if not config_path.is_file():
        return "no-config", installed
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        return "unreadable", installed
    enabled = ((data.get("plugins") or {}).get("enabled")) or []
    state = "enabled" if PLUGIN_NAME in enabled else "not-enabled"
    return state, installed


@dataclass
class ProfileStatus:
    name: str
    home: Path
    is_target: bool
    env_state: str  # "ok" | "missing" | "mismatch"
    env_current: str | None
    plugin_enabled: str  # "enabled" | "not-enabled" | "no-config" | "unreadable"
    plugin_installed: bool


@dataclass
class ProfileResult:
    name: str
    action: str  # "env-written" | "skipped" | "error"
    detail: str


def _env_state(current: str | None, target: Path) -> str:
    if current is None:
        return "missing"
    return "ok" if Path(current).expanduser() == target else "mismatch"


def detect(base_home: Path, target: Path, only: list[str] | None = None) -> list[ProfileStatus]:
    """Read-only per-profile status of the env leg and the plugin leg."""
    target = target.expanduser()
    statuses: list[ProfileStatus] = []
    for name, home in iter_profiles(base_home):
        if only and name not in only:
            continue
        current = read_env_var(home / ".env", ENV_KEY)
        enabled, installed = plugin_status(home)
        statuses.append(
            ProfileStatus(
                name=name,
                home=home,
                is_target=(home.resolve() == target.resolve()),
                env_state=_env_state(current, target),
                env_current=current,
                plugin_enabled=enabled,
                plugin_installed=installed,
            )
        )
    return statuses


def apply(statuses: list[ProfileStatus], target: Path) -> list[ProfileResult]:
    """Perform the .env upserts for profiles that need them. Per-profile error
    isolation: one failure is recorded and the run continues. Never touches
    config.yaml or plugins/."""
    results: list[ProfileResult] = []
    for st in statuses:
        if st.is_target:
            results.append(ProfileResult(st.name, "skipped", "is the shared telemetry home"))
            continue
        if st.env_state == "ok":
            results.append(ProfileResult(st.name, "skipped", "already points at the shared home"))
            continue
        try:
            changed = upsert_env_var(st.home / ".env", ENV_KEY, str(target))
        except OSError as exc:
            results.append(ProfileResult(st.name, "error", str(exc)))
            continue
        if changed:
            results.append(ProfileResult(st.name, "env-written", f"set {ENV_KEY}={target}"))
        else:
            results.append(ProfileResult(st.name, "skipped", "already set"))
    return results


def _env_note(st: ProfileStatus, result: ProfileResult | None) -> str:
    if result is not None:
        return f"{result.action}: {result.detail}"
    if st.is_target:
        return "is the shared home"
    if st.env_state == "ok":
        return "ok"
    if st.env_state == "missing":
        return "would set HERMES_TELEMETRY_HOME"
    return f"would change from {st.env_current}"


def render(
    statuses: list[ProfileStatus],
    target: Path,
    results: list[ProfileResult] | None = None,
) -> str:
    by_name = {r.name: r for r in results} if results else {}
    lines = [f"Shared telemetry home: {target}", ""]
    for st in statuses:
        note = _env_note(st, by_name.get(st.name))
        plugin = (
            "plugin ok" if st.plugin_enabled == "enabled" else f"plugin WARN: {st.plugin_enabled}"
        )
        lines.append(f"  {st.name:<20} env: {note:<40} {plugin}")
    warn = [st.name for st in statuses if st.plugin_enabled != "enabled"]
    if warn:
        lines.append("")
        lines.append("WARN: plugin not enabled in: " + ", ".join(warn))
        for name in warn:
            lines.append(f"  hermes plugins enable {PLUGIN_NAME} --profile {name}")
    return "\n".join(lines)


def to_json(
    statuses: list[ProfileStatus],
    target: Path,
    results: list[ProfileResult] | None = None,
) -> str:
    by_name = {r.name: {"action": r.action, "detail": r.detail} for r in results} if results else {}
    payload = {
        "target": str(target),
        "profiles": [
            {
                "name": st.name,
                "home": str(st.home),
                "is_target": st.is_target,
                "env_state": st.env_state,
                "env_current": st.env_current,
                "plugin_enabled": st.plugin_enabled,
                "plugin_installed": st.plugin_installed,
                "result": by_name.get(st.name),
            }
            for st in statuses
        ],
    }
    return json.dumps(payload, indent=2)
