# tests/test_telemetry_cli.py
from unittest.mock import patch

import pytest
from hermes_telemetry.telemetry_cli import main


def test_stats_today_default_text(capsys):
    with patch("hermes_telemetry.stats._summary_block", return_value="STATS_TODAY") as m:
        main(["stats"])
    out, _ = capsys.readouterr()
    assert out == "STATS_TODAY\n"
    m.assert_called_once_with(24)


def test_stats_today_explicit_text(capsys):
    with patch("hermes_telemetry.stats._summary_block", return_value="STATS_TODAY") as m:
        main(["stats", "today"])
    out, _ = capsys.readouterr()
    assert out == "STATS_TODAY\n"
    m.assert_called_once_with(24)


@pytest.mark.parametrize(
    "argv,expected_hours",
    [
        (["stats", "week"], 168),
        (["stats", "month"], 720),
    ],
)
def test_stats_summary_window_text(argv, expected_hours, capsys):
    with patch("hermes_telemetry.stats._summary_block", return_value="S") as m:
        main(argv)
    m.assert_called_once_with(expected_hours)


@pytest.mark.parametrize(
    "argv,expected_hours",
    [
        (["stats", "cron"], 168),
        (["stats", "cron-week"], 168),
        (["stats", "cron-month"], 720),
    ],
)
def test_stats_cron_text(argv, expected_hours, capsys):
    with patch("hermes_telemetry.stats._cron_block", return_value="C") as m:
        main(argv)
    m.assert_called_once_with(expected_hours)


@pytest.mark.parametrize(
    "argv,expected_hours",
    [
        (["stats", "providers"], 24),
        (["stats", "providers-week"], 168),
        (["stats", "providers-month"], 720),
    ],
)
def test_stats_providers_text(argv, expected_hours, capsys):
    with patch("hermes_telemetry.stats._providers_block", return_value="P") as m:
        main(argv)
    m.assert_called_once_with(expected_hours)


@pytest.mark.parametrize(
    "argv,expected_hours",
    [
        (["stats", "models"], 24),
        (["stats", "models-week"], 168),
        (["stats", "models-month"], 720),
    ],
)
def test_stats_models_text(argv, expected_hours, capsys):
    with patch("hermes_telemetry.stats._models_block", return_value="M") as m:
        main(argv)
    m.assert_called_once_with(expected_hours)
