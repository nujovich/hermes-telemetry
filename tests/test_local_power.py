"""Tests for local_power module."""

import sys
from unittest.mock import patch, MagicMock

import pytest

from hermes_telemetry.local_power import (
    detect,
    get_kwh_per_hour,
    PowerInfo,
    _APPLE_MACHINE_MAP,
    _APPLE_SILICON_WATTAGE,
    _resolve_chip,
    _GENERIC_WATTAGE,
)


class TestDetect:
    def test_linux_returns_undetected(self):
        """On non-darwin, detection returns is_detected=False."""
        with patch.object(sys, "platform", "linux"):
            info = detect()
        assert not info.is_detected
        assert info.wattage is None
        assert info.chip is None
        assert info.machine is None

    def test_windows_returns_undetected(self):
        with patch.object(sys, "platform", "win32"):
            info = detect()
        assert not info.is_detected

    def test_darwin_no_sysctl_returns_undetected(self):
        """sysctl failure should return undetected."""
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value=None,
            ):
                info = detect()
        assert not info.is_detected
        assert info.wattage is None
        assert info.machine is None

    def test_darwin_unknown_machine_returns_undetected(self):
        """Unknown machine ID should return undetected with the machine set."""
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="UnknownMachine1,1",
            ):
                info = detect()
        assert not info.is_detected
        assert info.wattage is None
        assert info.chip is None
        assert info.machine == "UnknownMachine1,1"

    def test_detects_m1_macbook_air(self):
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="MacBookAir10,1",
            ):
                info = detect()
        assert info.is_detected
        assert info.chip == "M1"
        assert info.machine == "MacBookAir10,1"
        assert info.wattage == 15

    def test_detects_m3_pro(self):
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="Mac15,6",
            ):
                info = detect()
        assert info.is_detected
        assert info.chip == "M3 Pro"
        assert info.machine == "Mac15,6"
        assert info.wattage == 28

    def test_detects_m4_max(self):
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="Mac16,4",
            ):
                info = detect()
        assert info.is_detected
        assert info.chip == "M4 Max"
        assert info.machine == "Mac16,4"
        assert info.wattage == 50

    def test_detects_m2_ultra(self):
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="Mac14,14",
            ):
                info = detect()
        assert info.is_detected
        assert info.chip == "M2 Ultra"
        assert info.wattage == 90


class TestGetKwhPerHour:
    def test_returns_float_when_detected(self):
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="MacBookAir10,1",
            ):
                kwh = get_kwh_per_hour()
        assert kwh == pytest.approx(0.015)  # 15W / 1000

    def test_returns_none_when_undetected(self):
        with patch.object(sys, "platform", "linux"):
            kwh = get_kwh_per_hour()
        assert kwh is None

    def test_m3_pro_kwh(self):
        with patch.object(sys, "platform", "darwin"):
            with patch(
                "hermes_telemetry.local_power._get_machine_id",
                return_value="Mac15,6",
            ):
                kwh = get_kwh_per_hour()
        assert kwh == pytest.approx(0.028)  # 28W / 1000


class TestPowerInfo:
    def test_power_info_dataclass_fields(self):
        info = PowerInfo(
            wattage=15,
            chip="M1",
            machine="MacBookAir10,1",
            is_detected=True,
        )
        assert info.wattage == 15
        assert info.chip == "M1"
        assert info.machine == "MacBookAir10,1"
        assert info.is_detected is True

    def test_power_info_undetected_defaults(self):
        info = PowerInfo(
            wattage=None,
            chip=None,
            machine=None,
            is_detected=False,
        )
        assert info.wattage is None
        assert info.is_detected is False


class TestResolveChip:
    def test_known_machine(self):
        assert _resolve_chip("MacBookAir10,1") == "M1"

    def test_unknown_machine(self):
        assert _resolve_chip("UnknownMachine1,1") is None

    def test_all_mapped_machines_have_wattage(self):
        """Every machine in the map should have a wattage entry."""
        for machine, chip in _APPLE_MACHINE_MAP.items():
            assert chip in _APPLE_SILICON_WATTAGE, (
                f"Machine '{machine}' maps to chip '{chip}' "
                f"which has no wattage entry"
            )


class TestWattageTable:
    def test_all_chips_have_positive_wattage(self):
        for chip, wattage in _APPLE_SILICON_WATTAGE.items():
            assert wattage > 0, f"Chip '{chip}' has non-positive wattage: {wattage}"

    def test_known_chips_have_expected_wattage(self):
        assert _APPLE_SILICON_WATTAGE["M1"] == 15
        assert _APPLE_SILICON_WATTAGE["M1 Pro"] == 30
        assert _APPLE_SILICON_WATTAGE["M1 Max"] == 50
        assert _APPLE_SILICON_WATTAGE["M1 Ultra"] == 90
        assert _APPLE_SILICON_WATTAGE["M3 Pro"] == 28
        assert _APPLE_SILICON_WATTAGE["M4"] == 15


class TestSysctlIntegration:
    @patch("subprocess.run")
    def test_sysctl_success(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Mac15,6\n"
        mock_run.return_value = mock_result

        from hermes_telemetry.local_power import _get_machine_id

        result = _get_machine_id()
        assert result == "Mac15,6"
        mock_run.assert_called_once_with(
            ["sysctl", "-n", "hw.machine"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("subprocess.run")
    def test_sysctl_failure(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        from hermes_telemetry.local_power import _get_machine_id

        result = _get_machine_id()
        assert result is None

    @patch("subprocess.run")
    def test_sysctl_exception(self, mock_run):
        mock_run.side_effect = FileNotFoundError("sysctl not found")

        from hermes_telemetry.local_power import _get_machine_id

        result = _get_machine_id()
        assert result is None