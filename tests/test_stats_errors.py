"""Tests for the LLM failed-attempt surfaces (issue #11): the error-rate line in
the /stats summary and the /stats errors view.

Covers:
  - db.llm_error_rate / db.stats_errors: period, date-range and profile filtering
  - stats.handle: the summary line (omitted at zero errors) and the errors view
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import hermes_telemetry.db as db
import hermes_telemetry.stats as stats_mod
import pytest


@pytest.fixture(autouse=True)
def isolated_db():
    db._local.conn = None
    yield
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _ok(session="s1", ts=None, n=1, model="m", provider="p"):
    db.start_run(session, model=model, platform="cli")
    for _ in range(n):
        db.record_llm_call(session, ts or db._utcnow(), model, provider, 10, 5, 0.001, 50)


def _err(session="s1", ts=None, status=429, etype="rate_limit_error", model="m", provider="p"):
    db.record_llm_call_error(
        session,
        ts or db._utcnow(),
        model=model,
        provider=provider,
        status_code=status,
        error_type=etype,
        error_message="boom",
    )


# ---------------------------------------------------------------------------
# db.llm_error_rate
# ---------------------------------------------------------------------------


def test_llm_error_rate_zero_when_no_errors():
    _ok(n=3)
    r = db.llm_error_rate(window_hours=24)
    assert r["errors"] == 0
    assert r["successes"] == 3
    assert r["error_rate"] == 0.0


def test_llm_error_rate_is_errors_over_errors_plus_successes():
    _ok(n=3)
    _err()
    r = db.llm_error_rate(window_hours=24)
    assert (r["errors"], r["successes"]) == (1, 3)
    assert r["error_rate"] == pytest.approx(0.25)


def test_llm_error_rate_all_errors_no_successes():
    _err()
    _err()
    r = db.llm_error_rate(window_hours=24)
    assert (r["errors"], r["successes"], r["error_rate"]) == (2, 0, 1.0)


def test_llm_error_rate_empty_db_is_zero_not_a_division_error():
    r = db.llm_error_rate(window_hours=24)
    assert (r["errors"], r["successes"], r["error_rate"]) == (0, 0, 0.0)


def test_llm_error_rate_respects_the_window():
    _ok(n=1)
    _err(ts=_ago(1))
    _err(ts=_ago(24 * 5))  # outside 24h, inside 168h
    assert db.llm_error_rate(window_hours=24)["errors"] == 1
    assert db.llm_error_rate(window_hours=168)["errors"] == 2


def test_llm_error_rate_respects_date_range():
    _err(ts="2026-06-10T12:00:00+00:00")
    _err(ts="2026-06-20T12:00:00+00:00")
    r = db.llm_error_rate(date_from="2026-06-15", date_to="2026-06-30")
    assert r["errors"] == 1


def test_llm_error_rate_filters_by_profile():
    db.start_run("s-faro", model="m", platform="cli", profile="faro")
    db.start_run("s-other", model="m", platform="cli", profile="other")
    db.start_run("s-none", model="m", platform="cli")
    db.record_llm_call("s-faro", db._utcnow(), "m", "p", 1, 1, 0.0, 5)
    _err("s-faro")
    _err("s-other")
    _err("s-other")
    _err("s-none")
    faro = db.llm_error_rate(window_hours=24, profile="faro")
    assert (faro["errors"], faro["successes"], faro["error_rate"]) == (1, 1, 0.5)
    assert db.llm_error_rate(window_hours=24, profile="other")["errors"] == 2
    assert db.llm_error_rate(window_hours=24, profile="-")["errors"] == 1
    assert db.llm_error_rate(window_hours=24)["errors"] == 4


# ---------------------------------------------------------------------------
# db.stats_errors
# ---------------------------------------------------------------------------


def test_stats_errors_empty():
    assert db.stats_errors(window_hours=24) == {"by_type": [], "by_model": []}


def test_stats_errors_groups_by_type_and_status_with_last_seen():
    newest = _ago(1)
    _err(ts=_ago(3), status=429, etype="rate_limit_error")
    _err(ts=newest, status=429, etype="rate_limit_error")
    _err(ts=_ago(2), status=500, etype="APIError")
    _err(ts=_ago(2), status=None, etype="Timeout")
    rows = db.stats_errors(window_hours=24)["by_type"]
    keyed = {(r["error_type"], r["status_code"]): r for r in rows}
    assert set(keyed) == {("rate_limit_error", 429), ("APIError", 500), ("Timeout", None)}
    assert keyed[("rate_limit_error", 429)]["count"] == 2
    assert keyed[("rate_limit_error", 429)]["last_seen"] == newest
    # Busiest group first.
    assert rows[0]["error_type"] == "rate_limit_error"


def test_stats_errors_groups_by_provider_and_model():
    _err(provider="nous", model="a")
    _err(provider="nous", model="a")
    _err(provider="nous", model="b")
    _err(provider="", model="")
    rows = db.stats_errors(window_hours=24)["by_model"]
    keyed = {(r["provider"], r["model"]): r["count"] for r in rows}
    assert keyed == {("nous", "a"): 2, ("nous", "b"): 1, ("(unknown)", "(unknown)"): 1}
    assert (rows[0]["provider"], rows[0]["model"]) == ("nous", "a")
    assert all(r["last_seen"] for r in rows)


def test_stats_errors_respects_window_and_profile():
    db.start_run("s-faro", model="m", platform="cli", profile="faro")
    _err("s-faro", ts=_ago(1))
    _err("s-faro", ts=_ago(24 * 5))
    _err("s-other", ts=_ago(1))
    assert sum(r["count"] for r in db.stats_errors(window_hours=24)["by_type"]) == 2
    assert sum(r["count"] for r in db.stats_errors(window_hours=168)["by_type"]) == 3
    only = db.stats_errors(window_hours=168, profile="faro")["by_type"]
    assert sum(r["count"] for r in only) == 2


# ---------------------------------------------------------------------------
# /stats summary line
# ---------------------------------------------------------------------------


def test_summary_omits_error_line_when_zero_errors():
    _ok(n=2)
    out = stats_mod.handle("")
    assert "Failed attempts" not in out
    assert "error rate" not in out


def test_summary_shows_failed_attempts_and_error_rate():
    _ok(n=3)
    _err()
    out = stats_mod.handle("")
    line = next(ln for ln in out.splitlines() if "Failed attempts" in ln)
    assert "1" in line
    assert "25.0%" in line


@pytest.mark.parametrize("arg", ["week", "month"])
def test_summary_error_line_follows_the_selected_period(arg):
    _ok(n=1)
    _err(ts=_ago(24 * 5))  # outside 24h, inside week/month
    assert "Failed attempts" not in stats_mod.handle("")
    assert "Failed attempts" in stats_mod.handle(arg)


def test_summary_error_line_follows_profile_filter():
    db.start_run("s-faro", model="m", platform="cli", profile="faro")
    _err("s-faro")
    assert "Failed attempts" in stats_mod.handle("--profile faro")
    assert "Failed attempts" not in stats_mod.handle("--profile nobody")


# ---------------------------------------------------------------------------
# /stats errors
# ---------------------------------------------------------------------------


def test_errors_view_empty_message():
    out = stats_mod.handle("errors")
    assert "No LLM call errors recorded" in out
    assert "last 24 h" in out


def test_errors_view_renders_both_groupings():
    _ok(n=3, provider="nous", model="deepseek")
    _err(provider="nous", model="deepseek", status=429, etype="rate_limit_error")
    _err(provider="nous", model="deepseek", status=429, etype="rate_limit_error")
    _err(provider="openrouter", model="gpt", status=500, etype="APIError")
    out = stats_mod.handle("errors")
    assert "errors (last 24 h)" in out
    assert "rate_limit_error" in out and "429" in out
    assert "APIError" in out and "500" in out
    assert "nous" in out and "deepseek" in out
    assert "openrouter" in out
    assert "Last seen" in out
    # Header carries the totals: 3 failed attempts of 6 total = 50.0%.
    assert "3" in out and "50.0%" in out


@pytest.mark.parametrize(
    "arg,label",
    [
        ("errors today", "last 24 h"),
        ("errors week", "last 7 days"),
        ("errors month", "last 30 days"),
    ],
)
def test_errors_view_period_subcommands(arg, label):
    _err()
    assert label in stats_mod.handle(arg)


def test_errors_view_honours_date_range_and_profile():
    db.start_run("s-faro", model="m", platform="cli", profile="faro")
    _err("s-faro", ts="2026-06-20T12:00:00+00:00", etype="OnlyFaro")
    _err("s-x", ts="2026-06-21T12:00:00+00:00", etype="Other")
    out = stats_mod.handle("errors --from 2026-06-15 --to 2026-06-30 --profile faro")
    assert "OnlyFaro" in out
    assert "Other" not in out
    assert "profile faro" in out


def test_usage_text_lists_errors_view():
    assert "/stats errors" in stats_mod.handle("bogus-view")
