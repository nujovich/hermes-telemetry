# Attribute async subagent cost to `per_cron_job` budgets — Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute async and nested `delegate_task` subagent cost to the parent cron job's `per_cron_job` budget, by persisting the parent→child delegation tree and resolving it to the root cron job at query time.

**Architecture:** A new persistent `subagent_edges` table (migration v11) records each parent→child edge, keyed by `child_session_id` (the only correlation key present in both `subagent_start` and `subagent_stop`). The hot path only INSERTs edges; `db.spend_by_scope("cron_job", …)` re-groups the existing per-session cost rows through a recursive CTE that seeds from cron root sessions and walks the edge tree down. `global` is unchanged, so the same cost rows are regrouped — no double counting. A query-time `unattributed_child_cost` diagnostic (no stored state) flags edges whose parent was never recorded.

**Tech Stack:** Python 3, SQLite (per-thread connections + WAL, `db.py`), pytest, ruff. Hooks registered in `__init__.py`.

**Base branch:** `feat/attribute-subagent-cron-budget` (branched from `feat/moa-integration`, schema at v10). This adds v11.

**Scope note:** Phase 1 is aggregation/reporting correctness only. In-path child-side enforcement (pausing a runaway async child mid-flight) is **out of scope** — see the design doc §7 and §11 (`docs/superpowers/specs/2026-07-03-attribute-subagent-cron-budget-design.md`). Because the edge table is persistent (not an in-memory map), there is no leak to clean up on session end — the issue's "cleanup on parent session end" criterion is moot by construction.

**Source-verified facts driving this plan** (NousResearch/hermes-agent@main):
- `subagent_start` (`tools/delegate_tool.py:1383`) kwargs: `parent_session_id`, `parent_turn_id`, `parent_subagent_id`, `child_session_id`, `child_subagent_id`, `child_role`, `child_goal`. Fires synchronously in `_build_child_agent`, before async dispatch.
- `subagent_stop` (`tools/delegate_tool.py:2726`) kwargs: `parent_session_id`, `parent_turn_id`, `child_session_id`, `child_role`, `child_summary`, `child_status`, `duration_ms`. **Carries `child_session_id`** (the in-repo ONBOARDING claim that it does not is stale) but **not `child_subagent_id`**.
- `post_api_request` / `post_tool_call` carry only the executing (child) `session_id`, never `parent_session_id`.
- Subagents run as threads (`DaemonThreadPoolExecutor`) in the same process, so the single registered plugin's hooks fire for every child; `db.py`'s per-thread connections + WAL handle concurrent writes.

---

## File Structure

- **Modify** `db.py` — add `_migrate_v11`, bump `_SCHEMA_VERSION`, append the migration call; add `record_subagent_start`, `record_subagent_stop`, `unattributed_child_cost`; rework the `cron_job` branch of `spend_by_scope`.
- **Modify** `__init__.py` — register a new `subagent_start` handler; extend the `subagent_stop` handler to finalize the edge (keep the existing proxy `tool_calls` row).
- **Modify** `budget.py` — update the `_cron_block` attribution note.
- **Modify** `stats.py` — add a delegation footer to `_cron_block`.
- **Modify** `ONBOARDING.md` — correct the stale `subagent_stop` claim, add the v11 row, update Subagent Architecture + Known Limitations + hook kwargs reference.
- **Test** `tests/test_db.py` — migration v11, edge write API, `unattributed_child_cost`, `spend_by_scope` attribution.
- **Test** `tests/test_subagent_reconciliation.py` — hooks record/finalize edges; flip the exclusion test to inclusion; nested→root; unlinked child excluded.
- **Test** `tests/test_budget.py` — updated attribution note.

---

## Task 1: Migration v11 — `subagent_edges` table

**Files:**
- Modify: `db.py:42` (`_SCHEMA_VERSION`), `db.py:169` (append call), after `db.py:421` (new `_migrate_v11`)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py` (module already imports `db` and `_SCHEMA_VERSION`):

```python
# ---------------------------------------------------------------------------
# v11 — subagent_edges (delegation tree for per_cron_job attribution, #49)
# ---------------------------------------------------------------------------


def test_schema_v11_subagent_edges_columns():
    """v11 creates the subagent_edges table with the delegation-tree columns."""
    conn = db._get_conn()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_edges" in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subagent_edges)")}
    assert cols == {
        "child_session_id",
        "parent_session_id",
        "parent_turn_id",
        "parent_subagent_id",
        "child_subagent_id",
        "child_role",
        "started_at",
        "stopped_at",
        "child_status",
    }


def test_schema_v11_recorded():
    versions = {r[0] for r in db._get_conn().execute("SELECT version FROM schema_version")}
    assert 11 in versions


def test_migrate_v11_creates_table_from_wedged_v10():
    """Upgrade path: a DB stuck in the pre-v11 shape (no subagent_edges, v11
    marker absent) self-heals on the next connect and the edge write works."""
    conn = db._get_conn()
    conn.execute("DROP TABLE IF EXISTS subagent_edges")
    conn.execute("DELETE FROM schema_version WHERE version >= 11")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_edges" not in tables  # wedged state confirmed

    db._ensure_schema(conn)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_edges" in tables
    db.record_subagent_start(
        child_session_id="c-heal",
        parent_session_id="p-heal",
        started_at="2026-07-03T00:00:00+00:00",
    )
    row = conn.execute(
        "SELECT parent_session_id FROM subagent_edges WHERE child_session_id='c-heal'"
    ).fetchone()
    assert row["parent_session_id"] == "p-heal"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py::test_schema_v11_subagent_edges_columns tests/test_db.py::test_schema_v11_recorded -v`
Expected: FAIL — `subagent_edges` table absent; `11` not in `schema_version`. (`test_schema_idempotent` will also fail once `_SCHEMA_VERSION` is bumped in step 3 but the migration isn't yet appended — expected mid-task.)

- [ ] **Step 3: Implement the migration**

In `db.py`, bump the version (line 42):

```python
_SCHEMA_VERSION = 11
```

Append the call in `_ensure_schema`, immediately after `_migrate_v10(conn)` (line 169):

```python
    _migrate_v10(conn)
    _migrate_v11(conn)
```

Add the migration function immediately after `_migrate_v10` (after line 421):

```python
def _migrate_v11(conn: sqlite3.Connection) -> None:
    """Add v11 schema: subagent_edges — the persistent parent→child delegation
    tree used to attribute async/nested subagent cost to per_cron_job budgets
    (issue #49).

    child_session_id is the correlation key present in BOTH subagent_start and
    subagent_stop (subagent_stop carries no child_subagent_id). No cost is stored
    here; cost is resolved to the cron root at query time via a recursive CTE in
    spend_by_scope, so there is no double counting against the global tally.
    """
    cur = conn.execute("SELECT version FROM schema_version WHERE version = 11")
    if cur.fetchone() is not None:
        return

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subagent_edges (
            child_session_id   TEXT PRIMARY KEY,
            parent_session_id  TEXT NOT NULL,
            parent_turn_id     TEXT,
            parent_subagent_id TEXT,
            child_subagent_id  TEXT,
            child_role         TEXT,
            started_at         TEXT NOT NULL,
            stopped_at         TEXT,
            child_status       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_subagent_edges_parent
            ON subagent_edges(parent_session_id);
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (11, ?)",
        (_utcnow(),),
    )
```

(`record_subagent_start` lands in Task 2; `test_migrate_v11_creates_table_from_wedged_v10` stays red until then — run the two schema tests now, the wedged test after Task 2.)

- [ ] **Step 4: Run tests to verify schema tests pass**

Run: `pytest tests/test_db.py::test_schema_v11_subagent_edges_columns tests/test_db.py::test_schema_v11_recorded tests/test_db.py::test_schema_idempotent -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): add subagent_edges table (migration v11) for #49"
```

---

## Task 2: Edge write API in `db.py`

**Files:**
- Modify: `db.py` (add three functions in the Write API section, after `record_tool_call`)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
def test_record_subagent_start_inserts_edge():
    db.record_subagent_start(
        child_session_id="c1",
        parent_session_id="p1",
        parent_turn_id="t1",
        parent_subagent_id="sa-0-aaa",
        child_subagent_id="sa-1-bbb",
        child_role="researcher",
        started_at="2026-07-03T00:00:00+00:00",
    )
    conn = db._get_conn()
    row = conn.execute("SELECT * FROM subagent_edges WHERE child_session_id='c1'").fetchone()
    assert row["parent_session_id"] == "p1"
    assert row["child_subagent_id"] == "sa-1-bbb"
    assert row["child_role"] == "researcher"
    assert row["stopped_at"] is None
    assert row["child_status"] is None


def test_record_subagent_start_idempotent():
    """First edge wins (INSERT OR IGNORE) — a duplicate start does not clobber."""
    db.record_subagent_start(child_session_id="c1", parent_session_id="p1")
    db.record_subagent_start(child_session_id="c1", parent_session_id="p_other")
    conn = db._get_conn()
    rows = conn.execute(
        "SELECT parent_session_id FROM subagent_edges WHERE child_session_id='c1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["parent_session_id"] == "p1"


def test_record_subagent_stop_finalizes_edge():
    db.record_subagent_start(
        child_session_id="c1", parent_session_id="p1", started_at="2026-07-03T00:00:00+00:00"
    )
    db.record_subagent_stop(
        child_session_id="c1",
        parent_session_id="p1",
        child_status="completed",
        stopped_at="2026-07-03T00:05:00+00:00",
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT stopped_at, child_status FROM subagent_edges WHERE child_session_id='c1'"
    ).fetchone()
    assert row["stopped_at"] == "2026-07-03T00:05:00+00:00"
    assert row["child_status"] == "completed"


def test_record_subagent_stop_backfills_when_start_missed():
    """If subagent_start was never seen, stop backfills the edge so the child
    still resolves to its parent. child_subagent_id is NULL (absent on stop)."""
    db.record_subagent_stop(
        child_session_id="c-orphan",
        parent_session_id="p1",
        child_status="completed",
        stopped_at="2026-07-03T00:05:00+00:00",
    )
    conn = db._get_conn()
    row = conn.execute(
        "SELECT parent_session_id, child_status, child_subagent_id "
        "FROM subagent_edges WHERE child_session_id='c-orphan'"
    ).fetchone()
    assert row is not None
    assert row["parent_session_id"] == "p1"
    assert row["child_status"] == "completed"
    assert row["child_subagent_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -k subagent_start_inserts or subagent_start_idempotent or subagent_stop_finalizes or subagent_stop_backfills -v`
Expected: FAIL with `AttributeError: module 'hermes_telemetry.db' has no attribute 'record_subagent_start'`.

- [ ] **Step 3: Implement the write API**

In `db.py`, add after `record_tool_call` (Write API section):

```python
def record_subagent_start(
    child_session_id: str,
    parent_session_id: str,
    parent_turn_id: str | None = None,
    parent_subagent_id: str | None = None,
    child_subagent_id: str | None = None,
    child_role: str | None = None,
    started_at: str | None = None,
) -> None:
    """Record a parent→child delegation edge. Idempotent on child_session_id.

    Fires from the subagent_start hook, which runs synchronously in Hermes'
    _build_child_agent BEFORE async dispatch — so the edge exists before the
    child's first post_api_request and resolution never races the child's events.
    """
    _get_conn().execute(
        """
        INSERT OR IGNORE INTO subagent_edges
            (child_session_id, parent_session_id, parent_turn_id,
             parent_subagent_id, child_subagent_id, child_role, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            child_session_id,
            parent_session_id,
            parent_turn_id,
            parent_subagent_id,
            child_subagent_id,
            child_role,
            started_at or _utcnow(),
        ),
    )


def record_subagent_stop(
    child_session_id: str,
    parent_session_id: str = "",
    child_status: str | None = None,
    child_role: str | None = None,
    stopped_at: str | None = None,
) -> None:
    """Finalize a delegation edge: set stopped_at + child_status.

    If the edge is missing (subagent_start not seen — rare, since start fires
    synchronously before dispatch), backfill it from the stop kwargs so the
    child's cost still resolves to its parent. child_subagent_id is absent on the
    stop hook, so a backfilled edge has no subagent id.
    """
    conn = _get_conn()
    now = stopped_at or _utcnow()
    cur = conn.execute(
        """
        UPDATE subagent_edges
        SET stopped_at   = ?,
            child_status = ?,
            child_role   = COALESCE(child_role, ?)
        WHERE child_session_id = ?
        """,
        (now, child_status, child_role, child_session_id),
    )
    if cur.rowcount == 0 and parent_session_id:
        conn.execute(
            """
            INSERT OR IGNORE INTO subagent_edges
                (child_session_id, parent_session_id, child_role,
                 started_at, stopped_at, child_status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (child_session_id, parent_session_id, child_role, now, now, child_status),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -k "subagent_start or subagent_stop or migrate_v11" -v`
Expected: PASS (includes `test_migrate_v11_creates_table_from_wedged_v10` from Task 1, now green).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): record_subagent_start/stop edge write API for #49"
```

---

## Task 3: Register `subagent_start` + extend `subagent_stop` hooks

**Files:**
- Modify: `__init__.py:500-519` (extend `subagent_stop`; add `subagent_start` + registration)
- Test: `tests/test_subagent_reconciliation.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_reconciliation.py`:

```python
def test_subagent_start_hook_records_edge():
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    ctx.fire(
        "subagent_start",
        parent_session_id="cron_nightly_20260601_020000",
        parent_turn_id="turn-1",
        parent_subagent_id="",
        child_session_id="child_edge_001",
        child_subagent_id="sa-0-abcd1234",
        child_role="researcher",
        child_goal="find things",
    )

    conn = db._get_conn()
    row = conn.execute(
        "SELECT parent_session_id, child_subagent_id, child_role, stopped_at "
        "FROM subagent_edges WHERE child_session_id='child_edge_001'"
    ).fetchone()
    assert row["parent_session_id"] == "cron_nightly_20260601_020000"
    assert row["child_subagent_id"] == "sa-0-abcd1234"
    assert row["child_role"] == "researcher"
    assert row["stopped_at"] is None


def test_subagent_stop_hook_finalizes_edge_and_keeps_proxy_row():
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    ctx.fire(
        "subagent_start",
        parent_session_id="p1",
        child_session_id="child_stop_001",
        child_subagent_id="sa-0-x",
    )
    ctx.fire(
        "subagent_stop",
        parent_session_id="p1",
        child_session_id="child_stop_001",
        child_role="researcher",
        child_status="completed",
        duration_ms=500,
    )

    conn = db._get_conn()
    edge = conn.execute(
        "SELECT stopped_at, child_status FROM subagent_edges WHERE child_session_id='child_stop_001'"
    ).fetchone()
    assert edge["stopped_at"] is not None
    assert edge["child_status"] == "completed"
    # Existing behavior preserved: proxy tool_calls row on the parent.
    tc = conn.execute(
        "SELECT ok FROM tool_calls WHERE session_id='p1' AND tool_name='delegate_task/subagent'"
    ).fetchone()
    assert tc is not None
    assert tc[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_subagent_reconciliation.py -k "start_hook_records_edge or stop_hook_finalizes" -v`
Expected: FAIL — `subagent_start` hook not registered (no edge row); `subagent_stop` does not read `child_session_id`, so no edge is finalized.

- [ ] **Step 3: Implement the hooks**

In `__init__.py`, replace the `subagent_stop` block (lines 495-519) with a new `subagent_start` handler followed by the extended `subagent_stop`:

```python
    # ------------------------------------------------------------------
    # subagent_start
    # Fired synchronously in Hermes' _build_child_agent BEFORE async
    # dispatch, so the parent→child edge is persisted before the child's
    # first post_api_request. Cost is attributed to the cron root at query
    # time (db.spend_by_scope), so this handler only records the raw edge.
    # kwargs: parent_session_id, parent_turn_id, parent_subagent_id,
    #         child_session_id, child_subagent_id, child_role, child_goal
    # ------------------------------------------------------------------
    def subagent_start(
        parent_session_id: str = "",
        parent_turn_id: str = "",
        parent_subagent_id: str = "",
        child_session_id: str = "",
        child_subagent_id: str = "",
        child_role: str = "",
        **_kw,
    ) -> None:
        try:
            if not child_session_id or not parent_session_id:
                return
            db.record_subagent_start(
                child_session_id=child_session_id,
                parent_session_id=parent_session_id,
                parent_turn_id=parent_turn_id or None,
                parent_subagent_id=parent_subagent_id or None,
                child_subagent_id=child_subagent_id or None,
                child_role=child_role or None,
                started_at=_utcnow(),
            )
        except Exception as exc:
            tele_log.error("subagent_start hook failed: %s", exc)

    ctx.register_hook("subagent_start", subagent_start)

    # ------------------------------------------------------------------
    # subagent_stop
    # Fired when a delegated subagent finishes. No token data here, but it
    # DOES carry child_session_id (verified against delegate_tool.py — the
    # in-repo doc claim that it does not is stale). We (1) log the proxy
    # tool_call row so subagent invocations stay visible in /stats, and
    # (2) finalize the delegation edge (stopped_at + child_status).
    # kwargs: parent_session_id, parent_turn_id, child_session_id,
    #         child_role, child_summary, child_status, duration_ms
    # ------------------------------------------------------------------
    def subagent_stop(
        parent_session_id: str = "",
        child_session_id: str = "",
        child_role: str = "",
        child_status: str = "",
        duration_ms: int = 0,
        **_kw,
    ) -> None:
        try:
            ok = child_status not in ("failed", "error", "interrupted", "timeout")
            db.record_tool_call(
                session_id=parent_session_id,
                ts=_utcnow(),
                tool_name="delegate_task/subagent",
                ok=ok,
                latency_ms=duration_ms,
            )
            if child_session_id:
                db.record_subagent_stop(
                    child_session_id=child_session_id,
                    parent_session_id=parent_session_id,
                    child_status=child_status or None,
                    child_role=child_role or None,
                    stopped_at=_utcnow(),
                )
        except Exception as exc:
            tele_log.error("subagent_stop hook failed: %s", exc)

    ctx.register_hook("subagent_stop", subagent_stop)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_subagent_reconciliation.py -k "start_hook or stop_hook or stop_success or stop_failure" -v`
Expected: PASS — new edge tests green; existing `test_subagent_stop_success_statuses` / `_failure_statuses` still green (proxy row unchanged).

- [ ] **Step 5: Commit**

```bash
git add __init__.py tests/test_subagent_reconciliation.py
git commit -m "feat(hooks): record delegation edges in subagent_start/stop for #49"
```

---

## Task 4: Recursive-CTE attribution in `spend_by_scope("cron_job", …)`

**Files:**
- Modify: `db.py:822-859` (`spend_by_scope`)
- Test: `tests/test_subagent_reconciliation.py` (flip + nested + unlinked)

- [ ] **Step 1: Write / rewrite the failing tests**

In `tests/test_subagent_reconciliation.py`, **replace** `test_subagent_cron_parent_costs_exclude_child` (lines 344-426) with the two tests below, and add the nested test:

```python
def test_subagent_cron_parent_costs_include_linked_child():
    """With a subagent_start edge, per-cron-job spend INCLUDES the child's cost
    (issue #49). Global still counts each cost row exactly once."""
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    PARENT_SID = "cron_nightly_20260601_020000"
    CHILD_SID = "child_cron_recon_001"
    MODEL, PROV = "claude-sonnet-4-6", "anthropic"

    ctx.fire("on_session_start", session_id=PARENT_SID, model=MODEL, platform="cron")
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

    # Delegation edge established synchronously before the child runs.
    ctx.fire(
        "subagent_start",
        parent_session_id=PARENT_SID,
        child_session_id=CHILD_SID,
        child_subagent_id="sa-0-aaaa",
        child_role="worker",
    )

    ctx.fire("on_session_start", session_id=CHILD_SID, model=MODEL, platform="cli")
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
    ctx.fire(
        "subagent_stop",
        parent_session_id=PARENT_SID,
        child_session_id=CHILD_SID,
        child_status="completed",
        duration_ms=2_000,
    )
    ctx.fire("on_session_end", session_id=CHILD_SID, completed=True, interrupted=False)
    ctx.fire("on_session_end", session_id=PARENT_SID, completed=True, interrupted=False)

    past = "2000-01-01T00:00:00+00:00"
    g = db.spend_by_scope("global", "", past)
    j = db.spend_by_scope("cron_job", "nightly", past)

    assert g["total_calls"] == 2
    assert j["total_calls"] == 2  # parent + child now attributed to the cron job
    # Both runs live under the one cron root, so the subtree total == global total.
    assert j["spent_usd"] == pytest.approx(g["spent_usd"])
    assert j["spent_usd"] > 0.0


def test_unlinked_child_excluded_from_cron():
    """Without a subagent_start edge, the child is NOT attributed to the cron
    job — its cost appears only in global. Documents the attribution boundary."""
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    PARENT_SID = "cron_nightly_20260601_020000"
    CHILD_SID = "child_cron_unlinked_001"
    MODEL, PROV = "claude-sonnet-4-6", "anthropic"

    ctx.fire("on_session_start", session_id=PARENT_SID, model=MODEL, platform="cron")
    ctx.fire(
        "post_api_request",
        session_id=PARENT_SID,
        model=MODEL,
        provider=PROV,
        api_duration=0.5,
        api_call_count=0,
        assistant_content_chars=200,
        usage={"input_tokens": 1_000, "output_tokens": 100, "cache_read_tokens": 0,
               "cache_write_tokens": 0, "reasoning_tokens": 0},
    )
    # Child with NO subagent_start edge.
    ctx.fire("on_session_start", session_id=CHILD_SID, model=MODEL, platform="cli")
    ctx.fire(
        "post_api_request",
        session_id=CHILD_SID,
        model=MODEL,
        provider=PROV,
        api_duration=1.0,
        api_call_count=0,
        assistant_content_chars=1_000,
        usage={"input_tokens": 5_000, "output_tokens": 500, "cache_read_tokens": 0,
               "cache_write_tokens": 0, "reasoning_tokens": 0},
    )

    past = "2000-01-01T00:00:00+00:00"
    assert db.spend_by_scope("global", "", past)["total_calls"] == 2
    assert db.spend_by_scope("cron_job", "nightly", past)["total_calls"] == 1


def test_nested_subagent_resolves_to_cron_root():
    """Grandchild cost rolls up to the root cron job through two edges."""
    import hermes_telemetry.db as db

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    ROOT = "cron_nightly_20260601_020000"
    CHILD = "child_lvl1_001"
    GRAND = "child_lvl2_001"
    MODEL, PROV = "claude-sonnet-4-6", "anthropic"

    def _api(sid, tin, tout):
        ctx.fire("on_session_start", session_id=sid, model=MODEL,
                 platform="cron" if sid == ROOT else "cli")
        ctx.fire(
            "post_api_request",
            session_id=sid,
            model=MODEL,
            provider=PROV,
            api_duration=0.5,
            api_call_count=0,
            assistant_content_chars=100,
            usage={"input_tokens": tin, "output_tokens": tout, "cache_read_tokens": 0,
                   "cache_write_tokens": 0, "reasoning_tokens": 0},
        )

    _api(ROOT, 1_000, 100)
    ctx.fire("subagent_start", parent_session_id=ROOT, child_session_id=CHILD)
    _api(CHILD, 2_000, 200)
    ctx.fire("subagent_start", parent_session_id=CHILD, child_session_id=GRAND)
    _api(GRAND, 3_000, 300)

    past = "2000-01-01T00:00:00+00:00"
    j = db.spend_by_scope("cron_job", "nightly", past)
    assert j["total_calls"] == 3  # root + child + grandchild
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_subagent_reconciliation.py -k "include_linked_child or nested_subagent" -v`
Expected: FAIL — `cron_job` scope currently filters `cron_job_id = ?` only, so `total_calls == 1` for the linked test and `1` for nested. (`test_unlinked_child_excluded_from_cron` should already PASS — it documents current behavior.)

- [ ] **Step 3: Implement the recursive CTE**

In `db.py`, replace the body of `spend_by_scope` (lines 829-849) so the `cron_job` scope resolves the delegation subtree:

```python
    conn = _get_conn()
    if scope == "cron_job":
        # Attribute the whole delegation subtree to the cron job: seed from the
        # cron root session(s), walk down subagent_edges to every descendant, and
        # sum cost over root + descendants. This pulls in async and nested
        # subagent spend that lands under child session_ids with cron_job_id NULL.
        # The "global" branch below sums ALL runs, so the same cost rows are only
        # regrouped here — no double counting. UNION (not UNION ALL) guards cycles.
        row = conn.execute(
            """
            WITH RECURSIVE tree(session_id) AS (
                SELECT session_id FROM runs WHERE cron_job_id = ?
                UNION
                SELECT e.child_session_id
                FROM subagent_edges e
                JOIN tree t ON e.parent_session_id = t.session_id
            )
            SELECT COALESCE(SUM(r.cost_usd), 0.0)          AS spent_usd,
                   COALESCE(SUM(r.estimated_llm_calls), 0) AS estimated_calls,
                   COALESCE(SUM(r.api_calls), 0)           AS total_calls
            FROM runs r
            JOIN tree ON r.session_id = tree.session_id
            WHERE r.started_at >= ?
            """,
            (scope_id, since_iso),
        ).fetchone()
    else:
        where = ["started_at >= ?"]
        params: list[Any] = [since_iso]
        if scope == "sender":
            where.append("sender_id = ?")
            params.append(scope_id)
        # "global": no extra filter
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(cost_usd), 0.0)            AS spent_usd,
                   COALESCE(SUM(estimated_llm_calls), 0)   AS estimated_calls,
                   COALESCE(SUM(api_calls), 0)             AS total_calls
            FROM runs
            WHERE {" AND ".join(where)}
            """,
            params,
        ).fetchone()
```

(Leave the `spent = float(...)` return block below unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_subagent_reconciliation.py tests/test_budget.py -v`
Expected: PASS — attribution tests green; `test_budget.py` still green (global/sender scopes unchanged; the caveat-text test is updated in Task 6).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_subagent_reconciliation.py
git commit -m "feat(db): resolve subagent subtree to cron root in spend_by_scope (#49)"
```

---

## Task 5: `unattributed_child_cost` query-time diagnostic

**Files:**
- Modify: `db.py` (add read function near `spend_by_scope`)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_db.py`:

```python
def test_unattributed_child_cost_flags_missing_parent():
    """A child edge whose parent has no runs row surfaces as unattributed."""
    db.start_run("orphan-child", model="m", platform="cli")
    db.record_llm_call("orphan-child", db._utcnow(), "m", "p", 100, 50, 0.02, 100)
    db.record_subagent_start(child_session_id="orphan-child", parent_session_id="ghost-parent")

    d = db.unattributed_child_cost("2000-01-01T00:00:00+00:00")
    assert d["edges"] == 1
    assert d["unattributed_usd"] == pytest.approx(0.02)


def test_unattributed_child_cost_zero_when_parent_present():
    db.start_run("real-parent", model="m", platform="cli")
    db.start_run("linked-child", model="m", platform="cli")
    db.record_llm_call("linked-child", db._utcnow(), "m", "p", 100, 50, 0.02, 100)
    db.record_subagent_start(child_session_id="linked-child", parent_session_id="real-parent")

    d = db.unattributed_child_cost("2000-01-01T00:00:00+00:00")
    assert d["edges"] == 0
    assert d["unattributed_usd"] == pytest.approx(0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -k unattributed_child_cost -v`
Expected: FAIL with `AttributeError: ... has no attribute 'unattributed_child_cost'`.

- [ ] **Step 3: Implement the diagnostic**

In `db.py`, add immediately after `spend_by_scope`:

```python
def unattributed_child_cost(since_iso: str) -> dict[str, Any]:
    """Query-time diagnostic (no stored state): cost of child sessions whose
    immediate parent has no runs row, so the child cannot roll up to a real
    root. Near-zero in practice because subagent_start records the edge before
    the child runs; a non-zero value signals delegation edges we failed to
    record (e.g. subagent_start handler error). Surfaced in /stats.
    """
    row = _get_conn().execute(
        """
        SELECT COALESCE(SUM(r.cost_usd), 0.0) AS unattributed_usd,
               COUNT(*)                       AS edges
        FROM subagent_edges e
        JOIN runs r ON r.session_id = e.child_session_id
        LEFT JOIN runs p ON p.session_id = e.parent_session_id
        WHERE r.started_at >= ? AND p.session_id IS NULL
        """,
        (since_iso,),
    ).fetchone()
    return {
        "unattributed_usd": float(row["unattributed_usd"] or 0.0),
        "edges": int(row["edges"] or 0),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -k unattributed_child_cost -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): query-time unattributed_child_cost diagnostic for #49"
```

---

## Task 6: Update budget note + `/stats cron` delegation footer

**Files:**
- Modify: `budget.py:485-488` (`_cron_block` note)
- Modify: `stats.py:20` (import), `stats.py:152-178` (`_cron_block` footer)
- Test: `tests/test_budget.py:355-368`

- [ ] **Step 1: Update the failing budget-note test**

In `tests/test_budget.py`, rename/adjust `test_budget_cron_subcommand_notes_subagent_limit` (lines 355-368) to assert the new attribution phrasing:

```python
def test_budget_cron_subcommand_notes_subagent_attribution(tmp_path):
    _write_budget(
        tmp_path,
        """
        budgets:
          per_cron_job:
            default:
              daily_usd: 1.00
    """,
    )
    _seed("s1", 0.50, platform="cron", cron_job_id="job1")
    out = budget.handle("cron")
    assert "job1" in out
    assert "subagent" in out.lower()
    assert "includes" in out.lower()  # per-cron-job now attributes linked subagent spend
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_budget.py::test_budget_cron_subcommand_notes_subagent_attribution -v`
Expected: FAIL — current note says spend "EXCLUDES subagent", so `"includes"` is absent.

- [ ] **Step 3: Update the budget note**

In `budget.py`, replace the note lines in `_cron_block` (lines 485-488):

```python
    lines.append("")
    lines.append("  Note: per-cron-job spend now INCLUDES linked subagent (delegate_task)")
    lines.append("  cost — child runs are attributed to their root cron job via the")
    lines.append("  subagent_edges tree (async + nested). Spend from unlinked children")
    lines.append("  (edge not recorded) is not attributable; the global budget remains")
    lines.append("  the catch-all tope.")
```

- [ ] **Step 4: Run the budget test to verify it passes**

Run: `pytest tests/test_budget.py::test_budget_cron_subcommand_notes_subagent_attribution -v`
Expected: PASS.

- [ ] **Step 5: Write the failing stats-footer test**

Add to `tests/test_dashboard.py`… no — add to a stats test. Add to `tests/test_stats_models.py` is wrong scope; create the assertion in `tests/test_subagent_reconciliation.py` (it already exercises the full hook flow):

```python
def test_stats_cron_footer_flags_unattributed(tmp_path, monkeypatch):
    """/stats cron shows a warning footer when a delegation edge has an
    unrecorded parent."""
    import hermes_telemetry.db as db
    import hermes_telemetry.stats as stats

    ctx = MockPluginContext()
    _init_mod.register(ctx)

    # A cron run so the cron block renders at all.
    ctx.fire("on_session_start", session_id="cron_job1_20260601_020000", model="m", platform="cron")
    ctx.fire(
        "post_api_request",
        session_id="cron_job1_20260601_020000",
        model="m",
        provider="anthropic",
        api_duration=0.1,
        api_call_count=0,
        assistant_content_chars=10,
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
               "cache_write_tokens": 0, "reasoning_tokens": 0},
    )
    # An orphan child edge (parent never recorded) with real cost.
    db.start_run("orphan-child", model="m", platform="cli")
    db.record_llm_call("orphan-child", db._utcnow(), "m", "anthropic", 100, 50, 0.02, 100)
    db.record_subagent_start(child_session_id="orphan-child", parent_session_id="ghost")

    out = stats._cron_block(window_hours=720)
    assert "unattributed" in out.lower()
```

- [ ] **Step 6: Run the stats-footer test to verify it fails**

Run: `pytest tests/test_subagent_reconciliation.py::test_stats_cron_footer_flags_unattributed -v`
Expected: FAIL — `_cron_block` renders no delegation footer.

- [ ] **Step 7: Implement the stats footer**

In `stats.py`, extend the import (line 20):

```python
from datetime import datetime, timedelta, timezone
```

In `_cron_block`, before `return "\n".join(lines)` (line 178), append the footer:

```python
    since_iso = (
        date_from
        if date_from
        else (datetime.now(timezone.utc) - timedelta(hours=window_hours or 720)).isoformat()
    )
    unattr = db.unattributed_child_cost(since_iso)
    if unattr["edges"]:
        lines.append("")
        lines.append(
            f"  ⚠ {unattr['edges']} subagent edge(s) with an unrecorded parent — "
            f"{_fmt_cost(unattr['unattributed_usd'])} unattributed to any cron job."
        )
        lines.append("  (Attributed subagent spend is reflected in `/budget cron`.)")
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/test_subagent_reconciliation.py::test_stats_cron_footer_flags_unattributed tests/test_budget.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add budget.py stats.py tests/test_budget.py tests/test_subagent_reconciliation.py
git commit -m "feat(stats): surface subagent attribution + unattributed cost for #49"
```

---

## Task 7: Docs — `ONBOARDING.md`

**Files:**
- Modify: `ONBOARDING.md` (Schema evolution table; Subagent Architecture; Known Limitations; hook kwargs reference for subagent_start/stop)

- [ ] **Step 1: Correct the stale `subagent_stop` claim and hook kwargs reference**

In the **Hook kwargs Reference → subagent_stop** section, replace the "no child id" note with the verified kwargs and add a `subagent_start` entry:

```markdown
- **`subagent_start`** — `parent_session_id`, `parent_turn_id`, `parent_subagent_id`,
  `child_session_id`, `child_subagent_id` (`sa-{i}-{uuid8}`), `child_role`, `child_goal`.
  Fires synchronously in Hermes' `_build_child_agent`, BEFORE async dispatch, so the
  parent→child edge is recorded before the child's first `post_api_request`.
- **`subagent_stop`** — `parent_session_id`, `parent_turn_id`, `child_session_id`,
  `child_role`, `child_summary`, `child_status`, `duration_ms`. Carries
  `child_session_id` (verified against `tools/delegate_tool.py`) but NOT
  `child_subagent_id`; no token/cost data. `child_session_id` is the correlation key
  used by `subagent_edges`.
```

- [ ] **Step 2: Add the v11 row to the Schema evolution table**

```markdown
| v11 | New table `subagent_edges` (parent→child delegation tree) + `idx_subagent_edges_parent`. Attributes async/nested subagent cost to `per_cron_job` via a recursive CTE at query time. (#49) |
```

- [ ] **Step 3: Update Subagent Architecture and Known Limitations**

Replace the "per_cron_job undercounts / parent_session_id never populated" text with:

```markdown
Delegated (subagent) spend IS now attributed to `per_cron_job`. Each `subagent_start`
records a `subagent_edges` row (`child_session_id → parent_session_id`, plus role,
subagent ids, timestamps); `db.spend_by_scope("cron_job", …)` seeds from the cron root
session(s) and walks the edge tree down with a recursive CTE, so async and nested
delegation resolve to the root cron job. `global` sums all runs, so the same cost rows
are only regrouped — no double counting.

`runs.parent_session_id` remains dormant on purpose: `subagent_edges` is the single
source of truth for the delegation tree (it also keeps role/subagent-id/timestamps for
debugging). The edge table is persistent, so there is no in-memory map to leak.

Phase-1 boundary: this is reporting/aggregation correctness. A detached child's own
turns still self-enforce only `global`; in-path enforcement (pausing a runaway async
child mid-flight) is a planned Phase 2. `unattributed_child_cost` (query-time) flags
child cost whose parent edge was never recorded.
```

- [ ] **Step 4: Commit**

```bash
git add ONBOARDING.md
git commit -m "docs: subagent cron attribution + correct stale subagent_stop kwargs (#49)"
```

---

## Task 8: Full verification + branch check

**Files:** none (verification only)

- [ ] **Step 1: Run the exact CI check**

Run: `ruff format --check . && ruff check . && pytest tests/ -v`
Expected: format clean, lint clean, all tests pass (new + existing).

- [ ] **Step 2: If ruff format flags files, apply and re-run**

Run: `ruff format . && ruff check . && pytest tests/ -q`
Expected: PASS. Then:

```bash
git add -A
git commit -m "style: ruff format for #49"
```

(Skip this commit if step 1 was already clean.)

- [ ] **Step 3: Confirm schema lockstep**

Run: `pytest tests/test_db.py::test_schema_idempotent tests/test_db.py -k "v11" -v`
Expected: PASS — `COUNT(*) schema_version == 11` and all v11 tests green.

- [ ] **Step 4: Sanity-check the diff scope**

Run: `git diff --stat feat/moa-integration...HEAD`
Expected: only `db.py`, `__init__.py`, `budget.py`, `stats.py`, `ONBOARDING.md`, the two design/plan docs, and the three test files.

---

## Deferred (not in this plan)

- **In-path child-side enforcement / cron pause mid-flight** — Phase 2 (design §7, §11).
- **`/stats cron` per-job cost column subtree-awareness** — `db.cost_by_job` still shows raw per-cron-session cost; the authoritative attributed total is `/budget cron` (`spend_by_scope`). Making `cost_by_job` subtree-aware (a second recursive CTE grouped by `cron_job_id`) and rendering the full parent→child tree are a follow-up slice.
- **Stored `unattributed_child_cost` counter** — rejected in favor of the query-time diagnostic (design §3).
- **Populating `runs.parent_session_id`** — stays dormant; `subagent_edges` is the source of truth.

---

## Self-Review

**Spec coverage:** migration v11 (Task 1) ✓ · edge write API (Task 2) ✓ · subagent_start/stop hooks (Task 3) ✓ · recursive-CTE attribution + nested→root + no double count (Task 4) ✓ · unattributed diagnostic (Task 5) ✓ · budget note + /stats footer (Task 6) ✓ · docs (Task 7) ✓ · full check (Task 8) ✓. Design §7 enforcement is intentionally deferred and documented.

**Placeholder scan:** every code step contains full code; every run step names the command and expected result. No TBD/TODO.

**Type/name consistency:** `record_subagent_start` / `record_subagent_stop` / `unattributed_child_cost` signatures are defined in Tasks 2 & 5 and called identically in Tasks 3, 4, 6 and the tests. `subagent_edges` column names match across the migration (Task 1), write API (Task 2), CTE (Task 4), and diagnostic (Task 5). `spend_by_scope` return keys (`spent_usd`, `total_calls`, `estimated_pct`) are unchanged.
