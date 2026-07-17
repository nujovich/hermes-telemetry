"""hermes telemetry pricing drift — module logic + CLI wiring."""

from __future__ import annotations

import json  # noqa: F401 -- used by later tasks in this plan (CLI --json output)

import pytest


@pytest.fixture(autouse=True)
def isolated_db():
    import hermes_telemetry.db as db_mod

    db_mod._local.conn = None
    yield
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


import hermes_telemetry.db as db
import hermes_telemetry.pricing as pricing
import hermes_telemetry.pricing_drift as pricing_drift

_NOW = "2026-07-16T00:00:00+00:00"


def _snap(inp, out):
    return {"input_cost_per_million": inp, "output_cost_per_million": out}


def _write_pricing_yaml(models: dict):
    import yaml
    from hermes_telemetry import paths

    path = paths.get_pricing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"models": models}, f, sort_keys=False)
    pricing.reload_custom_pricing()


def test_list_latest_pricing_snapshots_returns_latest_per_pair():
    db.record_pricing_snapshot("nous", "m", _snap(1.0, 2.0))
    db.record_pricing_snapshot("nous", "m", _snap(1.5, 2.5))  # newer → new row
    rows = db.list_latest_pricing_snapshots()
    ms = [r for r in rows if r["model"] == "m"]
    assert len(ms) == 1
    assert ms[0]["input_cost_per_million"] == 1.5
    assert ms[0]["output_cost_per_million"] == 2.5


def test_list_latest_pricing_snapshots_returns_all_pairs():
    db.record_pricing_snapshot("nous", "a", _snap(1.0, 2.0))
    db.record_pricing_snapshot("nous", "b", _snap(3.0, 4.0))
    rows = db.list_latest_pricing_snapshots()
    pairs = {(r["provider"], r["model"]) for r in rows}
    assert ("nous", "a") in pairs
    assert ("nous", "b") in pairs


def test_list_latest_pricing_snapshots_empty_returns_empty_list():
    assert db.list_latest_pricing_snapshots() == []


def test_run_detects_drift_on_input_and_output():
    db.record_pricing_snapshot("nous", "deepseek/deepseek-v4-pro", _snap(0.435, 0.87))
    _write_pricing_yaml({"deepseek/deepseek-v4-pro": {"input": 1.6, "output": 3.2}})
    result = pricing_drift.run(apply=False)
    assert len(result["drifted"]) == 1
    d = result["drifted"][0]
    assert d["model"] == "deepseek/deepseek-v4-pro"
    assert d["provider"] == "nous"
    assert d["snap_input"] == 0.435
    assert d["local_input"] == 1.6
    assert d["snap_output"] == 0.87
    assert d["local_output"] == 3.2
    assert result["written"] == 0
    assert result["in_sync"] == 0


def test_run_within_threshold_is_in_sync():
    db.record_pricing_snapshot("nous", "m", _snap(1.0, 2.0))
    _write_pricing_yaml({"m": {"input": 1.005, "output": 2.0}})  # 0.5% < 1%
    result = pricing_drift.run(apply=False, threshold_pct=1.0)
    assert result["drifted"] == []
    assert result["in_sync"] == 1
    assert result["compared"] == 1


def test_run_skips_subscription_entries():
    db.record_pricing_snapshot("nous", "sub/model", _snap(5.0, 10.0))
    _write_pricing_yaml({"sub/model": {"input": 0.0, "output": 0.0, "_subscription": True}})
    result = pricing_drift.run(apply=False)
    assert result["drifted"] == []
    assert {"provider": "nous", "model": "sub/model"} in result["skipped_subscription"]


def test_run_collapses_dated_to_canonical():
    db.record_pricing_snapshot(
        "nous",
        "deepseek/deepseek-v4-pro-20260423",
        _snap(0.435, 0.87),
        resolved_model="deepseek/deepseek-v4-pro",
    )
    _write_pricing_yaml({"deepseek/deepseek-v4-pro": {"input": 1.6, "output": 3.2}})
    result = pricing_drift.run(apply=False)
    assert len(result["drifted"]) == 1
    assert result["drifted"][0]["model"] == "deepseek/deepseek-v4-pro"


def test_run_reports_snapshot_without_local_price():
    db.record_pricing_snapshot("nous", "ghost/model", _snap(1.0, 2.0))
    _write_pricing_yaml({})  # empty pricing.yaml
    result = pricing_drift.run(apply=False)
    assert {"provider": "nous", "model": "ghost/model"} in result["no_local_price"]
    assert result["drifted"] == []


def test_run_flags_coverage_gap():
    db.record_llm_call("s1", _NOW, "uncovered/model", "nous", 10, 2, 0.01, 100)
    result = pricing_drift.run(apply=False)
    assert result["coverage_gap"] >= 1


def test_run_model_filter():
    db.record_pricing_snapshot("nous", "a", _snap(0.435, 0.87))
    db.record_pricing_snapshot("nous", "b", _snap(0.435, 0.87))
    _write_pricing_yaml({"a": {"input": 1.6, "output": 3.2}, "b": {"input": 1.6, "output": 3.2}})
    result = pricing_drift.run(apply=False, model="a")
    assert len(result["drifted"]) == 1
    assert result["drifted"][0]["model"] == "a"
