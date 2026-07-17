"""hermes telemetry pricing drift — module logic + CLI wiring."""

from __future__ import annotations

import json

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
    _write_pricing_yaml({})
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


def test_drift_pct_none_for_zero_snapshot_and_numeric_otherwise():
    from hermes_telemetry import pricing_drift as pd

    assert pd._drift_pct(1.0, 0.0) is None
    assert pd._drift_pct(0.0, 0.0) is None
    assert pd._drift_pct(1.5, 1.0) == 50.0
    assert pd._drift_pct(0.5, 1.0) == -50.0


def test_is_drift_zero_snapshot_and_threshold_boundary():
    from hermes_telemetry import pricing_drift as pd

    assert pd._is_drift(1.0, 0.0, 1.0) is True
    assert pd._is_drift(0.0, 0.0, 1.0) is False
    assert pd._is_drift(1.01, 1.0, 1.0) is False  # exactly 1.0% is NOT > 1.0%
    assert pd._is_drift(1.02, 1.0, 1.0) is True


def test_run_zero_snapshot_drifts_with_none_pct_and_json_safe():
    db.record_pricing_snapshot("nous", "m", _snap(0.0, 0.0))
    _write_pricing_yaml({"m": {"input": 1.0, "output": 2.0}})
    result = pricing_drift.run(apply=False)
    assert len(result["drifted"]) == 1
    d = result["drifted"][0]
    assert d["input_drift_pct"] is None
    assert d["output_drift_pct"] is None
    # JSON-safe: no Infinity token
    payload = json.loads(json.dumps(result, default=str))
    assert payload["drifted"][0]["input_drift_pct"] is None


def test_run_output_only_drift():
    db.record_pricing_snapshot("nous", "m", _snap(1.0, 2.0))
    _write_pricing_yaml({"m": {"input": 1.0, "output": 3.0}})  # input sync, output +50%
    result = pricing_drift.run(apply=False)
    assert len(result["drifted"]) == 1
    assert result["drifted"][0]["output_drift_pct"] == 50.0
    assert result["in_sync"] == 0


def test_run_provider_assumed_routed_to_no_local_price(monkeypatch):
    db.record_pricing_snapshot("nous", "x", _snap(0.5, 1.0))
    _write_pricing_yaml({"x": {"input": 1.6, "output": 3.2}})
    monkeypatch.setattr(
        pricing,
        "_resolve_pricing",
        lambda m, p="": {"input": 1.6, "output": 3.2, "_provider_assumed": True},
    )
    result = pricing_drift.run(apply=False)
    assert {"provider": "nous", "model": "x"} in result["no_local_price"]
    assert result["drifted"] == []


def test_render_dry_run_lists_drift_and_coverage_hint():
    db.record_pricing_snapshot("nous", "deepseek/deepseek-v4-pro", _snap(0.435, 0.87))
    _write_pricing_yaml({"deepseek/deepseek-v4-pro": {"input": 1.6, "output": 3.2}})
    db.record_llm_call("s1", _NOW, "uncovered/model", "nous", 10, 2, 0.01, 100)
    text = pricing_drift.render(pricing_drift.run(apply=False))
    assert "dry-run" in text
    assert "deepseek/deepseek-v4-pro" in text
    assert "backfill" in text  # coverage-gap hint present on unfiltered run
    assert "--apply" in text


def test_render_zero_snapshot_shows_was_0_not_crash():
    db.record_pricing_snapshot("nous", "m", _snap(0.0, 0.0))
    _write_pricing_yaml({"m": {"input": 1.0, "output": 2.0}})
    text = pricing_drift.render(pricing_drift.run(apply=False))
    assert "was $0" in text  # None pct rendered safely, no format crash


def test_render_coverage_hint_suppressed_when_model_filtered():
    db.record_pricing_snapshot("nous", "a", _snap(0.435, 0.87))
    _write_pricing_yaml({"a": {"input": 1.6, "output": 3.2}})
    db.record_llm_call("s1", _NOW, "uncovered/model", "nous", 10, 2, 0.01, 100)
    text = pricing_drift.render(pricing_drift.run(apply=False, model="a"))
    assert "backfill" not in text  # scoped run must not nag about unrelated models


def test_render_apply_summary():
    db.record_pricing_snapshot("nous", "m", _snap(0.435, 0.87))
    _write_pricing_yaml({"m": {"input": 1.6, "output": 3.2}})
    text = pricing_drift.render(pricing_drift.run(apply=True))
    assert "--apply" in text
    assert "pricing.yaml" in text


def test_to_json_roundtrips():
    db.record_pricing_snapshot("nous", "m", _snap(0.435, 0.87))
    _write_pricing_yaml({"m": {"input": 1.6, "output": 3.2}})
    payload = json.loads(pricing_drift.to_json(pricing_drift.run(apply=False)))
    assert len(payload["drifted"]) == 1
    assert payload["drifted"][0]["model"] == "m"


def test_apply_rewrites_input_output_and_reloads():
    db.record_pricing_snapshot("nous", "m", _snap(0.435, 0.87))
    _write_pricing_yaml({"m": {"input": 1.6, "output": 3.2}})
    result = pricing_drift.run(apply=True)
    assert result["written"] == 1
    # Cache was reloaded → new resolution reflects the snapshot rate.
    prices = pricing._resolve_pricing("m", "nous")
    assert prices["input"] == 0.435
    assert prices["output"] == 0.87


def test_apply_tags_source_and_preserves_other_entries():
    import yaml
    from hermes_telemetry import paths

    db.record_pricing_snapshot("nous", "m", _snap(0.435, 0.87))
    _write_pricing_yaml(
        {"m": {"input": 1.6, "output": 3.2}, "keep/me": {"input": 9.0, "output": 9.0}}
    )
    pricing_drift.run(apply=True)
    with open(paths.get_pricing_path()) as f:
        data = yaml.safe_load(f)
    assert data["models"]["m"]["input"] == 0.435
    assert data["models"]["m"]["output"] == 0.87
    assert data["models"]["m"]["_source"] == "core-snapshot"
    # Untouched entry survives the merge.
    assert data["models"]["keep/me"]["input"] == 9.0


def test_apply_does_not_write_subscription_entries():
    import yaml
    from hermes_telemetry import paths

    db.record_pricing_snapshot("nous", "sub/model", _snap(5.0, 10.0))
    _write_pricing_yaml({"sub/model": {"input": 0.0, "output": 0.0, "_subscription": True}})
    result = pricing_drift.run(apply=True)
    assert result["written"] == 0
    with open(paths.get_pricing_path()) as f:
        data = yaml.safe_load(f)
    assert data["models"]["sub/model"]["input"] == 0.0  # unchanged


def test_apply_creates_new_entry_when_model_absent():
    import yaml
    from hermes_telemetry import paths

    _write_pricing_yaml({})  # empty models map
    written = pricing_drift._apply_drift(
        [{"provider": "nous", "model": "brand/new", "snap_input": 0.4, "snap_output": 0.8}]
    )
    assert written == 1
    with open(paths.get_pricing_path()) as f:
        data = yaml.safe_load(f)
    assert data["models"]["brand/new"]["input"] == 0.4
    assert data["models"]["brand/new"]["output"] == 0.8
    assert data["models"]["brand/new"]["_source"] == "core-snapshot"


def test_apply_updates_legacy_flat_format_in_place():
    import yaml
    from hermes_telemetry import paths

    path = paths.get_pricing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"m": {"input": 1.6, "output": 3.2}}, f)  # legacy flat: no models/defaults
    pricing.reload_custom_pricing()
    written = pricing_drift._apply_drift(
        [{"provider": "nous", "model": "m", "snap_input": 0.4, "snap_output": 0.8}]
    )
    assert written == 1
    with open(path) as f:
        data = yaml.safe_load(f)
    assert data["m"]["input"] == 0.4  # updated in the flat map
    assert "models" not in data


def test_apply_same_model_two_providers_writes_once():
    written = pricing_drift._apply_drift(
        [
            {"provider": "nous", "model": "m", "snap_input": 0.4, "snap_output": 0.8},
            {"provider": "openrouter", "model": "m", "snap_input": 0.4, "snap_output": 0.8},
        ]
    )
    assert written == 1  # same model+rates → written once, not double-counted


def test_apply_same_model_conflicting_rates_keeps_first(caplog):
    import logging

    import yaml
    from hermes_telemetry import paths

    _write_pricing_yaml({})
    with caplog.at_level(logging.WARNING, logger="hermes_telemetry.pricing_drift"):
        written = pricing_drift._apply_drift(
            [
                {"provider": "nous", "model": "m", "snap_input": 0.4, "snap_output": 0.8},
                {"provider": "openrouter", "model": "m", "snap_input": 0.9, "snap_output": 1.8},
            ]
        )
    assert written == 1
    with open(paths.get_pricing_path()) as f:
        data = yaml.safe_load(f)
    assert data["models"]["m"]["input"] == 0.4  # first kept
    assert "conflicting core rates" in caplog.text
