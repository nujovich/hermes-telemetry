# CLAUDE.md — hermes-telemetry

## Project context

Hermes Agent observability plugin. Captures tokens, cost, latency, and tool calls per
session and cron job. Enforces spend budgets via hooks. Persists to local SQLite.

Read **ONBOARDING.md** before touching any code — it documents every non-obvious design
decision made since v0.1, including what Hermes hooks can and cannot do.

---

## Critical rule: always verify against the Hermes Agent source

Any change that involves hook kwargs, return value semantics, PluginContext API
signatures, or any status/enum string **MUST be verified against the live
NousResearch/hermes-agent source — WITHOUT EXCEPTION** before implementation.

Documentation and comments in this repo can be stale. The source is the only authority.

### What triggers a source check

- Hook kwargs — the exact fields available in each hook callback
- Hook return value semantics — what Hermes does with what you return
- `ctx.register_hook()` / `ctx.register_command()` signatures and accepted kwargs
- Any status or enum string used in comparisons (`child_status`, `platform`, `finish_reason`)
- Any claim about hook firing order or frequency ("fires once per turn", etc.)
- Any PluginContext method not already listed in `ONBOARDING.md § PluginContext API`
- **Dashboard slot names** — the shell only renders slots from its catalogue
  (`docs/extending-the-dashboard.md:590-600`). `registerSlot()` with an unknown
  name is a silent no-op (widget loads, never mounts). NEVER propose a slot name
  without verifying it exists in the catalogue. See `ONBOARDING.md § Slot widgets`
  for the verified list (`sessions:top`, `cron:top`, `header-right`,
  `analytics:bottom`). If no shell slot fits, render inside `TelemetryPage` instead.

### How to verify

Fetch the relevant file directly:

```
https://raw.githubusercontent.com/NousResearch/hermes-agent/main/<path>
```

| File | What it covers |
|------|----------------|
| `hermes_cli/plugins.py` | PluginContext API, VALID_HOOKS, hook dispatch, pre_tool_call block semantics |
| `agent/conversation_loop.py` | pre/post_llm_call, pre/post_api_request, on_session_* kwargs |
| `tools/delegate_tool.py` | subagent_stop kwargs, child_status canonical values |
| `model_tools.py` | post_tool_call kwargs |
| `cron/scheduler.py` | cron session_id format |
| `agent/moa_loop.py` · `agent/auxiliary_client.py` · `hermes_cli/moa_config.py` | MoA virtual provider: reference/aggregator call mechanics (auxiliary `call_llm`, no hooks fire), preset config shape. See `ONBOARDING.md § Mixture of Agents (MoA)` |
| `docs/extending-the-dashboard.md` | Dashboard slot catalogue (valid `registerSlot` names) |

---

## Design authority: ONBOARDING.md

`ONBOARDING.md` documents every non-obvious design decision in this codebase.

1. Check it before implementing anything that touches hooks, budget, pricing, or DB.
2. If you discover something new (an undocumented kwarg, a new status value, a changed
   API), update ONBOARDING.md as part of the same PR.

Never contradict ONBOARDING.md without first verifying against the source and
updating the doc.

---

## Critical rule: schema changes require a migration

Any change to the SQLite shape (new column, new table, new index, renamed
or retyped column) **MUST** go through a new `_migrate_vN` function in
`db.py`. Editing `_ensure_schema` directly only affects fresh DBs — every
upgrading user keeps the old shape and the plugin breaks silently.

Before touching `db.py`, read **ONBOARDING.md § Adding a column or table —
mandatory checklist** in full. Non-negotiables:

- New shape ⇒ new `_migrate_vN`, never an in-place edit of an existing one.
- `_SCHEMA_VERSION` bumps in lockstep (CI enforces via `test_schema_idempotent`).
- Use `_add_column_if_missing` for `ALTER TABLE ADD COLUMN`. Never write a
  raw `ALTER` wrapped in `try/except sqlite3.OperationalError: pass` — that
  is the exact pattern that wedged user DBs in the v7 incident (the
  exception also covers `SQLITE_LOCKED` from cross-process cron contention,
  so the column never lands but the version gets marked applied).
- Add a per-version test (`test_schema_vN_columns` or
  `test_migrate_vN_*`) AND a test that simulates the upgrade path from
  the previous version with the column missing (template:
  `test_migrate_v9_repairs_missing_column_from_wedged_v7`).
- Forward-only: never reuse a version number. A buggy migration is fixed
  by a follow-up `_migrate_v(N+1)` repair pass, never by editing
  `_migrate_vN` in place.

Updating the migration table in ONBOARDING.md § Schema evolution is part
of the same PR, not a follow-up.

---

## Test and lint commands

```bash
# Full check — matches CI exactly. Run before every commit.
ruff format --check . && ruff check . && pytest tests/ -v

# Tests only
pytest tests/ -v --tb=short

# Single file
pytest tests/test_init.py -v
```

---

## File isolation rule

Never use `Path.home()` directly. All Hermes file paths must go through:

```python
hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
```

Tests redirect `HERMES_HOME` to an isolated tmp dir. A direct `Path.home()` call
escapes the isolation and touches the developer's real `~/.hermes`.
See `tests/test_isolation.py` and `ONBOARDING.md § Test Isolation Contract`.
