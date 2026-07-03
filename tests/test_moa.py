"""Tests for moa.py — MoA (Mixture-of-Agents) preset resolution helpers."""

from __future__ import annotations

import moa

# ---------------------------------------------------------------------------
# is_moa
# ---------------------------------------------------------------------------


def test_is_moa_true_variants():
    assert moa.is_moa("moa")
    assert moa.is_moa("MoA")
    assert moa.is_moa("  moa  ")


def test_is_moa_false():
    assert not moa.is_moa("openrouter")
    assert not moa.is_moa("")
    assert not moa.is_moa(None)


# ---------------------------------------------------------------------------
# aggregator_from_preset
# ---------------------------------------------------------------------------


def test_aggregator_from_preset_extracts_slot():
    preset = {"aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"}}
    assert moa.aggregator_from_preset(preset) == ("openrouter", "anthropic/claude-opus-4.8")


def test_aggregator_from_preset_strips_whitespace():
    preset = {"aggregator": {"provider": "  nous ", "model": " kimi "}}
    assert moa.aggregator_from_preset(preset) == ("nous", "kimi")


def test_aggregator_from_preset_missing_or_malformed():
    assert moa.aggregator_from_preset(None) == ("", "")
    assert moa.aggregator_from_preset({}) == ("", "")
    assert moa.aggregator_from_preset({"aggregator": None}) == ("", "")
    assert moa.aggregator_from_preset({"aggregator": "nope"}) == ("", "")
    assert moa.aggregator_from_preset({"aggregator": {}}) == ("", "")


# ---------------------------------------------------------------------------
# reference_labels
# ---------------------------------------------------------------------------


def test_reference_labels():
    preset = {
        "reference_models": [
            {"provider": "openai-codex", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
        ]
    }
    assert moa.reference_labels(preset) == [
        "openai-codex:gpt-5.5",
        "openrouter:deepseek/deepseek-v4-pro",
    ]


def test_reference_labels_empty_or_malformed():
    assert moa.reference_labels(None) == []
    assert moa.reference_labels({}) == []
    assert moa.reference_labels({"reference_models": None}) == []
    assert moa.reference_labels({"reference_models": ["bad", 3]}) == []


# ---------------------------------------------------------------------------
# resolve_preset (swallows errors; delegates to hermes_cli)
# ---------------------------------------------------------------------------


def test_resolve_preset_empty_name_returns_none():
    assert moa.resolve_preset("") is None


def test_resolve_preset_returns_dict(monkeypatch):
    fake = {"aggregator": {"provider": "openrouter", "model": "x"}, "reference_models": []}
    monkeypatch.setattr(moa, "_resolve_preset_via_hermes", lambda name: fake)
    assert moa.resolve_preset("default") is fake


def test_resolve_preset_swallows_errors(monkeypatch):
    def boom(name):
        raise RuntimeError("hermes_cli not importable")

    monkeypatch.setattr(moa, "_resolve_preset_via_hermes", boom)
    assert moa.resolve_preset("default") is None


def test_resolve_preset_non_dict_returns_none(monkeypatch):
    monkeypatch.setattr(moa, "_resolve_preset_via_hermes", lambda name: "not-a-dict")
    assert moa.resolve_preset("default") is None
