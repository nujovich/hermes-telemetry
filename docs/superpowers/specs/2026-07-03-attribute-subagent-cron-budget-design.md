# Design — Attribute async subagent cost to `per_cron_job` budgets (Phase 1)

**Issue:** [#49](https://github.com/nujovich/hermes-telemetry/issues/49)
**Date:** 2026-07-03
**Scope:** Phase 1 — reporting/aggregation correctness. In-path child-side
enforcement is explicitly deferred to Phase 2.

---

## 1. Problem

Since Hermes v17 made subagents async (`delegate_task(background=true)`),
delegated work fans out in parallel. Child token/cost lands in `global` totals
(a global cap still stops a runaway), but it is **never attributed to the parent
cron job**, so `per_cron_job` budgets undercount delegated spend.

The correlation primitive exists in Hermes; it just isn't in the hooks where the
plugin counts cost. `post_api_request` / `post_tool_call` carry only the
**executing** agent's `session_id` (the child), with no `parent_session_id`. The
parent↔child link lives only in the delegation hooks (`subagent_start` /
`subagent_stop`).

## 2. Source verification (NousResearch/hermes-agent@main)

Verified directly against source — the in-repo docs were partly stale.

| Hook | File | Kwargs (relevant) |
|------|------|-------------------|
| `subagent_start` | `tools/delegate_tool.py:1383` | `parent_session_id`, `parent_turn_id`, `parent_subagent_id`, `child_session_id`, `child_subagent_id`, `child_role`, `child_goal` |
| `subagent_stop` | `tools/delegate_tool.py:2726` | `parent_session_id`, `parent_turn_id`, `child_session_id`, `child_role`, `child_summary`, `child_status`, `duration_ms` |
| `post_api_request` | `agent/conversation_loop.py:4112` | `session_id` (executing/child), `usage`, `model`, `provider`, `finish_reason`, … — **no `parent_session_id`** |
| `post_tool_call` | `model_tools.py:885` | `session_id` (executing/child), … — **no `parent_session_id`** |

Key findings that shape the design:

1. **`subagent_stop` DOES carry `child_session_id` now** (`child_session_id=getattr(_child_agent, "session_id", None)`). The in-repo `ONBOARDING.md` claim that it does not is **stale** and must be corrected.
2. **The correlation key present in BOTH start and stop is `child_session_id`.**
   `subagent_stop` does *not* carry `child_subagent_id`. So `child_session_id` is
   the join/primary key for the edge, not `child_subagent_id`.
3. **`child_role` is available now** (at both start and stop). The issue's open
   question about waiting for an upstream `delegated_role` kwarg is moot.
4. **`subagent_start` runs synchronously in `_build_child_agent`, before async
   dispatch.** The edge is therefore established before any child
   `post_api_request` fires — resolution never races the child's own events.
5. **No double-count risk by construction.** The plugin counts cost from
   `post_api_request.usage` keyed by the executing session. `global` sums all
   `runs` rows; `per_cron_job` will sum the resolved subtree. Same rows, different
   grouping. (Hermes internally folds child cost into the parent's
   `session_estimated_cost_usd` at `subagent_stop`, but the plugin never reads
   that attribute, so it is irrelevant here.)
6. `child_status` canonical values: `completed | interrupted | failed | timeout | error`.
7. Subagents run under a `DaemonThreadPoolExecutor`; edge writes go through the
   existing per-thread-connection model in `db.py`.

## 3. Design decisions

- **Enforcement scope: aggregation-only (Phase 1).** Resolve to the root cron job
  at query time. The hot path (`subagent_start` / child `post_api_request`) does
  INSERTs only. In-path enforcement during a detached child's own turns is Phase 2.
- **`unattributed_child_cost`: computed at query time, no stored counter.**
  A stored counter would be precomputed state that can drift; a query-time
  diagnostic keeps a single source of truth (the raw edge tree).
- **Persistent `subagent_edges` table**, not the dormant `runs.parent_session_id`
  column. The column would suffice for pure resolution (one parent per child), but
  the table preserves the real delegation tree — role, subagent ids, turn,
  timestamps, status — for `/stats` debugging. `runs.parent_session_id` stays
  dormant and documented; the edge table is the single source of truth.
- **Resolve to the root** for nested delegation (`max_spawn_depth > 1`). The
  recursive CTE seeds from cron roots and walks the whole subtree down, so
  nesting resolves to root naturally.

## 4. Data model — `subagent_edges` (migration v11)

```sql
CREATE TABLE IF NOT EXISTS subagent_edges (
    child_session_id   TEXT PRIMARY KEY,   -- key present in start AND stop
    parent_session_id  TEXT NOT NULL,
    parent_turn_id     TEXT,
    parent_subagent_id TEXT,               -- full subagent-id tree (debug)
    child_subagent_id  TEXT,               -- sa-{i}-{uuid8}; start only
    child_role         TEXT,               -- available now
    started_at         TEXT NOT NULL,
    stopped_at         TEXT,               -- NULL until subagent_stop
    child_status       TEXT                -- NULL until stop
);
CREATE INDEX IF NOT EXISTS idx_subagent_edges_parent
    ON subagent_edges(parent_session_id);
```

- `_migrate_v11` creates the table + index (`IF NOT EXISTS`), appended in
  `_ensure_schema` after `_migrate_v10`.
- `_SCHEMA_VERSION` bumps 10 → 11 in lockstep (`test_schema_idempotent`).
- Forward-only; a new table (not a column) so `_add_column_if_missing` does not
  apply, but the "wedged prior version" upgrade test still does (simulate a v10 DB
  missing the table → `_ensure_schema` creates it).

## 5. Hook changes (`__init__.py`)

- **Register a new `subagent_start` handler** (not currently registered):
  `INSERT OR IGNORE` an edge row with `started_at = now`, `stopped_at = NULL`.
  Idempotent on the `child_session_id` PK. Fires synchronously before dispatch,
  so the edge exists before the child's first `post_api_request`.
- **Modify the `subagent_stop` handler**: keep the existing
  `delegate_task/subagent` proxy `tool_calls` row (recorded against the parent),
  and additionally `UPDATE subagent_edges SET stopped_at = now, child_status = ?`
  correlated by `child_session_id`. If no edge exists (start was missed),
  **backfill** an edge from the stop kwargs (`parent_session_id` +
  `child_session_id` are present; `child_subagent_id` is NULL there).
- **No change to `post_api_request` / `post_tool_call`.** Child usage already
  lands in `llm_calls` / `runs` keyed by the child session — that is the source
  data the aggregation resolves.

## 6. Aggregation — recursive CTE in `db.spend_by_scope("cron_job", …)`

Replace the `WHERE cron_job_id = ?` filter with: seed from cron roots, walk the
edge tree down, sum over root + descendants.

```sql
WITH RECURSIVE tree(session_id) AS (
    SELECT session_id FROM runs WHERE cron_job_id = ?
    UNION
    SELECT e.child_session_id
    FROM subagent_edges e
    JOIN tree t ON e.parent_session_id = t.session_id
)
SELECT COALESCE(SUM(r.cost_usd), 0.0),
       COALESCE(SUM(r.estimated_llm_calls), 0),
       COALESCE(SUM(r.api_calls), 0)
FROM runs r
JOIN tree ON r.session_id = tree.session_id
WHERE r.started_at >= ?;
```

- Seeding from cron roots resolves nested delegation to the root transitively.
- `global` is unchanged (sums all `runs`), so no double counting.
- Accepted simplification: the window filter `started_at >= ?` is applied to child
  rows too. Children run within the parent's lifetime, so for daily/monthly
  windows this is immaterial.
- `UNION` (not `UNION ALL`) guards against cycles/duplicate paths.

## 7. Enforcement path (mechanics unchanged, now correct)

`budget.evaluate_run` for the **parent cron run** already checks the `cron_job`
scope; its next `pre_llm_call` / `pre_tool_call` now sees the true subtree total
via the updated `spend_by_scope`. **No new enforcement code.**

Phase-1 boundary (documented): a detached child's *own* turns still self-enforce
only `global`. The cron cap is enforced whenever the cron session itself next
evaluates, or via `global`. Stopping a runaway async child mid-flight is Phase 2.

## 8. `/stats` and the `unattributed_child_cost` diagnostic (query-time)

- `/stats` renders the delegation tree (parent → children, role, status, cost) by
  joining `subagent_edges` to per-run cost.
- `unattributed_child_cost` is **computed**, not stored: the cost of child
  sessions whose walk-up terminates at a `parent_session_id` with no `runs` row
  (parent never recorded). A best-effort health gauge — near-zero given
  synchronous-start edges — with no precomputed state.

## 9. Testing

- **Migration:** `test_schema_v11_columns` (table + columns exist);
  `test_migrate_v11_creates_table_from_wedged_v10` (v10 DB missing the table →
  `_ensure_schema` creates it and the record path works). `test_schema_idempotent`
  already enforces the version lockstep.
- **Edges:** `subagent_start` inserts an edge; `subagent_stop` sets
  `stopped_at` / `child_status`; backfill when start was missed.
- **Aggregation:** `spend_by_scope("cron_job", X)` includes child and grandchild
  cost; async `background=true` path; nested → root; `global` still counts once.
- **Tests that flip:** `test_subagent_cron_parent_costs_exclude_child`
  (`tests/test_subagent_reconciliation.py:344`) must assert **inclusion**;
  `test_budget_cron_subcommand_notes_subagent_limit` (`tests/test_budget.py:355`)
  and the `budget._cron_block` caveat text (`budget.py:485`) change (no longer
  "excludes subagent spend").
- `ruff format --check . && ruff check .` clean.

## 10. Docs (same PR)

- **ONBOARDING.md:** correct the stale claim (`subagent_stop` DOES carry
  `child_session_id`; `subagent_start` carries parent+child session ids, both
  subagent ids, `child_role`, `child_goal`); add the v11 row to the Schema
  evolution table; update Subagent Architecture and Known Limitations
  (`per_cron_job` now attributes delegated spend via the edge tree, with the
  Phase-1 enforcement boundary noted); update the hook kwargs reference for
  `subagent_start` / `subagent_stop`; note `runs.parent_session_id` stays dormant
  with the edge table as the source of truth.

## 11. Out of scope (Phase 1)

- In-path child-side enforcement / cron pause mid-flight (**Phase 2**).
- A stored `unattributed_child_cost` counter.
- Populating `runs.parent_session_id`.
