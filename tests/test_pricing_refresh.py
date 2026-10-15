"""Tests for pricing_refresh sources — GoogleAISource and registry integration."""

from __future__ import annotations

import datetime as dt

import pytest

from pricing import _DEFAULT_PRICING
from pricing_refresh import (
    _SOURCES,
    GoogleAISource,
    OpenRouterSource,
    PricingSource,
    register_source,
)

# ---------------------------------------------------------------------------
# GoogleAISource — shape and contract
# ---------------------------------------------------------------------------


def test_google_ai_source_is_pricing_source_subclass():
    assert issubclass(GoogleAISource, PricingSource)


def test_google_ai_source_has_required_class_attrs():
    assert GoogleAISource.name == "google-ai"
    assert isinstance(GoogleAISource.LAST_VERIFIED, str)
    # Must parse as ISO date so the LAST_VERIFIED contract is enforceable.
    dt.date.fromisoformat(GoogleAISource.LAST_VERIFIED)


def test_google_ai_source_fetch_returns_non_empty_dict():
    result = GoogleAISource().fetch()
    assert isinstance(result, dict)
    assert len(result) > 0


def test_google_ai_source_entries_have_required_fields():
    result = GoogleAISource().fetch()
    for model, pricing in result.items():
        assert "input" in pricing, f"{model} missing input"
        assert "output" in pricing, f"{model} missing output"
        assert "cache_read" in pricing, f"{model} missing cache_read"
        assert isinstance(pricing["input"], (int, float))
        assert isinstance(pricing["output"], (int, float))
        assert isinstance(pricing["cache_read"], (int, float))


def test_google_ai_source_prices_are_positive():
    result = GoogleAISource().fetch()
    for model, pricing in result.items():
        assert pricing["input"] > 0, f"{model} has non-positive input price"
        assert pricing["output"] > 0, f"{model} has non-positive output price"
        assert pricing["cache_read"] > 0, f"{model} has non-positive cache_read price"


def test_google_ai_source_cache_read_below_input():
    """Cache reads are always cheaper than fresh input tokens (Google AI policy)."""
    result = GoogleAISource().fetch()
    for model, pricing in result.items():
        assert pricing["cache_read"] < pricing["input"], (
            f"{model}: cache_read ({pricing['cache_read']}) should be < input ({pricing['input']})"
        )


def test_google_ai_source_uses_bare_model_ids():
    """Keys must not carry the 'google/' prefix — that's OpenRouterSource's territory."""
    result = GoogleAISource().fetch()
    for model in result:
        assert not model.startswith("google/"), (
            f"{model} should not have 'google/' prefix in GoogleAISource"
        )
        assert model.startswith("gemini-"), f"{model} should be a Gemini model id"


# ---------------------------------------------------------------------------
# Consistency with _DEFAULT_PRICING in pricing.py
# ---------------------------------------------------------------------------


def test_google_ai_source_matches_default_pricing():
    """GoogleAISource must agree with the embedded _DEFAULT_PRICING for shared keys.

    If they diverge, either the source is stale (bump LAST_VERIFIED + table)
    or _DEFAULT_PRICING is stale (sync from source).
    """
    google = GoogleAISource().fetch()
    for model, pricing in google.items():
        if model not in _DEFAULT_PRICING:
            continue  # source may carry models not yet in defaults — OK
        default = _DEFAULT_PRICING[model]
        for field in ("input", "output", "cache_read"):
            if field not in default:
                continue
            assert pricing[field] == default[field], (
                f"{model}.{field}: source={pricing[field]} default={default[field]}"
            )


def test_google_ai_source_covers_all_default_gemini_models():
    """Every Gemini entry in _DEFAULT_PRICING should be present in GoogleAISource.

    Catches the failure mode where _DEFAULT_PRICING gains a new Gemini model
    but the new source isn't updated.
    """
    gemini_defaults = {k for k in _DEFAULT_PRICING if k.startswith("gemini-")}
    source_models = set(GoogleAISource().fetch())
    missing = gemini_defaults - source_models
    assert not missing, (
        f"GoogleAISource missing Gemini models from _DEFAULT_PRICING: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_google_ai_source_is_registered():
    assert GoogleAISource in _SOURCES


def test_openrouter_source_is_registered():
    """Sanity check — ensures we didn't replace OpenRouterSource by accident."""
    assert OpenRouterSource in _SOURCES


def test_register_source_appends_to_registry():
    class _DummySource(PricingSource):
        name = "dummy"

        def fetch(self) -> dict[str, dict]:
            return {}

    original_len = len(_SOURCES)
    try:
        register_source(_DummySource)
        assert _DummySource in _SOURCES
        assert len(_SOURCES) == original_len + 1
    finally:
        _SOURCES.remove(_DummySource)


# ---------------------------------------------------------------------------
# Fetch returns independent copies
# ---------------------------------------------------------------------------


def test_google_ai_source_fetch_returns_copies_not_references():
    """Mutating the returned dict must not corrupt the source's class table."""
    src = GoogleAISource()
    first = src.fetch()
    first_model = next(iter(first))
    first[first_model]["input"] = 99999.0
    second = src.fetch()
    assert second[first_model]["input"] != 99999.0, (
        "fetch() should return fresh copies, not references to class state"
    )


# ---------------------------------------------------------------------------
# Subscription-model meta + survival across refresh (issue #24, Option A)
# ---------------------------------------------------------------------------


def _StubSource(models: dict[str, dict]):
    class _S(PricingSource):
        name = "openrouter"  # impersonate OpenRouter so entries carry that source

        def fetch(self) -> dict[str, dict]:
            return {k: dict(v) for k, v in models.items()}

    return _S


def test_manual_subscription_entry_survives_refresh(tmp_path, monkeypatch):
    """A hand-added `_subscription` entry under the provider-native (bare) id is
    never returned by OpenRouter, so a refresh leaves it untouched and records
    it in _meta.subscription_models — not in estimated_price_models."""
    import yaml

    import paths
    import pricing_refresh

    pfile = paths.get_pricing_path()
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "qwen3.7-plus": {"input": 0.0, "output": 0.0, "_subscription": True},
                },
                "defaults": {"cache_read_multiplier": 0.10, "cache_write_multiplier": 1.25},
            }
        )
    )
    # OpenRouter returns the PREFIXED form — a different key, so no collision.
    monkeypatch.setattr(
        pricing_refresh,
        "_SOURCES",
        [_StubSource({"qwen/qwen3.7-plus": {"input": 0.40, "output": 1.60}})],
    )

    changes, _overrides = pricing_refresh.refresh_pricing()

    written = yaml.safe_load(pfile.read_text())
    models = written["models"]
    meta = written["_meta"]
    # Manual subscription entry preserved verbatim
    assert models["qwen3.7-plus"]["_subscription"] is True
    assert models["qwen3.7-plus"]["input"] == 0.0
    # OpenRouter prefixed form added alongside (different key — no clobber)
    assert models["qwen/qwen3.7-plus"]["input"] == 0.40
    # Meta classifies the subscription model correctly
    assert "qwen3.7-plus" in meta["subscription_models"]
    assert "qwen3.7-plus" not in meta.get("estimated_price_models", [])


def test_refresh_pricing_honors_telemetry_home_over_hermes_home(tmp_path, monkeypatch):
    """refresh_pricing must resolve pricing.yaml via paths.get_pricing_path(),
    honoring HERMES_TELEMETRY_HOME over HERMES_HOME. Regression: it wrote to a
    module-level PRICING_FILE computed from HERMES_HOME at import time, so a set
    HERMES_TELEMETRY_HOME (the consolidated cost-center dir used to unify profiles
    on the VPS) was silently ignored and refresh landed in the wrong file."""
    import yaml

    import paths
    import pricing_refresh

    hermes_home = tmp_path / "hermes_home"
    telemetry_home = tmp_path / "telemetry_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(telemetry_home))

    monkeypatch.setattr(
        pricing_refresh,
        "_SOURCES",
        [_StubSource({"acme/model-x": {"input": 1.0, "output": 2.0}})],
    )

    pricing_refresh.refresh_pricing()

    # HERMES_TELEMETRY_HOME outranks HERMES_HOME → refresh must write here.
    target = telemetry_home / "telemetry" / "pricing.yaml"
    assert target == paths.get_pricing_path()
    assert target.exists()
    assert yaml.safe_load(target.read_text())["models"]["acme/model-x"]["input"] == 1.0
    # ...and must NOT write to the HERMES_HOME location.
    assert not (hermes_home / "telemetry" / "pricing.yaml").exists()


# ---------------------------------------------------------------------------
# Atomic write (issue #105/#106) — _save_yaml must never leave a
# truncated/partial pricing.yaml for a concurrent reader under Hermes
# multiplex (N plugin instances per process sharing one file).
# ---------------------------------------------------------------------------


def test_save_yaml_commits_via_os_replace(tmp_path, monkeypatch):
    """The write must commit via os.replace (atomic rename) from a distinct
    temp file, not an in-place truncate — the actual defect in #105/#106."""
    import paths
    import pricing_refresh

    target = paths.get_pricing_path()

    calls = []
    real_replace = pricing_refresh.os.replace

    def _spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(pricing_refresh.os, "replace", _spy_replace)

    pricing_refresh._save_yaml(target, {"models": {"m": {"input": 1.0, "output": 1.0}}})

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == target
    assert src != target  # committed from a separate temp file, not in-place
    assert target.exists()


def test_save_yaml_leaves_original_intact_on_replace_failure(tmp_path, monkeypatch):
    """If the commit step (os.replace) fails, the original file must survive
    untouched — never truncated or partially written — and no stray temp file
    should be left behind in the telemetry directory."""
    import paths
    import pricing_refresh

    target = paths.get_pricing_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("models:\n  original-model:\n    input: 1.0\n    output: 1.0\n")

    def _boom(*_args, **_kwargs):
        raise OSError("simulated failure mid-commit")

    monkeypatch.setattr(pricing_refresh.os, "replace", _boom)

    # _save_yaml is fail-open — it must not raise even though the commit failed.
    pricing_refresh._save_yaml(target, {"models": {"new-model": {"input": 9.0, "output": 9.0}}})

    content = target.read_text()
    assert "original-model" in content
    assert "new-model" not in content  # never truncated/partially overwritten

    leftovers = [p for p in target.parent.iterdir() if p != target]
    assert leftovers == [], f"temp file(s) left behind: {leftovers}"


def test_atomic_write_follows_symlink_and_keeps_the_link(tmp_path):
    """A pricing.yaml that is a symlink (e.g. into a dotfiles repo) must stay a
    symlink: the write lands in the real target, and os.replace must not
    swap the link itself for a regular file."""
    import pricing_refresh

    real = tmp_path / "real.yaml"
    real.write_text("models: {}\n")
    real.chmod(0o640)
    link = tmp_path / "pricing.yaml"
    link.symlink_to(real)

    new_content = "models:\n  m:\n    input: 1.0\n    output: 2.0\n"
    pricing_refresh._atomic_write_text(link, new_content)

    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert real.read_text() == new_content
    assert real.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pricing.yaml", "real.yaml"]


def test_atomic_write_preserves_existing_file_permissions(tmp_path):
    """A rewrite must not silently tighten pricing.yaml's permissions.
    tempfile.mkstemp() creates its file 0600 — if that mode rides along into
    the replace, a dashboard running as another user loses read access. The
    existing file's mode must be preserved across the atomic replace."""
    import os

    import paths
    import pricing_refresh

    target = paths.get_pricing_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("models: {}\n")
    os.chmod(target, 0o640)

    pricing_refresh._atomic_write_text(target, "models:\n  m:\n    input: 1.0\n    output: 1.0\n")

    assert target.stat().st_mode & 0o777 == 0o640


def test_atomic_write_new_file_uses_default_mode_minus_umask(tmp_path):
    """A brand-new pricing.yaml (no prior file to inherit a mode from) must
    get the same permissions `open(path, "w")` used to produce: 0o666 masked
    by the process umask — not tempfile.mkstemp()'s restrictive 0600. Reads
    the umask the same non-mutating way the implementation does (no
    os.umask() call here either — that call is itself unsafe to make from a
    process that may have concurrent threads mid-uptime)."""
    import paths
    import pricing_refresh

    target = paths.get_pricing_path()
    assert not target.exists()

    pricing_refresh._atomic_write_text(target, "models: {}\n")

    expected_mode = 0o666 & ~pricing_refresh._read_process_umask()
    assert target.stat().st_mode & 0o777 == expected_mode
    assert target.stat().st_mode & 0o777 == pricing_refresh._DEFAULT_FILE_MODE


def test_read_process_umask_matches_proc_self_status():
    """Sanity check against the raw /proc/self/status field this session
    actually has, so the parser isn't just self-consistent with itself."""
    import re
    from pathlib import Path

    import pricing_refresh

    raw = Path("/proc/self/status").read_text()
    match = re.search(r"^Umask:\s*([0-7]+)\s*$", raw, re.MULTILINE)
    assert match, "test environment has no readable Umask: field to compare against"
    expected = int(match.group(1), 8)

    assert pricing_refresh._read_process_umask() == expected


def test_read_process_umask_falls_back_when_proc_unavailable(monkeypatch):
    """Non-Linux (or a /proc/self/status that can't be opened for any other
    reason) must fall back to the common default umask, never raise."""
    import pricing_refresh

    def _boom(*_args, **_kwargs):
        raise OSError("simulated: /proc/self/status not available")

    monkeypatch.setattr(pricing_refresh, "open", _boom, raising=False)

    assert pricing_refresh._read_process_umask() == 0o022


def test_atomic_write_closes_fd_when_fchmod_fails(monkeypatch):
    """If os.fchmod() raises, the temp file's fd must still be closed, not
    leaked. Moving the fchmod call inside the `with os.fdopen(fd, "w") as f`
    block means the context manager's __exit__ closes it even when fchmod
    itself is what raised inside the block."""
    import os

    import paths
    import pricing_refresh

    target = paths.get_pricing_path()

    captured = {}

    def _spy_then_boom(fd, _mode):
        captured["fd"] = fd
        raise OSError("simulated fchmod failure")

    monkeypatch.setattr(pricing_refresh.os, "fchmod", _spy_then_boom)

    with pytest.raises(OSError):
        pricing_refresh._atomic_write_text(target, "models: {}\n")

    assert "fd" in captured
    with pytest.raises(OSError):
        os.fstat(captured["fd"])  # EBADF if properly closed; succeeds if leaked
