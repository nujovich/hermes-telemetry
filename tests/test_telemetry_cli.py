# tests/test_telemetry_cli.py
import json as _json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from hermes_telemetry.telemetry_cli import main


def test_stats_today_default_text(capsys):
    with patch("hermes_telemetry.stats._summary_block", return_value="STATS_TODAY") as m:
        main(["stats"])
    out, _ = capsys.readouterr()
    assert out == "STATS_TODAY\n"
    # Now called with date_from/date_to keyword args
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


def test_stats_today_explicit_text(capsys):
    with patch("hermes_telemetry.stats._summary_block", return_value="STATS_TODAY") as m:
        main(["stats", "today"])
    out, _ = capsys.readouterr()
    assert out == "STATS_TODAY\n"
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "week"], "202"),  # year in date_from
        (["stats", "month"], "202"),
    ],
)
def test_stats_summary_window_text(argv, expected_from_substr, capsys):
    with patch("hermes_telemetry.stats._summary_block", return_value="S") as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "cron"], "202"),
        (["stats", "cron-week"], "202"),
        (["stats", "cron-month"], "202"),
    ],
)
def test_stats_cron_text(argv, expected_from_substr, capsys):
    with patch("hermes_telemetry.stats._cron_block", return_value="C") as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "providers"], "202"),
        (["stats", "providers-week"], "202"),
        (["stats", "providers-month"], "202"),
    ],
)
def test_stats_providers_text(argv, expected_from_substr, capsys):
    with patch("hermes_telemetry.stats._providers_block", return_value="P") as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "models"], "202"),
        (["stats", "models-week"], "202"),
        (["stats", "models-month"], "202"),
    ],
)
def test_stats_models_text(argv, expected_from_substr, capsys):
    with patch("hermes_telemetry.stats._models_block", return_value="M") as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


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


def test_budget_forecast_text(capsys):
    with patch("hermes_telemetry.budget._forecast_block", return_value="FORECAST_OUT") as m:
        main(["budget", "forecast", "monthly", "global"])
    out, _ = capsys.readouterr()
    assert out == "FORECAST_OUT\n"
    m.assert_called_once_with("global", "", "monthly")


def test_budget_forecast_json(capsys):
    fake = {"enabled": True, "status": "ok", "projected_total_usd": 3.5}
    with patch("hermes_telemetry.budget.burn_rate_projection", return_value=fake) as m:
        main(["budget", "forecast", "daily", "global", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["status"] == "ok"
    m.assert_called_once()


def test_budget_set_text(capsys):
    with patch("hermes_telemetry.budget._set_budget", return_value="Budget updated.") as m:
        main(["budget", "set", "global", "daily", "10.00"])
    out, _ = capsys.readouterr()
    assert out == "Budget updated.\n"
    m.assert_called_once_with("global", "daily", 10.0, "")


def test_budget_set_profile_cli(capsys):
    with patch("hermes_telemetry.budget._set_budget", return_value="ok") as m:
        main(["budget", "set", "profile", "monthly", "50.00", "--id", "faro"])
    m.assert_called_once_with("profile", "monthly", 50.0, "faro")


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
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


def test_stats_week_json(capsys):
    fake = {"total_runs": 20, "cost_usd": 2.10}
    with patch("hermes_telemetry.telemetry_cli.db.stats_summary", return_value=fake) as m:
        main(["stats", "week", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["total_runs"] == 20
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


def test_stats_cron_json(capsys):
    fake = [{"job_id": "backup", "cost_usd": 0.10, "runs": 5}]
    with patch("hermes_telemetry.telemetry_cli.db.cost_by_job", return_value=fake) as m:
        main(["stats", "cron", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data[0]["job_id"] == "backup"
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


def test_stats_providers_json(capsys):
    fake = [{"provider": "anthropic", "cost_usd": 1.00}]
    with patch("hermes_telemetry.telemetry_cli.db.stats_by_provider", return_value=fake) as m:
        main(["stats", "providers", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data[0]["provider"] == "anthropic"
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


def test_stats_models_json(capsys):
    fake = [{"model": "claude-sonnet-4-6", "cost_usd": 0.80}]
    with patch("hermes_telemetry.telemetry_cli.db.stats_by_model", return_value=fake) as m:
        main(["stats", "models", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data[0]["model"] == "claude-sonnet-4-6"
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "cron-week", "--json"], "202"),
        (["stats", "cron-month", "--json"], "202"),
    ],
)
def test_stats_cron_json_windowed(argv, expected_from_substr, capsys):
    fake = [{"job_id": "j", "cost_usd": 0.01}]
    with patch("hermes_telemetry.telemetry_cli.db.cost_by_job", return_value=fake) as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "providers-week", "--json"], "202"),
        (["stats", "providers-month", "--json"], "202"),
    ],
)
def test_stats_providers_json_windowed(argv, expected_from_substr, capsys):
    fake = [{"provider": "x", "cost_usd": 0.01}]
    with patch("hermes_telemetry.telemetry_cli.db.stats_by_provider", return_value=fake) as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


@pytest.mark.parametrize(
    "argv,expected_from_substr",
    [
        (["stats", "models-week", "--json"], "202"),
        (["stats", "models-month", "--json"], "202"),
    ],
)
def test_stats_models_json_windowed(argv, expected_from_substr, capsys):
    fake = [{"model": "x", "cost_usd": 0.01}]
    with patch("hermes_telemetry.telemetry_cli.db.stats_by_model", return_value=fake) as m:
        main(argv)
    args, kwargs = m.call_args
    assert kwargs.get("date_from") is not None
    assert expected_from_substr in kwargs["date_from"]
    assert kwargs.get("date_to") is None


from hermes_telemetry.budget import BudgetVerdict  # noqa: E402


@pytest.fixture
def global_verdict():
    return BudgetVerdict(
        scope="global",
        scope_id="",
        window="daily",
        status="ok",
        spent=1.0,
        limit=5.0,
        pct=0.20,
        based_on_estimates=False,
        degraded=False,
        period_key="2026-06-11",
    )


def test_budget_status_json_global_ok(global_verdict, capsys):
    with patch("hermes_telemetry.budget.check", return_value=global_verdict), patch(
        "hermes_telemetry.telemetry_cli.db.list_cron_job_ids", return_value=[]
    ), patch("hermes_telemetry.telemetry_cli.db.list_sender_ids", return_value=[]), patch(
        "hermes_telemetry.telemetry_cli.db.list_profile_ids", return_value=[]
    ):
        main(["budget", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["global"]["status"] == "ok"
    assert data["global"]["spent"] == pytest.approx(1.0)
    assert data["global"]["limit"] == pytest.approx(5.0)
    assert data["cron_jobs"] == {}
    assert data["senders"] == {}
    assert data["profiles"] == {}


def test_budget_status_json_no_budgets(capsys):
    with patch("hermes_telemetry.budget.check", return_value=None), patch(
        "hermes_telemetry.telemetry_cli.db.list_cron_job_ids", return_value=[]
    ), patch("hermes_telemetry.telemetry_cli.db.list_sender_ids", return_value=[]), patch(
        "hermes_telemetry.telemetry_cli.db.list_profile_ids", return_value=[]
    ):
        main(["budget", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["global"] is None
    assert data["profiles"] == {}


def test_budget_status_json_profiles(capsys):
    profile_verdict = BudgetVerdict(
        scope="profile",
        scope_id="faro",
        window="daily",
        status="soft",
        spent=4.2,
        limit=5.0,
        pct=0.84,
        based_on_estimates=False,
        degraded=False,
        period_key="2026-06-11",
    )
    with patch("hermes_telemetry.budget.check", return_value=profile_verdict), patch(
        "hermes_telemetry.telemetry_cli.db.list_cron_job_ids", return_value=[]
    ), patch("hermes_telemetry.telemetry_cli.db.list_sender_ids", return_value=[]), patch(
        "hermes_telemetry.telemetry_cli.db.list_profile_ids", return_value=["faro"]
    ):
        main(["budget", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert data["profiles"]["faro"]["scope"] == "profile"
    assert data["profiles"]["faro"]["scope_id"] == "faro"
    assert data["profiles"]["faro"]["status"] == "soft"
    assert data["profiles"]["faro"]["spent"] == pytest.approx(4.2)
    assert data["profiles"]["faro"]["limit"] == pytest.approx(5.0)


def test_budget_cron_json(capsys):
    cron_verdict = BudgetVerdict(
        scope="cron_job",
        scope_id="backup",
        window="daily",
        status="soft",
        spent=4.2,
        limit=5.0,
        pct=0.84,
        based_on_estimates=False,
        degraded=False,
        period_key="2026-06-11",
    )
    with patch("hermes_telemetry.budget.check", return_value=cron_verdict), patch(
        "hermes_telemetry.telemetry_cli.db.list_cron_job_ids", return_value=["backup"]
    ):
        main(["budget", "cron", "--json"])
    out, _ = capsys.readouterr()
    data = _json.loads(out)
    assert len(data) == 1
    assert data[0]["scope_id"] == "backup"
    assert data[0]["status"] == "soft"


def test_main_no_subcommand_exits():
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 0


@pytest.mark.parametrize("tracked_path", ["hermes-telemetry", ".githooks/pre-commit"])
def test_entry_points_are_tracked_executable(tracked_path):
    """Files git must hand over runnable need mode 100755 in the index.

    Tracked as 100644, every fresh checkout lands a non-executable file:
    `hermes-telemetry` then dies with "Permission denied" (only the integrated
    `hermes telemetry` path keeps working), and `.githooks/pre-commit` is
    silently ignored by git, so format/lint/test breakage reaches CI unchecked.
    The working-tree permission is not enough to assert — a local chmod hides a
    wrong index mode, so read the index itself.
    """
    repo_root = Path(__file__).resolve().parents[1]
    if not (repo_root / ".git").exists():
        pytest.skip("not a git checkout")

    result = subprocess.run(
        ["git", "ls-files", "-s", "--", tracked_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip(), f"{tracked_path} is not tracked by git"
    mode = result.stdout.split()[0]
    assert mode == "100755", (
        f"{tracked_path} is tracked as {mode}; fix with "
        f"`git update-index --chmod=+x {tracked_path}`"
    )


# ─── _resolve_date_range ──────────────────────────────────────────────────────


def test_resolve_date_range_from_only_returns_none_date_to():
    """--from alone must not synthesize date_to."""
    from hermes_telemetry.telemetry_cli import _resolve_date_range

    args = _args(date_from="2026-06-16T12:00:00Z", date_to=None)
    date_from, date_to = _resolve_date_range(args)
    assert date_from == "2026-06-16T12:00:00Z"
    assert date_to is None


def test_resolve_date_range_from_and_to_returns_both():
    from hermes_telemetry.telemetry_cli import _resolve_date_range

    args = _args(date_from="2026-06-10", date_to="2026-06-15")
    date_from, date_to = _resolve_date_range(args)
    assert date_from == "2026-06-10"
    assert date_to == "2026-06-15"


# ─── _date_range_label ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "date_from, date_to, expected",
    [
        # Timestamped --from (no --to) → "since <timestamp>"
        ("2026-06-16T12:00:00Z", None, "since 2026-06-16T12:00:00"),
        # Date-only --from (no --to) → "since <date>"
        ("2026-06-16", None, "since 2026-06-16"),
        # Both date-only → "X to Y"
        ("2026-06-10", "2026-06-15", "2026-06-10 to 2026-06-15"),
        # Timestamped from + date to → keep time on from, strip on to
        ("2026-06-10T08:00:00Z", "2026-06-15", "2026-06-10T08:00:00 to 2026-06-15"),
        # T00:00:00 on both → stripped
        ("2026-06-10T00:00:00Z", "2026-06-15T00:00:00Z", "2026-06-10 to 2026-06-15"),
        # --to only
        (None, "2026-06-15", "until 2026-06-15"),
        # Neither
        (None, None, "all time"),
        # +00:00 suffix instead of Z
        ("2026-06-16T12:00:00+00:00", None, "since 2026-06-16T12:00:00"),
        # Microseconds
        ("2026-06-16T12:00:00.123456Z", None, "since 2026-06-16T12:00:00"),
    ],
)
def test_date_range_label(date_from, date_to, expected):
    from hermes_telemetry.stats import _date_range_label

    assert _date_range_label(date_from, date_to) == expected


# ─── helpers ──────────────────────────────────────────────────────────────────


def _args(**kwargs):
    from argparse import Namespace

    ns = Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns
