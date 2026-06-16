"""Tests for /stats models — per-model breakdown within each provider.

Covers:
  - db.stats_by_model: correct (provider, model) aggregation, real/estimated
    split, and ordering (provider asc, calls desc)
  - stats.handle('models'): table formatting + window subcommands
"""

from __future__ import annotations

import hermes_telemetry.db as db
import hermes_telemetry.stats as stats_mod
import pytest


@pytest.fixture(autouse=True)
def isolated_db():
    # HERMES_HOME is isolated by the conftest baseline; reset the DB conn here.
    db._local.conn = None
    yield
    if getattr(db._local, "conn", None):
        db._local.conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# db.stats_by_model
# ---------------------------------------------------------------------------


def test_stats_by_model_empty():
    assert db.stats_by_model(window_hours=24) == []


def test_stats_by_model_groups_by_provider_and_model():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    # Two distinct models under the openrouter provider, one under anthropic.
    db.record_llm_call(
        "s1", now, "google/gemini-3-flash-preview-20251217", "openrouter", 100, 50, 0.0, 100
    )
    db.record_llm_call(
        "s1", now, "google/gemini-3-flash-preview-20251217", "openrouter", 100, 50, 0.0, 100
    )
    db.record_llm_call("s1", now, "openai/gpt-5.5-20260423", "openrouter", 100, 50, 0.005, 100)
    db.record_llm_call("s1", now, "claude-opus-4-8", "anthropic", 100, 50, 0.01, 100)

    rows = db.stats_by_model(window_hours=24)
    # 3 distinct (provider, model) pairs
    assert len(rows) == 3

    keyed = {(r["provider"], r["model"]): r for r in rows}
    gem = keyed[("openrouter", "google/gemini-3-flash-preview-20251217")]
    assert gem["total_calls"] == 2
    assert gem["cost_usd"] == 0.0  # the dated $0.00 case made visible


def test_stats_by_model_real_vs_estimated_split():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelA", "openai", 100, 50, 0.001, 50, estimated=False)
    db.record_llm_call("s1", now, "modelA", "openai", 100, 50, 0.001, 50, estimated=True)

    rows = db.stats_by_model(window_hours=24)
    assert len(rows) == 1
    r = rows[0]
    assert r["total_calls"] == 2
    assert r["real_calls"] == 1
    assert r["estimated_calls"] == 1
    assert abs(r["estimated_pct"] - 0.5) < 1e-9


def test_stats_by_model_ordered_provider_then_calls_desc():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    # provider 'aaa' with a low-call model; provider 'bbb' with two models.
    db.record_llm_call("s1", now, "low", "aaa", 1, 1, 0.0, 1)
    db.record_llm_call("s1", now, "busy", "bbb", 1, 1, 0.0, 1)
    db.record_llm_call("s1", now, "busy", "bbb", 1, 1, 0.0, 1)
    db.record_llm_call("s1", now, "busy", "bbb", 1, 1, 0.0, 1)
    db.record_llm_call("s1", now, "quiet", "bbb", 1, 1, 0.0, 1)

    rows = db.stats_by_model(window_hours=24)
    # provider asc: aaa before bbb
    assert rows[0]["provider"] == "aaa"
    # within bbb: busy (3 calls) before quiet (1 call)
    bbb = [r for r in rows if r["provider"] == "bbb"]
    assert bbb[0]["model"] == "busy"
    assert bbb[0]["total_calls"] == 3
    assert bbb[1]["model"] == "quiet"


def test_stats_by_model_null_provider_and_model():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db._get_conn().execute(
        "INSERT INTO llm_calls (session_id, ts, model, provider, tokens_in, tokens_out, "
        "cost_usd, latency_ms, estimated) VALUES (?, ?, NULL, NULL, 0, 0, 0, 0, 0)",
        ("s1", now),
    )
    rows = db.stats_by_model(window_hours=24)
    assert rows[0]["provider"] == "(unknown)"
    assert rows[0]["model"] == "(unknown)"


# ---------------------------------------------------------------------------
# stats.handle('models') output format
# ---------------------------------------------------------------------------


def test_stats_models_command_output():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call(
        "s1", now, "google/gemini-3-flash-preview-20251217", "openrouter", 100, 50, 0.0, 100
    )
    db.record_llm_call("s1", now, "claude-opus-4-8", "anthropic", 100, 50, 0.01, 100)

    out = stats_mod.handle("models")
    # header columns present
    assert "Provider" in out
    assert "Model" in out
    assert "Calls" in out
    assert "Cost" in out
    # both models listed, dated model shows $0.00 separately
    assert "google/gemini-3-flash-preview-20251217" in out
    assert "claude-opus-4-8" in out
    assert "$0.000000" in out


def test_stats_models_empty_output():
    out = stats_mod.handle("models")
    assert "No API calls" in out


def test_stats_models_week_subcommand():
    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "modelX", "openai", 100, 50, 0.01, 100)

    out = stats_mod.handle("models week")
    assert "modelX" in out
    assert "last 7 days" in out


# ---------------------------------------------------------------------------
# $0.00 row classification: subscription vs no-price-entry
# ---------------------------------------------------------------------------


def _seed_pricing_yaml(tmp_path, body: str) -> None:
    pricing_dir = tmp_path / "telemetry"
    pricing_dir.mkdir(parents=True, exist_ok=True)
    (pricing_dir / "pricing.yaml").write_text(body)


def test_stats_models_zero_cost_subscription_row_labeled(tmp_path, monkeypatch):
    """A $0.00 row whose model is flagged `_subscription: true` is tagged as
    subscription/free-tier and the footer claims it as declared, NOT as missing
    pricing."""
    _seed_pricing_yaml(
        tmp_path,
        "models:\n"
        "  nvidia/nemotron-3-ultra:free:\n"
        "    input: 0.0\n"
        "    output: 0.0\n"
        "    _subscription: true\n",
    )
    # conftest already set HERMES_HOME=tmp_path; reload the pricing cache so it
    # picks up the file we just wrote.
    import hermes_telemetry.pricing as pricing

    pricing.reload_custom_pricing()

    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "nvidia/nemotron-3-ultra:free", "nous", 100, 50, 0.0, 100)

    out = stats_mod.handle("models")
    assert "nvidia/nemotron-3-ultra:free" in out
    assert "subscription/free-tier" in out
    assert "subscription/free tier (declared in pricing.yaml" in out
    # Should NOT claim missing pricing for this row.
    assert "no price entry" not in out


def test_stats_models_zero_cost_no_entry_row_labeled(tmp_path, monkeypatch):
    """A $0.00 row NOT flagged as subscription is tagged 'no price entry' and
    the footer keeps the original /setup pricing auto hint."""
    import hermes_telemetry.pricing as pricing

    pricing.reload_custom_pricing()  # ensure empty cache (no pricing.yaml)

    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "some/unpriced-model", "openrouter", 100, 50, 0.0, 100)

    out = stats_mod.handle("models")
    assert "some/unpriced-model" in out
    assert "no price entry" in out
    assert "/setup pricing auto" in out
    assert "subscription/free-tier" not in out


def test_stats_models_zero_cost_mixed_emits_both_footers(tmp_path, monkeypatch):
    """When the window contains both kinds of $0.00 rows, both footer lines are
    emitted so the user can tell them apart."""
    _seed_pricing_yaml(
        tmp_path,
        "models:\n"
        "  nvidia/nemotron-3-ultra:free:\n"
        "    input: 0.0\n"
        "    output: 0.0\n"
        "    _subscription: true\n",
    )
    import hermes_telemetry.pricing as pricing

    pricing.reload_custom_pricing()

    now = db._utcnow()
    db.start_run("s1", model="m", platform="cli")
    db.record_llm_call("s1", now, "nvidia/nemotron-3-ultra:free", "nous", 10, 10, 0.0, 50)
    db.record_llm_call("s1", now, "some/unpriced-model", "openrouter", 10, 10, 0.0, 50)

    out = stats_mod.handle("models")
    assert "subscription/free tier (declared in pricing.yaml" in out
    assert "no price entry in pricing.yaml" in out
    assert "/setup pricing auto" in out
