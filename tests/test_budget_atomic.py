"""Tests for budget.yaml atomic writes + failed-read handling (issue #135).

Covers the three writers (``budget set``, ``setup_wizard._write_budget``,
dashboard budget edit) and the reader contract: a malformed budget.yaml must
disable budgets for that call only — never pinned into the cache — and a
missing file must still mean "budgets disabled" (documented behavior).
"""

from __future__ import annotations

import builtins
import os
import sys
import textwrap
from pathlib import Path

import hermes_telemetry.budget as budget
import hermes_telemetry.pricing_refresh as pricing_refresh
import pytest
import yaml


@pytest.fixture(autouse=True)
def isolated():
    """Fresh budget config + cleared caches for every test.

    HERMES_HOME isolation is provided by the project-level autouse fixture in
    conftest.py; this resets the budget module's caches around each test.
    """
    budget.reload_config()
    yield
    budget.reload_config()


def _budget_path(tmp_path: Path) -> Path:
    return tmp_path / "telemetry" / "budget.yaml"


def _write_raw_budget(tmp_path: Path, body: str) -> None:
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    _budget_path(tmp_path).write_text(textwrap.dedent(body))
    budget.reload_config()


VALID_BUDGET = """\
    budgets:
      global:
        daily_usd: 7.50
        monthly_usd: 150.00
    """

GARBAGE = "this: [is: not: valid: yaml\n"


# ---------------------------------------------------------------------------
# Reader: failed reads are never memoized
# ---------------------------------------------------------------------------


def test_missing_budget_means_disabled(tmp_path):
    """Documented behavior: no budget.yaml → budgets disabled."""
    cfg = budget.load_config()
    assert cfg["budgets"] == {}


def test_malformed_budget_disables_for_that_call_only(tmp_path):
    _write_raw_budget(tmp_path, GARBAGE)
    assert budget.load_config()["budgets"] == {}


def test_malformed_budget_is_not_pinned(tmp_path):
    """A failed read must not disable enforcement once the file is fixed —
    without reload_config() or a restart (#135's core defect)."""
    _write_raw_budget(tmp_path, GARBAGE)
    assert budget.load_config()["budgets"] == {}

    _write_raw_budget(tmp_path, VALID_BUDGET)
    cfg = budget.load_config()  # no reload_config() — the retry is automatic
    assert cfg["budgets"]["global"]["daily_usd"] == 7.50


def test_unbroken_failure_streak_does_not_reopen(tmp_path, monkeypatch):
    """While the broken file is unchanged, repeated calls return the default
    without re-opening the file (one stat per call, no parse)."""
    _write_raw_budget(tmp_path, GARBAGE)
    assert budget.load_config()["budgets"] == {}

    budget_file = _budget_path(tmp_path)
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if Path(file) == budget_file:
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    assert budget.load_config()["budgets"] == {}
    assert budget.load_config()["budgets"] == {}
    assert opened == []


def test_failure_streak_logs_warning_once(tmp_path, caplog):
    """One WARNING per failure streak; a real retry that still fails logs
    DEBUG (mirrors the pricing.yaml fix in #134)."""
    _write_raw_budget(tmp_path, GARBAGE)
    with caplog.at_level("DEBUG", logger="hermes_telemetry.budget"):
        budget.load_config()  # first failure → WARNING
        budget.load_config()  # unchanged → short-circuit, no log at all
        # Change the file (still broken) → real retry → DEBUG, not WARNING
        _budget_path(tmp_path).write_text(GARBAGE + "# still broken\n")
        budget.load_config()

    warnings = [r for r in caplog.records if r.levelno == 30]  # logging.WARNING
    debugs = [r for r in caplog.records if r.levelno == 10]  # logging.DEBUG
    assert len(warnings) == 1
    assert any("Failed to load budget config" in r.message for r in debugs)


def test_pre_read_failure_is_recorded_and_load_config_never_raises(tmp_path, monkeypatch):
    """A failure BEFORE the read must be recorded, not escape as UnboundLocalError.

    ``sig`` is captured outside the ``try`` for exactly this case. With it inside, a raising
    ``import yaml`` reached the ``except`` branch with ``sig`` still unbound, so
    ``_load_failure_sig = sig`` raised UnboundLocalError *out of* ``load_config()`` — which
    sits on the ``pre_tool_call`` path and must never raise.
    """
    _write_raw_budget(tmp_path, VALID_BUDGET)

    # ``import yaml`` inside load_config resolves through sys.modules, so None there makes it
    # raise ImportError — the very first statement inside the try fails.
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert budget.load_config()["budgets"] == {}  # the point: no exception

    # The failure was recorded, so the unchanged file is not re-opened (one stat per call).
    budget_file = _budget_path(tmp_path)
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if Path(file) == budget_file:
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    assert budget.load_config()["budgets"] == {}
    assert opened == []


def test_reload_config_clears_failure_streak(tmp_path):
    _write_raw_budget(tmp_path, GARBAGE)
    assert budget.load_config()["budgets"] == {}
    budget.reload_config()  # explicit reload forces a real attempt
    assert budget.load_config()["budgets"] == {}  # still broken, still disabled
    _write_raw_budget(tmp_path, VALID_BUDGET)
    assert budget.load_config()["budgets"]["global"]["daily_usd"] == 7.50


def test_empty_budget_file_is_not_pinned(tmp_path):
    """A 0-byte budget.yaml (a truncate-in-place writer caught mid-write) must
    not be pinned as "budgets disabled" -- a valid replacement is picked up
    with no reload_config() (#146)."""
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    budget_file = _budget_path(tmp_path)
    budget_file.write_text("")  # 0-byte truncation window
    assert budget.load_config()["budgets"] == {}

    budget_file.write_text(textwrap.dedent(VALID_BUDGET))
    cfg = budget.load_config()  # no reload_config() -- the retry is automatic
    assert cfg["budgets"]["global"]["daily_usd"] == 7.50


def test_comments_only_budget_file_is_not_pinned(tmp_path):
    """A non-empty file that parses to None (comments/whitespace only) is just
    as ambiguous as a 0-byte file and must also retry, not pin (#146)."""
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    budget_file = _budget_path(tmp_path)
    budget_file.write_text("# comments only, no budgets\n")
    assert budget.load_config()["budgets"] == {}

    budget_file.write_text(textwrap.dedent(VALID_BUDGET))
    cfg = budget.load_config()
    assert cfg["budgets"]["global"]["daily_usd"] == 7.50


def test_empty_budget_file_streak_does_not_reopen(tmp_path, monkeypatch):
    """While the empty file is unchanged, repeated calls return disabled without
    re-opening (one stat per call), mirroring the malformed-file streak (#146)."""
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    budget_file = _budget_path(tmp_path)
    budget_file.write_text("")
    assert budget.load_config()["budgets"] == {}

    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        if Path(file) == budget_file:
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    assert budget.load_config()["budgets"] == {}
    assert budget.load_config()["budgets"] == {}
    assert opened == []


# ---------------------------------------------------------------------------
# Writers: atomic, unique temp, mode kept, symlink followed
# ---------------------------------------------------------------------------


def test_budget_set_writer_leaves_no_temp_behind(tmp_path):
    out = budget.handle("set global daily 7.50")
    assert "7.50" in out
    tele = tmp_path / "telemetry"
    leftovers = [p.name for p in tele.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert budget.load_config()["budgets"]["global"]["daily_usd"] == 7.50


def test_budget_set_writer_follows_symlink(tmp_path):
    tele = tmp_path / "telemetry"
    tele.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real-budget.yaml"
    real.write_text("budgets: {}\n")
    link = tele / "budget.yaml"
    link.symlink_to(real)

    budget.handle("set global daily 3.25")

    assert link.is_symlink()  # link preserved, not replaced by a regular file
    assert budget.load_config()["budgets"]["global"]["daily_usd"] == 3.25


def test_budget_set_writer_preserves_mode(tmp_path):
    _write_raw_budget(tmp_path, VALID_BUDGET)
    budget_file = _budget_path(tmp_path)
    os.chmod(budget_file, 0o640)

    budget.handle("set global daily 9.00")

    assert budget_file.stat().st_mode & 0o777 == 0o640
    assert budget.load_config()["budgets"]["global"]["daily_usd"] == 9.00


def test_concurrent_writers_never_share_temp(tmp_path):
    """Unique temp names (mkstemp) — the old fixed ``budget.yaml.tmp`` let
    two concurrent writers clobber each other's temp file."""
    import threading

    target = _budget_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(25):
                pricing_refresh._atomic_write_text(target, f"budgets: {{}} # {n}-{i}\n")
        except Exception as e:  # noqa: BLE001 - record, don't raise in thread
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert yaml.safe_load(target.read_text())  # full content, never partial
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_budget_writes_through_the_shared_atomic_helper(monkeypatch, tmp_path):
    """budget.py must not carry its own copy of the primitive (#140 review item 2).

    #134 landed the pricing-side helper and the plugin side now shares it, so there is
    one implementation and one place to fix. The standalone dashboard keeps its own copy
    (it cannot import plugin code) and its parity is asserted separately.
    """
    calls: list[Path] = []
    real = pricing_refresh._atomic_write_text

    def spy(path, content):
        calls.append(Path(path))
        real(path, content)

    monkeypatch.setattr(pricing_refresh, "_atomic_write_text", spy)
    _write_raw_budget(tmp_path, VALID_BUDGET)

    budget._set_budget("global", "daily", 12.5)

    assert calls, "budget did not write through the shared atomic helper"
    assert calls[-1].name == "budget.yaml", calls


def test_atomic_write_text_roundtrip_and_mode(tmp_path):
    target = tmp_path / "sub" / "file.txt"  # parent does not exist yet
    pricing_refresh._atomic_write_text(target, "hello")
    assert target.read_text() == "hello"

    os.chmod(target, 0o640)
    pricing_refresh._atomic_write_text(target, "hello world")
    assert target.read_text() == "hello world"
    assert target.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_atomic_write_text_failure_cleans_up(tmp_path, monkeypatch):
    target = tmp_path / "file.txt"

    def boom(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(budget.os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        pricing_refresh._atomic_write_text(target, "hello")
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    assert not target.exists()


def test_setup_wizard_write_budget_is_atomic(tmp_path):
    from hermes_telemetry import setup_wizard

    data = {
        "budgets": {"global": {"daily_usd": 5.00, "monthly_usd": 100.00}},
        "thresholds": {"soft_pct": 0.80, "hard_pct": 1.00},
        "on_estimated": {"mode": "warn_only"},
    }
    p = setup_wizard._write_budget(data)
    assert p.exists()
    leftovers = [q.name for q in p.parent.iterdir() if q.name.endswith(".tmp")]
    assert leftovers == []
    cfg = budget.load_config()
    assert cfg["budgets"]["global"]["daily_usd"] == 5.00
