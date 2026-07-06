"""Chip-aware local power estimation.

Estimates the wattage of the local machine for cost modeling of
locally-run models (Ollama, llama.cpp). On Apple Silicon, detects the
chip family via sysctl hw.machine and returns a known wattage estimate.
On non-detected hardware, returns a prompt for the user to configure
their own wattage value.

Usage as a library:
    from . import local_power
    info = local_power.detect()
    # info.wattage: estimated wattage (int) or None
    # info.chip: chip identifier string or None
    # info.is_detected: bool

Usage as a CLI:
    python -m hermes_telemetry.local_power
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Known Apple Silicon chip wattage estimates (TDP in watts).
# Sources: Apple documentation, AnandTech, notebookcheck.net.
# These are approximate TDP values for the full chip.
# ---------------------------------------------------------------------------
_APPLE_SILICON_WATTAGE: dict[str, int] = {
    # M1 family
    "M1": 15,
    "M1 Pro": 30,
    "M1 Max": 50,
    "M1 Ultra": 90,
    # M2 family
    "M2": 15,
    "M2 Pro": 30,
    "M2 Max": 50,
    "M2 Ultra": 90,
    # M3 family
    "M3": 15,
    "M3 Pro": 28,
    "M3 Max": 50,
    "M3 Ultra": 90,
    # M4 family
    "M4": 15,
    "M4 Pro": 30,
    "M4 Max": 50,
    "M4 Ultra": 90,
    # M5 family (assumed similar to M4 until official specs)
    "M5": 15,
    "M5 Pro": 30,
    "M5 Max": 50,
    "M5 Ultra": 90,
}

# ---------------------------------------------------------------------------
# Known Apple Silicon machine identifiers from sysctl hw.machine
# Maps machine name → chip identifier
# ---------------------------------------------------------------------------
_APPLE_MACHINE_MAP: dict[str, str] = {
    # Apple Silicon — arm64 only
    # M1
    "MacBookAir10,1": "M1",  # M1 MacBook Air
    "MacBookPro17,1": "M1",  # M1 MacBook Pro 13"
    "MacBookPro18,3": "M1 Pro",  # M1 Pro MacBook Pro 14"
    "MacBookPro18,4": "M1 Pro",  # M1 Pro MacBook Pro 16"
    "MacBookPro18,1": "M1 Max",  # M1 Max MacBook Pro 14"
    "MacBookPro18,2": "M1 Max",  # M1 Max MacBook Pro 16"
    "Macmini9,1": "M1",  # M1 Mac mini
    "iMac21,1": "M1",  # M1 iMac
    "iMac21,2": "M1",  # M1 iMac
    "Mac13,1": "M1 Ultra",  # Mac Studio M1 Ultra
    "Mac13,2": "M1 Max",  # Mac Studio M1 Max
    # M2
    "Mac14,2": "M2",  # M2 MacBook Air
    "Mac14,7": "M2",  # M2 MacBook Pro 13"
    "Mac14,5": "M2 Pro",  # M2 Pro MacBook Pro 14"
    "Mac14,6": "M2 Pro",  # M2 Pro MacBook Pro 16"
    "Mac14,9": "M2 Max",  # M2 Max MacBook Pro 14"
    "Mac14,10": "M2 Max",  # M2 Max MacBook Pro 16"
    "Mac14,3": "M2",  # M2 Mac mini
    "Mac14,8": "M2 Pro",  # M2 Pro Mac mini
    "Mac14,12": "M2",  # M2 Mac mini
    "Mac14,13": "M2 Max",  # M2 Max Mac Studio
    "Mac14,14": "M2 Ultra",  # M2 Ultra Mac Studio
    "Mac15,12": "M2 Ultra",  # Mac Pro (2023) M2 Ultra
    # M3
    "Mac15,3": "M3",  # M3 MacBook Pro 14"
    "Mac15,4": "M3",  # M3 iMac
    "Mac15,5": "M3",  # M3 iMac
    "Mac15,6": "M3 Pro",  # M3 Pro MacBook Pro 14"
    "Mac15,7": "M3 Pro",  # M3 Pro MacBook Pro 16"
    "Mac15,8": "M3 Max",  # M3 Max MacBook Pro 14"
    "Mac15,9": "M3 Max",  # M3 Max MacBook Pro 16"
    "Mac15,10": "M3 Max",  # M3 Max MacBook Pro 16"
    "Mac15,11": "M3",  # M3 iMac
    "Mac15,13": "M3",  # M3 MacBook Air
    # M4
    "Mac16,1": "M4",  # M4 MacBook Pro 14"
    "Mac16,2": "M4 Pro",  # M4 Pro MacBook Pro 14"
    "Mac16,3": "M4 Pro",  # M4 Pro MacBook Pro 16"
    "Mac16,4": "M4 Max",  # M4 Max MacBook Pro 14"
    "Mac16,5": "M4 Max",  # M4 Max MacBook Pro 16"
    "Mac16,6": "M4",  # M4 iMac
    "Mac16,7": "M4",  # M4 MacBook Air
    "Mac16,8": "M4 Pro",  # M4 Pro Mac mini
    "Mac16,9": "M4",  # M4 Mac mini
    "Mac16,10": "M4 Ultra",  # M4 Ultra Mac Studio
    "Mac16,11": "M4 Max",  # M4 Max Mac Studio
    "Mac16,12": "M4 Ultra",  # Mac Pro (2024) M4 Ultra
}

# ---------------------------------------------------------------------------
# Fallback wattage when detection fails (used as the prompt default).
# ---------------------------------------------------------------------------
_GENERIC_WATTAGE = 50


@dataclass
class PowerInfo:
    """Information about the local machine's power characteristics."""

    wattage: int | None
    """Estimated chip TDP in watts, or None if unknown."""

    chip: str | None
    """Detected chip identifier (e.g. 'M3 Pro'), or None."""

    machine: str | None
    """Raw machine identifier from sysctl (e.g. 'Mac15,6'), or None."""

    is_detected: bool
    """True if the chip was positively identified via sysctl."""


def detect() -> PowerInfo:
    """Detect chip and estimate wattage.

    Returns a PowerInfo with the best available estimate.
    If the chip cannot be identified, wattage is None and is_detected is False.
    """
    if sys.platform != "darwin":
        return PowerInfo(
            wattage=None,
            chip=None,
            machine=None,
            is_detected=False,
        )

    machine = _get_machine_id()
    if not machine:
        return PowerInfo(
            wattage=None,
            chip=None,
            machine=None,
            is_detected=False,
        )

    chip = _resolve_chip(machine)
    if chip:
        wattage = _APPLE_SILICON_WATTAGE.get(chip)
        return PowerInfo(
            wattage=wattage,
            chip=chip,
            machine=machine,
            is_detected=True,
        )

    return PowerInfo(
        wattage=None,
        chip=None,
        machine=machine,
        is_detected=False,
    )


def get_kwh_per_hour() -> float | None:
    """Return estimated kWh per hour of runtime, or None if unknown.

    Converts wattage estimate to kWh: wattage / 1000.
    """
    info = detect()
    if info.wattage is not None:
        return info.wattage / 1000.0
    return None


def _get_machine_id() -> str | None:
    """Run `sysctl -n hw.machine` and return the machine identifier."""
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.machine"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def _resolve_chip(machine: str) -> str | None:
    """Map a machine identifier to a chip name."""
    return _APPLE_MACHINE_MAP.get(machine)


def _generate_machine_map_help() -> str:
    """Generate a human-readable table of known machine identifiers."""
    lines = []
    for machine, chip in sorted(_APPLE_MACHINE_MAP.items()):
        lines.append(f"  {machine:20s} → {chip}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    info = detect()
    if info.is_detected:
        print(f"Chip:    {info.chip}")
        print(f"Machine: {info.machine}")
        print(f"Wattage: {info.wattage} W (TDP estimate)")
        kwh = get_kwh_per_hour()
        if kwh is not None:
            print(f"kWh/hr:  {kwh:.4f}")
    elif info.machine:
        print(f"Machine: {info.machine}")
        print(
            "Status:  Unknown chip — set the MINT_LOCAL_WATTAGE env var to your chip's TDP in watts"
        )
    else:
        print("Status:  Not running on macOS, or sysctl unavailable")
        print("Set the MINT_LOCAL_WATTAGE env var to your chip's TDP in watts")
