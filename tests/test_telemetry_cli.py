# tests/test_telemetry_cli.py
import json as _json
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


def test_budget_status_text(capsys):
    with patch("hermes_telemetry.budget._status_block", return_value="BUDGET_STATUS"):
        main(["budget"])
    out, _ = capsys.readouterr()
    assert out == "BUDGET_STATUS\n"


def test_budget_cron_text(capsys):
    with patch("hermes_telemetry.budget._cron_block", return_value="BUDGET_CRON"):
        main(["budget", "cron"])
    out, _ = capsys.readouterr()
    assert out == "BUDGET_CRON\n"


def test_budget_set_text(capsys):
    with patch("hermes_telemetry.budget._set_budget", return_value="Budget updated.") as m:
        main(["budget", "set", "global", "daily", "10.00"])
    out, _ = capsys.readouterr()
    assert out == "Budget updated.\n"
    m.assert_called_once_with("global", "daily", 10.0)


def test_budget_set_invalid_amount_exits():
    with pytest.raises(SystemExit):
        main(["budget", "set", "global", "daily", "notanumber"])


def test_stats_today_json(capsys):
    fake = {"total_runs": 3, "cost_usd": 0.42, "tokens_in": 1000}
    with patch("hermes_telemetry.telemetry_cli.db.stats_summary", return_value=fake) as m:
        main(["stats", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["total_runs"] == 3
    m.assert_called_once_with(24)


def test_stats_week_json(capsys):
    fake = {"total_runs": 20, "cost_usd": 2.10}
    with patch("hermes_telemetry.telemetry_cli.db.stats_summary", return_value=fake) as m:
        main(["stats", "week", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["total_runs"] == 20
    m.assert_called_once_with(168)


def test_stats_cron_json(capsys):
    fake = [{"job_id": "backup", "cost_usd": 0.10, "runs": 5}]
    with patch("hermes_telemetry.telemetry_cli.db.cost_by_job", return_value=fake) as m:
        main(["stats", "cron", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data[0]["job_id"] == "backup"
    m.assert_called_once_with(168)


def test_stats_providers_json(capsys):
    fake = [{"provider": "anthropic", "cost_usd": 1.00}]
    with patch("hermes_telemetry.telemetry_cli.db.stats_by_provider", return_value=fake) as m:
        main(["stats", "providers", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data[0]["provider"] == "anthropic"
    m.assert_called_once_with(24)


def test_stats_models_json(capsys):
    fake = [{"model": "claude-sonnet-4-6", "cost_usd": 0.80}]
    with patch("hermes_telemetry.telemetry_cli.db.stats_by_model", return_value=fake) as m:
        main(["stats", "models", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data[0]["model"] == "claude-sonnet-4-6"
    m.assert_called_once_with(24)
