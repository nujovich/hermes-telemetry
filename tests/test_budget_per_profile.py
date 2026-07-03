"""per_profile budget scope: limit resolution, verdict labeling, and that
evaluate_run emits a profile verdict when a run carries a profile."""

import hermes_telemetry.budget as budget
import hermes_telemetry.db as db
import pytest


@pytest.fixture(autouse=True)
def isolated_db_and_caches():
    db._local.conn = None
    # Budget engine caches config + verdicts at module scope; clear both so
    # each test starts clean.
    with budget._config_lock:
        budget._config_cache = None
    with budget._verdict_lock:
        budget._verdict_cache.clear()
    yield
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None
    with budget._config_lock:
        budget._config_cache = None
    with budget._verdict_lock:
        budget._verdict_cache.clear()


def _set_config(cfg):
    with budget._config_lock:
        budget._config_cache = cfg


def test_resolve_limits_profile_override_and_default():
    _set_config(
        {
            "budgets": {
                "per_profile": {
                    "default": {"daily_usd": 2.0},
                    "overrides": {"coder": {"daily_usd": 10.0}},
                }
            },
            "thresholds": {"soft_pct": 0.8, "hard_pct": 1.0},
            "on_estimated": {"mode": "warn_only"},
        }
    )
    assert budget._resolve_limits("profile", "coder")["daily_usd"] == 10.0
    assert budget._resolve_limits("profile", "other")["daily_usd"] == 2.0


def test_scope_label_profile():
    v = budget.BudgetVerdict(
        scope="profile",
        scope_id="coder",
        window="daily",
        status="soft",
        spent=1.0,
        limit=2.0,
        pct=0.5,
        based_on_estimates=False,
        degraded=False,
        period_key="2026-07-03",
    )
    assert v.scope_label() == "Profile 'coder'"


def test_evaluate_run_includes_profile_scope():
    _set_config(
        {
            "budgets": {"per_profile": {"default": {"daily_usd": 1.0}}},
            "thresholds": {"soft_pct": 0.8, "hard_pct": 1.0},
            "on_estimated": {"mode": "enforce"},
        }
    )
    db.start_run("br1", "m", "cli", profile="coder")
    db._get_conn().execute("UPDATE runs SET cost_usd = 5.0 WHERE session_id = 'br1'")

    verdicts = budget.evaluate_run(db.get_run("br1"))
    profile_verdicts = [v for v in verdicts if v.scope == "profile"]
    assert len(profile_verdicts) == 1
    assert profile_verdicts[0].scope_id == "coder"
    assert profile_verdicts[0].status == "hard"
