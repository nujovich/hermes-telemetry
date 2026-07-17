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
import hermes_telemetry.pricing_drift as pricing_drift  # noqa: F401 -- exercised by later tasks

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
