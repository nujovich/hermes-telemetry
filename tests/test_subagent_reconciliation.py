"""A1 reconciliation test: subagent tokens counted exactly once.

Simulates the Hermes hook sequence for a parent session that calls delegate_task
to spawn one child agent. Compares tokens in the DB (child's run row) against
the tokens reported inside the delegate_task result JSON.

WHAT IS VERIFIED HERE (simulation):
  - With the plugin loaded in BOTH parent and child, each session records its
    own tokens via on_session_start / post_api_request — no double-count, no gap.
  - post_tool_call for delegate_task adds a tool_calls row but NOT a llm_calls
    row (no proxy cost rows that would inflate spend).
  - The parent run row accumulates only the parent's own API tokens.
  - The global total (parent + child run rows) equals expected tokens.

WHAT IS NOT VERIFIED HERE (live run needed):
  - Whether Hermes actually loads the plugin inside child/subagent processes.
    If the child runs WITHOUT the plugin, db_child_tokens == 0 — silent undercount.
    A live run will show this gap immediately via /stats (child run missing from DB).

LIVE VERIFICATION PROCEDURE (run yourself once you have a Hermes session):
  1. Clear or snapshot the DB: cp ~/.hermes/telemetry/telemetry.db ~/before.db
  2. Run ONE session that calls delegate_task (any minimal task works).
  3. /stats raw 10   — look for two runs: the parent and the child.
     If only ONE run appears → child lacks plugin → undercount confirmed.
     If TWO runs appear → check: parent tokens + child tokens = API bill tokens?
  4. /stats providers  — confirm your provider shows 0% estimated.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the plugin __init__ by exec-ing it into the real hermes_telemetry package
# stub that conftest already set up.  This lets register()'s relative imports
# (from . import db) resolve correctly via hermes_telemetry.__path__.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
import hermes_telemetry as _init_mod

_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_spec.loader.exec_module(_init_mod)


class MockPluginContext:
    """Minimal stand-in for Hermes PluginContext."""

    def __init__(self):
        self.hooks: dict = {}
        self.commands: dict = {}

    def register_hook(self, name: str, fn) -> None:
        self.hooks[name] = fn

    def register_command(self, name: str, fn, description="", args_hint="") -> None:
        self.commands[name] = fn

    def fire(self, hook_name: str, **kwargs):
        fn = self.hooks.get(hook_name)
        if fn:
            return fn(**kwargs)
        return None


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_telemetry.db as db_mod

    db_mod._local.conn = None
    yield
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


# ---------------------------------------------------------------------------
# Helper: delegate_task result shape from tools/delegate_tool.py:2303-2309
# ---------------------------------------------------------------------------


def _delegate_result(
    input_tokens: int, output_tokens: int, model: str, api_calls: int = 1, status: str = "ok"
) -> str:
    """Serialize a delegate_task result the way Hermes does."""
    return json.dumps(
        {
            "results": [
                {
                    "tokens": {"input": input_tokens, "output": output_tokens},
                    "model": model,
                    "api_calls": api_calls,
                    "status": status,
                }
            ]
        }
    )


# ---------------------------------------------------------------------------
# A1 core: tokens counted once, not double, not missed
# ---------------------------------------------------------------------------


def test_subagent_tokens_counted_once():
    """With plugin in both parent and child, tokens appear exactly once in DB.

    Assert: db_child_tokens == result_child_tokens (from delegate_task result).
    Assert: parent run tokens == parent's own API calls only.
    Assert: global total == parent + child tokens.
    Assert: no llm_calls row added by post_tool_call(delegate_task).
    """
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    PARENT = "parent_recon_001"
    CHILD = "child_recon_001"
    MODEL = "claude-sonnet-4-6"
    PROV = "anthropic"

    PARENT_IN = 5_000
    PARENT_OUT = 500
    CHILD_IN = 3_000
    CHILD_OUT = 800

    # --- Parent session starts ---
    ctx.fire("on_session_start", session_id=PARENT, model=MODEL, platform="cli")

    # --- Parent makes one API call (deciding to delegate) ---
    ctx.fire("pre_api_request", session_id=PARENT, api_call_count=0, approx_input_tokens=PARENT_IN)
    ctx.fire(
        "post_api_request",
        session_id=PARENT,
        model=MODEL,
        provider=PROV,
        api_duration=1.5,
        api_call_count=0,
        assistant_content_chars=PARENT_OUT * 4,
        usage={
            "input_tokens": PARENT_IN,
            "output_tokens": PARENT_OUT,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
    )

    # --- Child session runs its own hooks (plugin loaded in child too) ---
    ctx.fire("on_session_start", session_id=CHILD, model=MODEL, platform="cli")
    ctx.fire("pre_api_request", session_id=CHILD, api_call_count=0, approx_input_tokens=CHILD_IN)
    ctx.fire(
        "post_api_request",
        session_id=CHILD,
        model=MODEL,
        provider=PROV,
        api_duration=0.8,
        api_call_count=0,
        assistant_content_chars=CHILD_OUT * 4,
        usage={
            "input_tokens": CHILD_IN,
            "output_tokens": CHILD_OUT,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
    )
    ctx.fire("on_session_end", session_id=CHILD, completed=True, interrupted=False)

    # Snapshot llm_calls count BEFORE post_tool_call(delegate_task)
    conn = db._get_conn()
    llm_count_before = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]

    # --- Parent receives delegate_task result (exact shape from delegate_tool.py:2303-2309) ---
    result_json = _delegate_result(CHILD_IN, CHILD_OUT, MODEL)
    ctx.fire(
        "post_tool_call",
        tool_name="delegate_task",
        result=result_json,
        duration_ms=2_500,
        session_id=PARENT,
    )

    # --- subagent_stop fires on parent ---
    ctx.fire(
        "subagent_stop",
        parent_session_id=PARENT,
        child_role="assistant",
        child_status="ok",
        duration_ms=2_500,
    )

    # --- Parent ends ---
    ctx.fire("on_session_end", session_id=PARENT, completed=True, interrupted=False)

    # -----------------------------------------------------------------------
    # Assert A: post_tool_call for delegate_task must not add llm_calls rows
    # -----------------------------------------------------------------------
    llm_count_after = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    assert llm_count_after == llm_count_before, (
        f"post_tool_call(delegate_task) added {llm_count_after - llm_count_before} "
        "extra llm_calls row(s) — no proxy cost rows should be created."
    )

    # -----------------------------------------------------------------------
    # Assert B: child DB tokens == result tokens (the key reconciliation check)
    # -----------------------------------------------------------------------
    child_run = db.get_run(CHILD)
    assert child_run is not None, (
        "Child session not found in DB. In a live run this means the child "
        "process did not have the plugin loaded — tokens are undercounted."
    )
    db_child_tokens = child_run["tokens_in"] + child_run["tokens_out"]
    result_child_tokens = sum(
        r["tokens"]["input"] + r["tokens"]["output"] for r in json.loads(result_json)["results"]
    )
    assert db_child_tokens == result_child_tokens, (
        f"Token mismatch: DB child={db_child_tokens}, "
        f"delegate_task result={result_child_tokens}. "
        "Either double-counted or undercounted."
    )

    # -----------------------------------------------------------------------
    # Assert C: parent run tokens are ONLY the parent's own API calls
    # -----------------------------------------------------------------------
    parent_run = db.get_run(PARENT)
    assert parent_run["tokens_in"] == PARENT_IN, (
        f"Parent tokens_in={parent_run['tokens_in']}, expected {PARENT_IN}. "
        "Child tokens must not leak into parent run."
    )
    assert parent_run["tokens_out"] == PARENT_OUT

    # -----------------------------------------------------------------------
    # Assert D: global total = parent + child (nothing lost)
    # -----------------------------------------------------------------------
    db_total = (
        parent_run["tokens_in"]
        + parent_run["tokens_out"]
        + child_run["tokens_in"]
        + child_run["tokens_out"]
    )
    expected_total = PARENT_IN + PARENT_OUT + CHILD_IN + CHILD_OUT
    assert db_total == expected_total, (
        f"Global token total {db_total} != expected {expected_total}."
    )


def test_subagent_with_multiple_child_api_calls():
    """Child makes 2 API calls; tokens from both accumulate into the child run row."""
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    PARENT = "parent_multi_001"
    CHILD = "child_multi_001"
    MODEL = "claude-sonnet-4-6"
    PROV = "anthropic"

    CHILD_IN_1, CHILD_OUT_1 = 2_000, 300
    CHILD_IN_2, CHILD_OUT_2 = 2_500, 400

    ctx.fire("on_session_start", session_id=PARENT, model=MODEL, platform="cli")
    ctx.fire("on_session_start", session_id=CHILD, model=MODEL, platform="cli")

    # Child makes 2 API calls
    for i, (tin, tout) in enumerate([(CHILD_IN_1, CHILD_OUT_1), (CHILD_IN_2, CHILD_OUT_2)]):
        ctx.fire("pre_api_request", session_id=CHILD, api_call_count=i, approx_input_tokens=tin)
        ctx.fire(
            "post_api_request",
            session_id=CHILD,
            model=MODEL,
            provider=PROV,
            api_duration=0.5,
            api_call_count=i,
            assistant_content_chars=tout * 4,
            usage={
                "input_tokens": tin,
                "output_tokens": tout,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
    ctx.fire("on_session_end", session_id=CHILD, completed=True, interrupted=False)

    # delegate_task result reports the sum of all child calls
    result_json = _delegate_result(
        CHILD_IN_1 + CHILD_IN_2, CHILD_OUT_1 + CHILD_OUT_2, MODEL, api_calls=2
    )
    ctx.fire(
        "post_tool_call",
        tool_name="delegate_task",
        result=result_json,
        duration_ms=3_000,
        session_id=PARENT,
    )
    ctx.fire("on_session_end", session_id=PARENT, completed=True, interrupted=False)

    child_run = db.get_run(CHILD)
    db_child_tokens = child_run["tokens_in"] + child_run["tokens_out"]
    result_child_tokens = sum(
        r["tokens"]["input"] + r["tokens"]["output"] for r in json.loads(result_json)["results"]
    )
    assert db_child_tokens == result_child_tokens
    assert child_run["api_calls"] == 2


def test_subagent_tool_call_recorded_on_parent():
    """delegate_task post_tool_call increments parent's tool_calls counter."""
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    PARENT = "parent_tool_cnt_001"
    ctx.fire("on_session_start", session_id=PARENT, model="m", platform="cli")

    result_json = _delegate_result(1000, 200, "m")
    ctx.fire(
        "post_tool_call",
        tool_name="delegate_task",
        result=result_json,
        duration_ms=1_000,
        session_id=PARENT,
    )
    ctx.fire("on_session_end", session_id=PARENT, completed=True, interrupted=False)

    parent_run = db.get_run(PARENT)
    assert parent_run["tool_calls"] == 1
    # Confirm no llm_calls rows were added for the parent via delegate_task
    conn = db._get_conn()
    llm_count = conn.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE session_id = ?", (PARENT,)
    ).fetchone()[0]
    assert llm_count == 0


def test_subagent_cron_parent_costs_exclude_child():
    """Per-cron-job budget scope counts only the parent's own tokens (not child's).

    This is a documented limitation: the child runs under its own session_id with
    no cron_job_id, so per-cron-job cost excludes delegated spend. Global scope
    correctly includes both.
    """
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    PARENT_SID = "cron_nightly_20260601_020000"
    CHILD_SID = "child_cron_recon_001"
    MODEL = "claude-sonnet-4-6"
    PROV = "anthropic"

    # Parent is a cron job
    ctx.fire("on_session_start", session_id=PARENT_SID, model=MODEL, platform="cron")
    ctx.fire("pre_api_request", session_id=PARENT_SID, api_call_count=0, approx_input_tokens=1_000)
    ctx.fire(
        "post_api_request",
        session_id=PARENT_SID,
        model=MODEL,
        provider=PROV,
        api_duration=0.5,
        api_call_count=0,
        assistant_content_chars=200,
        usage={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
    )

    # Child has its own session (no cron_job_id — standard child run format)
    ctx.fire("on_session_start", session_id=CHILD_SID, model=MODEL, platform="cli")
    ctx.fire("pre_api_request", session_id=CHILD_SID, api_call_count=0, approx_input_tokens=5_000)
    ctx.fire(
        "post_api_request",
        session_id=CHILD_SID,
        model=MODEL,
        provider=PROV,
        api_duration=1.0,
        api_call_count=0,
        assistant_content_chars=1_000,
        usage={
            "input_tokens": 5_000,
            "output_tokens": 500,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
    )
    ctx.fire("on_session_end", session_id=CHILD_SID, completed=True, interrupted=False)

    result_json = _delegate_result(5_000, 500, MODEL)
    ctx.fire(
        "post_tool_call",
        tool_name="delegate_task",
        result=result_json,
        duration_ms=2_000,
        session_id=PARENT_SID,
    )
    ctx.fire("on_session_end", session_id=PARENT_SID, completed=True, interrupted=False)

    parent_run = db.get_run(PARENT_SID)
    child_run = db.get_run(CHILD_SID)

    # The cron job row carries only the parent's tokens
    assert parent_run["cron_job_id"] == "nightly"
    assert child_run["cron_job_id"] is None

    # Global spend includes both
    past = "2000-01-01T00:00:00+00:00"
    g = db.spend_by_scope("global", "", past)
    assert g["total_calls"] == 2  # 2 llm_calls rows: one parent, one child

    # Per-cron-job scope sees only the parent's llm_call (not the child's)
    j = db.spend_by_scope("cron_job", "nightly", past)
    assert j["total_calls"] == 1
