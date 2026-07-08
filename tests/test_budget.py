"""Tests for budget.py — the budget engine, anti-spam, degradation, and cron
pause. None of these require a live Hermes; spend is seeded directly into the
SQLite layer and cron.jobs.pause_job is mocked.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import hermes_telemetry.budget as budget
import hermes_telemetry.db as db
import pytest


@pytest.fixture(autouse=True)
def isolated():
    """Fresh DB + fresh budget config + cleared caches for every test.

    HERMES_HOME isolation is provided by the project-level autouse fixture in
    conftest.py; this only resets the DB connection and budget caches.
    """
    db._local.conn = None
    budget.reload_config()  # clears config + verdict caches
    yield
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None
    budget.reload_config()


def _write_budget(tmp_path: Path, body: str) -> None:
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    (tele / "budget.yaml").write_text(textwrap.dedent(body))
    budget.reload_config()


def _seed(
    session_id,
    cost,
    *,
    platform="cli",
    cron_job_id=None,
    sender_id=None,
    estimated=False,
    model="claude-sonnet-4-6",
):
    db.start_run(session_id, model=model, platform=platform, cron_job_id=cron_job_id)
    if sender_id:
        db.set_sender(session_id, sender_id)
    db.record_llm_call(
        session_id,
        ts=db._utcnow(),
        model=model,
        provider="test",
        tokens_in=0,
        tokens_out=0,
        cost_usd=cost,
        latency_ms=0,
        estimated=estimated,
    )


# ---------------------------------------------------------------------------
# Engine: ok / soft / hard
# ---------------------------------------------------------------------------


def test_no_config_means_disabled(tmp_path):
    _seed("s1", 999.0)
    assert budget.check("global", "") is None  # no limit → not enforced


def test_under_budget_is_ok(tmp_path):
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("s1", 1.00)
    v = budget.check("global", "")
    assert v is not None
    assert v.status == "ok"
    assert v.window == "daily"


def test_soft_band(tmp_path):
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("s1", 4.25)  # 85% → soft (≥0.80, <1.00)
    v = budget.check("global", "")
    assert v.status == "soft"
    assert 0.80 <= v.pct < 1.00


def test_hard_band(tmp_path):
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("s1", 5.50)  # 110% → hard
    v = budget.check("global", "")
    assert v.status == "hard"
    assert v.pct >= 1.00


def test_most_severe_window_wins(tmp_path):
    # daily over hard, monthly fine — verdict should reflect the hard daily one
    _write_budget(
        tmp_path,
        """
        budgets:
          global:
            daily_usd: 1.00
            monthly_usd: 1000.00
    """,
    )
    _seed("s1", 1.50)
    v = budget.check("global", "")
    assert v.status == "hard"
    assert v.window == "daily"


# ---------------------------------------------------------------------------
# Estimated-spend degradation
# ---------------------------------------------------------------------------


def test_hard_degrades_to_soft_when_estimated(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          global:
            daily_usd: 5.00
        on_estimated:
          mode: warn_only
    """,
    )
    _seed("s1", 6.00, estimated=True)  # over hard, but estimated
    v = budget.check("global", "")
    assert v.based_on_estimates is True
    assert v.status == "soft"  # degraded
    assert v.degraded is True


def test_estimated_enforced_when_mode_enforce(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          global:
            daily_usd: 5.00
        on_estimated:
          mode: enforce
    """,
    )
    _seed("s1", 6.00, estimated=True)
    v = budget.check("global", "")
    assert v.status == "hard"  # not degraded
    assert v.degraded is False


# ---------------------------------------------------------------------------
# Per-scope routing
# ---------------------------------------------------------------------------


def test_per_cron_job_default_and_override(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          per_cron_job:
            default:
              daily_usd: 1.00
            overrides:
              big_job:
                daily_usd: 10.00
    """,
    )
    _seed("s1", 2.00, platform="cron", cron_job_id="small_job")
    _seed("s2", 2.00, platform="cron", cron_job_id="big_job")
    assert budget.check("cron_job", "small_job").status == "hard"  # 2.0/1.0
    assert budget.check("cron_job", "big_job").status == "ok"  # 2.0/10.0


def test_per_sender_isolation(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          per_sender:
            default:
              daily_usd: 2.00
    """,
    )
    _seed("s1", 2.50, sender_id="alice")
    _seed("s2", 0.10, sender_id="bob")
    assert budget.check("sender", "alice").status == "hard"
    assert budget.check("sender", "bob").status == "ok"


def test_evaluate_run_covers_applicable_scopes(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          global:
            daily_usd: 100.00
          per_cron_job:
            default:
              daily_usd: 1.00
    """,
    )
    _seed("s1", 2.00, platform="cron", cron_job_id="job1")
    run = db.get_run("s1")
    verdicts = budget.evaluate_run(run)
    scopes = {v.scope for v in verdicts}
    assert "global" in scopes
    assert "cron_job" in scopes
    cron_v = next(v for v in verdicts if v.scope == "cron_job")
    assert cron_v.status == "hard"


# ---------------------------------------------------------------------------
# Anti-spam: one alert per (scope, window, period, level)
# ---------------------------------------------------------------------------


def test_soft_alert_fires_once_per_window(tmp_path):
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("s1", 4.25)  # soft
    run = db.get_run("s1")

    first = budget.alert_context(budget.evaluate_run(run))
    assert first is not None and "BUDGET" in first

    budget.reload_config()  # clear verdict TTL cache, not the alert ledger
    second = budget.alert_context(budget.evaluate_run(run))
    assert second is None  # already alerted this window/level


def test_soft_then_hard_each_alert_once(tmp_path):
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("s1", 4.25)  # soft
    run = db.get_run("s1")
    assert budget.alert_context(budget.evaluate_run(run)) is not None

    _seed("s2", 1.00)  # now 5.25 total → hard
    budget.reload_config()
    msg = budget.alert_context(budget.evaluate_run(db.get_run("s1")))
    assert msg is not None and "hit its" in msg  # hard alert is a new level


# ---------------------------------------------------------------------------
# Hard block message (pre_tool_call gate)
# ---------------------------------------------------------------------------


def test_block_message_only_on_hard(tmp_path):
    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 5.00\n")
    _seed("s1", 4.25)  # soft
    assert budget.block_message_for(budget.evaluate_run(db.get_run("s1"))) is None

    _seed("s2", 2.0)  # over hard
    budget.reload_config()
    msg = budget.block_message_for(budget.evaluate_run(db.get_run("s1")))
    assert msg is not None and "blocked" in msg


def test_degraded_hard_does_not_block(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          global:
            daily_usd: 5.00
        on_estimated:
          mode: warn_only
    """,
    )
    _seed("s1", 6.00, estimated=True)
    assert budget.block_message_for(budget.evaluate_run(db.get_run("s1"))) is None


# ---------------------------------------------------------------------------
# Cron pause
# ---------------------------------------------------------------------------


def test_cron_pause_called_once(tmp_path, monkeypatch):
    _write_budget(
        tmp_path,
        """
        budgets:
          per_cron_job:
            default:
              daily_usd: 1.00
    """,
    )
    calls = []
    monkeypatch.setattr(
        budget, "_pause_cron_job", lambda job_id, reason: calls.append((job_id, reason)) or True
    )
    _seed("s1", 2.00, platform="cron", cron_job_id="job1")  # hard

    verdicts = budget.evaluate_run(db.get_run("s1"))
    budget.enforce_cron_pause(verdicts)
    budget.enforce_cron_pause(verdicts)  # second call must be a no-op (anti-spam)

    assert len(calls) == 1
    assert calls[0][0] == "job1"
    assert "budget" in calls[0][1].lower()


def test_cron_pause_not_called_when_ok(tmp_path, monkeypatch):
    _write_budget(
        tmp_path,
        """
        budgets:
          per_cron_job:
            default:
              daily_usd: 10.00
    """,
    )
    calls = []
    monkeypatch.setattr(
        budget, "_pause_cron_job", lambda job_id, reason: calls.append(job_id) or True
    )
    _seed("s1", 1.00, platform="cron", cron_job_id="job1")  # ok
    budget.enforce_cron_pause(budget.evaluate_run(db.get_run("s1")))
    assert calls == []


# ---------------------------------------------------------------------------
# /budget command
# ---------------------------------------------------------------------------


def test_budget_set_persists_and_reloads(tmp_path):
    out = budget.handle("set global daily 7.50")
    assert "7.50" in out
    # The new limit must be live immediately (hot reload)
    _seed("s1", 8.00)
    v = budget.check("global", "")
    assert v is not None and v.status == "hard"


def test_budget_set_validation(tmp_path):
    assert "Usage" in budget.handle("set global daily")  # too few args
    assert "Invalid amount" in budget.handle("set global daily abc")
    assert "scope" in budget.handle("set bogus daily 5").lower()
    assert "window" in budget.handle("set global yearly 5").lower()


def test_budget_status_no_config(tmp_path):
    out = budget.handle("")
    assert "No budgets configured" in out


def test_budget_cron_subcommand_notes_subagent_attribution(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          per_cron_job:
            default:
              daily_usd: 1.00
    """,
    )
    _seed("s1", 0.50, platform="cron", cron_job_id="job1")
    out = budget.handle("cron")
    assert "job1" in out
    assert "subagent" in out.lower()
    assert "includes" in out.lower()  # per-cron-job now attributes linked subagent spend


# ---------------------------------------------------------------------------
# Burn-rate forecasting (Milestone 3)
# ---------------------------------------------------------------------------


def _seed_on_day(session_id, cost, day_iso, *, cron_job_id=None, sender_id=None):
    """Seed a run that started on a specific UTC day (YYYY-MM-DD)."""
    from hermes_telemetry import db as _db

    started_at = f"{day_iso}T12:00:00+00:00"
    _db.start_run(
        session_id,
        model="claude-sonnet-4-6",
        platform="cli",
        cron_job_id=cron_job_id,
    )
    if sender_id:
        _db.set_sender(session_id, sender_id)
    _db.record_llm_call(
        session_id,
        ts=started_at,
        model="claude-sonnet-4-6",
        provider="test",
        tokens_in=0,
        tokens_out=0,
        cost_usd=cost,
        latency_ms=0,
    )
    # Force the run's started_at to the target day so daily bucketing is exact.
    conn = _db._get_conn()
    conn.execute("UPDATE runs SET started_at = ? WHERE session_id = ?", (started_at, session_id))


def test_daily_spend_series_fills_zero_days(tmp_path):
    from datetime import datetime, timedelta, timezone

    from hermes_telemetry import db as _db

    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _seed_on_day("s1", 1.00, "2026-06-01")
    _seed_on_day("s2", 3.00, "2026-06-03")
    series = _db.daily_spend_series("global", "", 3, now=base + timedelta(days=2, hours=15))
    assert [d["day"] for d in series] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert series[0]["cost_usd"] == pytest.approx(1.00)
    assert series[1]["cost_usd"] == pytest.approx(0.0)  # gap filled with zero
    assert series[2]["cost_usd"] == pytest.approx(3.00)


def test_burn_rate_disabled_without_limit(tmp_path):
    from datetime import datetime, timezone

    _seed("s1", 5.00)
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    proj = budget.burn_rate_projection("global", "", window="daily", now=now)
    assert proj["enabled"] is False


def test_burn_rate_projection_daily_on_track(tmp_path):
    from datetime import datetime, timedelta, timezone

    _write_budget(tmp_path, "budgets:\n  global:\n    daily_usd: 10.00\n")
    # Spend $2/day on each of the last 3 days -> avg $2/day, limit $10/day
    base = datetime(2026, 6, 10, tzinfo=timezone.utc)
    for i in range(3):
        day = (base - timedelta(days=2 - i)).strftime("%Y-%m-%d")
        _seed_on_day(f"s{i}", 2.00, day)
    now = base.replace(hour=12, minute=0, second=0)  # mid-day on day 3
    proj = budget.burn_rate_projection("global", "", window="daily", lookback_days=3, now=now)
    assert proj["enabled"] is True
    assert proj["limit_usd"] == pytest.approx(10.00)
    assert proj["avg_daily_usd"] == pytest.approx(2.00)
    assert proj["status"] in ("ok", "soft", "hard")
    # Projected total should be under the limit (spent so far + half a day at $2/day)
    assert proj["projected_total_usd"] < 10.00
    assert proj["status"] == "ok"


def test_burn_rate_projection_monthly_hard(tmp_path):
    from datetime import datetime, timezone

    _write_budget(tmp_path, "budgets:\n  global:\n    monthly_usd: 10.00\n")
    # Spend $9/day for a 30-day month on the 1st-5th -> avg $9/day, limit $10/month
    for d in range(1, 6):
        _seed_on_day(f"m{d}", 9.00, f"2026-02-{d:02d}")
    now = datetime(2026, 2, 5, 12, 0, tzinfo=timezone.utc)
    proj = budget.burn_rate_projection("global", "", window="monthly", lookback_days=14, now=now)
    assert proj["enabled"] is True
    assert proj["limit_usd"] == pytest.approx(10.00)
    # Already $45 spent against a $10 monthly limit -> projected hard
    assert proj["spent_so_far_usd"] == pytest.approx(45.00)
    assert proj["status"] == "hard"
    assert proj["projected_pct"] > 1.0


def test_budget_forecast_command_text(tmp_path):
    from datetime import datetime, timezone

    _write_budget(tmp_path, "budgets:\n  global:\n    monthly_usd: 100.00\n")
    today = datetime.now(timezone.utc).strftime("%Y-%m-10")
    _seed_on_day("s1", 5.00, today)
    out = budget.handle("forecast monthly")
    assert "burn-rate forecast" in out
    assert "Projected" in out


def test_budget_forecast_command_no_limit(tmp_path):
    out = budget.handle("forecast daily")
    assert "No daily budget" in out


def test_budget_forecast_command_unknown_scope(tmp_path):
    out = budget.handle("forecast daily bogus")
    assert "Unknown scope" in out
