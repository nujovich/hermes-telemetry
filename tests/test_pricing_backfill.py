"""hermes telemetry pricing backfill — module logic + CLI wiring."""

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


import hermes_telemetry.core_pricing as core_pricing
import hermes_telemetry.db as db
import hermes_telemetry.pricing_backfill as pricing_backfill

_NOW = "2026-07-14T00:00:00+00:00"


def _seed():
    db.record_llm_call("s1", _NOW, "deepseek/deepseek-v4-pro-20260423", "nous", 10, 2, 0.01, 100)
    db.record_llm_call("s2", _NOW, "tencent/hy3:free", "nous", 5, 1, 0.0, 50)
    db.record_llm_call("s3", _NOW, "nvidia/nemotron-3-ultra:free", "nous", 5, 1, 0.0, 50)


def _fake_resolve(model, provider="", base_url=""):
    if model == "deepseek/deepseek-v4-pro":
        return {
            "input_cost_per_million": 0.435,
            "output_cost_per_million": 0.87,
            "source": "provider_models_api",
        }
    if model == "tencent/hy3:free":
        return {
            "input_cost_per_million": 0.0,
            "output_cost_per_million": 0.0,
            "source": "provider_models_api",
        }
    return None  # nemotron never resolves (dated or canonical)


def test_run_dry_run_writes_nothing(monkeypatch):
    _seed()
    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve)
    result = pricing_backfill.run(apply=False)
    assert result["applied"] is False
    assert result["to_process"] == 3
    assert len(result["resolvable"]) == 2
    assert result["via_fallback"] == 1  # deepseek recovered via canonical
    assert len(result["unresolvable"]) == 1
    assert result["written"] == 0
    assert db.get_latest_pricing_snapshot("nous", "deepseek/deepseek-v4-pro-20260423") is None


def test_run_apply_writes_rows_and_is_idempotent(monkeypatch):
    _seed()
    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve)
    r1 = pricing_backfill.run(apply=True)
    assert r1["written"] == 2
    row = db.get_latest_pricing_snapshot("nous", "deepseek/deepseek-v4-pro-20260423")
    assert row["resolved_model"] == "deepseek/deepseek-v4-pro"
    assert row["input_cost_per_million"] == 0.435
    assert db.get_latest_pricing_snapshot("nous", "tencent/hy3:free")["resolved_model"] is None

    r2 = pricing_backfill.run(apply=True)
    assert r2["written"] == 0
    assert r2["to_process"] == 1  # only the still-unresolvable nemotron remains
    assert len(r2["unresolvable"]) == 1


def test_run_apply_unresolvable_writes_no_row(monkeypatch):
    _seed()
    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve)
    pricing_backfill.run(apply=True)
    assert db.get_latest_pricing_snapshot("nous", "nvidia/nemotron-3-ultra:free") is None


def test_render_dry_run_text(monkeypatch):
    _seed()
    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve)
    text = pricing_backfill.render(pricing_backfill.run(apply=False))
    assert "dry-run" in text
    assert "To process:" in text
    assert "nvidia/nemotron-3-ultra:free" in text


def test_to_json_roundtrips(monkeypatch):
    _seed()
    monkeypatch.setattr(core_pricing, "resolve", _fake_resolve)
    payload = json.loads(pricing_backfill.to_json(pricing_backfill.run(apply=True)))
    assert payload["written"] == 2
    assert payload["via_fallback"] == 1


def test_cli_pricing_backfill_dry_run_default(monkeypatch, capsys):
    import hermes_telemetry.pricing_backfill as pb
    import hermes_telemetry.telemetry_cli as cli

    captured = {}

    def _fake_run(apply):
        captured["apply"] = apply
        return {
            "applied": apply,
            "distinct_total": 0,
            "already_covered": 0,
            "to_process": 0,
            "resolvable": [],
            "unresolvable": [],
            "written": 0,
            "via_fallback": 0,
        }

    monkeypatch.setattr(pb, "run", _fake_run)
    cli.main(["pricing", "backfill"])
    assert captured["apply"] is False
    assert "dry-run" in capsys.readouterr().out


def test_cli_pricing_backfill_apply_json(monkeypatch, capsys):
    import hermes_telemetry.pricing_backfill as pb
    import hermes_telemetry.telemetry_cli as cli

    captured = {}

    def _fake_run(apply):
        captured["apply"] = apply
        return {
            "applied": apply,
            "distinct_total": 3,
            "already_covered": 1,
            "to_process": 2,
            "resolvable": [{"provider": "nous", "model": "m", "resolved_model": None}],
            "unresolvable": [],
            "written": 1,
            "via_fallback": 0,
        }

    monkeypatch.setattr(pb, "run", _fake_run)
    cli.main(["pricing", "backfill", "--apply", "--json"])
    assert captured["apply"] is True
    assert json.loads(capsys.readouterr().out)["written"] == 1
