"""hermes telemetry pricing recalculate — module logic + CLI wiring."""

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
import hermes_telemetry.pricing_recalculate as pricing_recalculate

_NOW = "2026-07-16T00:00:00+00:00"


def _write_pricing_yaml(models: dict):
    import yaml
    from hermes_telemetry import paths

    path = paths.get_pricing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"models": models}, f, sort_keys=False)
    pricing.reload_custom_pricing()


def _seed():
    # Explicitly-priced row with a STALE cost: 1000 in / 1000 out at
    # 0.435 / 0.87 per million == 0.001305, but recorded as 0.005 (old rate).
    db.record_llm_call("s1", _NOW, "recalc/model", "nous", 1000, 1000, 0.005, 100)
    # Estimated row: NOT a candidate.
    db.record_llm_call("s2", _NOW, "recalc/model", "nous", 1000, 1000, 0.005, 100, estimated=True)
    # Provider-assumed row: NOT a candidate.
    db.record_llm_call(
        "s3", _NOW, "recalc/model", "nous", 1000, 1000, 0.005, 100, provider_assumed=True
    )


def test_only_explicit_rows_are_candidates():
    _seed()
    _write_pricing_yaml({"recalc/model": {"input": 0.435, "output": 0.87}})
    result = pricing_recalculate.run(apply=False)
    assert result["candidates"] == 1  # only s1, not the estimated/assumed rows


def test_dry_run_reports_but_writes_nothing():
    _seed()
    _write_pricing_yaml({"recalc/model": {"input": 0.435, "output": 0.87}})
    result = pricing_recalculate.run(apply=False)
    assert result["applied"] is False
    assert result["changed"] == 1
    assert result["unchanged"] == 0
    assert result["skipped_unpriced"] == 0
    # Stale cost is reported but NOT rewritten.
    assert abs(result["old_total"] - 0.005) < 1e-9
    assert abs(result["new_total"] - 0.001305) < 1e-9
    assert db.get_run("s1")["cost_usd"] == 0.005


def test_apply_updates_row_and_run_cost():
    _seed()
    _write_pricing_yaml({"recalc/model": {"input": 0.435, "output": 0.87}})
    result = pricing_recalculate.run(apply=True)
    assert result["applied"] is True
    assert result["changed"] == 1

    conn = db._get_conn()
    row = conn.execute("SELECT cost_usd FROM llm_calls WHERE session_id = 's1'").fetchone()
    assert abs(row["cost_usd"] - 0.001305) < 1e-9
    # runs.cost_usd re-summed to the corrected row cost.
    assert abs(db.get_run("s1")["cost_usd"] - 0.001305) < 1e-9


def test_unchanged_rows_are_not_rewritten():
    _seed()
    _write_pricing_yaml({"recalc/model": {"input": 0.435, "output": 0.87}})
    # Rewrite s1 to the already-correct cost, then recalc: no change.
    conn = db._get_conn()
    conn.execute("UPDATE llm_calls SET cost_usd = 0.001305 WHERE session_id = 's1'")
    result = pricing_recalculate.run(apply=False)
    assert result["changed"] == 0
    assert result["unchanged"] == 1
    assert result["delta"] == 0.0


def test_skips_row_whose_model_is_no_longer_priced():
    db.record_llm_call("s1", _NOW, "ghost/model", "nous", 1000, 1000, 0.005, 100)
    _write_pricing_yaml({})  # no entry; ghost/model is not a built-in
    result = pricing_recalculate.run(apply=True)
    assert result["skipped_unpriced"] == 1
    assert result["changed"] == 0
    # Cost left untouched, not zeroed.
    assert db.get_run("s1")["cost_usd"] == 0.005


def test_render_dry_run_text():
    _seed()
    _write_pricing_yaml({"recalc/model": {"input": 0.435, "output": 0.87}})
    text = pricing_recalculate.render(pricing_recalculate.run(apply=False))
    assert "dry-run" in text
    assert "Would change:" in text
    assert "--apply" in text


def test_to_json_roundtrips():
    _seed()
    _write_pricing_yaml({"recalc/model": {"input": 0.435, "output": 0.87}})
    payload = json.loads(pricing_recalculate.to_json(pricing_recalculate.run(apply=True)))
    assert payload["changed"] == 1
    assert abs(payload["delta"] - (0.001305 - 0.005)) < 1e-9


def test_cli_pricing_recalculate_dry_run_default(monkeypatch, capsys):
    import hermes_telemetry.pricing_recalculate as pr
    import hermes_telemetry.telemetry_cli as cli

    captured = {}

    def _fake_run(apply):
        captured["apply"] = apply
        return {
            "applied": apply,
            "candidates": 0,
            "changed": 0,
            "unchanged": 0,
            "skipped_unpriced": 0,
            "old_total": 0.0,
            "new_total": 0.0,
            "delta": 0.0,
        }

    monkeypatch.setattr(pr, "run", _fake_run)
    cli.main(["pricing", "recalculate"])
    assert captured["apply"] is False
    assert "dry-run" in capsys.readouterr().out


def test_cli_pricing_recalculate_apply_json(monkeypatch, capsys):
    import hermes_telemetry.pricing_recalculate as pr
    import hermes_telemetry.telemetry_cli as cli

    captured = {}

    def _fake_run(apply):
        captured["apply"] = apply
        return {
            "applied": apply,
            "candidates": 3,
            "changed": 2,
            "unchanged": 1,
            "skipped_unpriced": 0,
            "old_total": 0.01,
            "new_total": 0.005,
            "delta": -0.005,
        }

    monkeypatch.setattr(pr, "run", _fake_run)
    cli.main(["pricing", "recalculate", "--apply", "--json"])
    assert captured["apply"] is True
    assert json.loads(capsys.readouterr().out)["changed"] == 2
