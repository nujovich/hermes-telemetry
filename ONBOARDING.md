# hermes-telemetry — Design & Implementation Notes

> **Onboarding document for new contributors.** This file captures every
> non-obvious design decision made from v0.1 through v0.4.0. Reading the code
> without this context risks re-learning hard-won findings from the Hermes Agent
> source. Start here.

---

## Table of Contents

1. [What This Plugin Does](#what-this-plugin-does)
2. [Module Map](#module-map)
3. [The Hermes Plugin Model (what we can and can't do)](#the-hermes-plugin-model)
4. [Hook Pipeline](#hook-pipeline)
5. [Hook kwargs Reference](#hook-kwargs-reference)
6. [Database Layer](#database-layer)
7. [Pricing Engine](#pricing-engine)
8. [Budget Engine](#budget-engine)
9. [Pricing Auto-Refresh](#pricing-auto-refresh)
10. [Subagent Architecture](#subagent-architecture)
11. [Test Isolation Contract](#test-isolation-contract)
12. [Design Decisions by Version](#design-decisions-by-version)
13. [Metrics: Real vs Estimated](#metrics-real-vs-estimated)
14. [CI/CD Pipeline](#cicd-pipeline)
15. [Known Limitations](#known-limitations)
16. [PluginContext API](#plugincontext-api)
17. [Valid Hooks Reference](#valid-hooks-reference)
18. [Dashboard Plugin Surface](#dashboard-plugin-surface)

---

## What This Plugin Does

Hermes Agent runs autonomously — across sessions, platforms (CLI, cron, Telegram,
Discord), and subagents — which means it can keep spending even when you're not
watching. `hermes-telemetry` lives **inside the runtime** as a Hermes plugin and
does two things:

1. **Captures telemetry** (tokens, cost, latency, tool calls, session metadata)
   and persists it to a local SQLite database.
2. **Enforces budget guardrails** before the next tool call is made — soft alerts
   injected into the conversation, hard blocks that abort tool calls when limits
   are exceeded, and cron-job pausing for future runs.

**Design principle:** observability is invisible to the model. Everything goes
through hooks. The only user-facing surface is `/stats`, `/budget`, and `/setup`.
Errors are swallowed in every hook — the plugin must never take down a session.

This plugin was built for the [Hermes Agent Challenge](https://dev.to/devteam/join-the-hermes-agent-challenge-1000-in-prizes-13cd)
and addresses [NousResearch/hermes-agent#6642](https://github.com/NousResearch/hermes-agent/issues/6642).

---

## Module Map

```
hermes-telemetry/
├── __init__.py          ← Plugin entry point. Registers all 10 hooks + 3 slash
│                          commands. Contains the cron regex, approx-token store,
│                          and the fallback estimation logic for usage=None.
├── db.py                ← SQLite persistence layer. Schema v1→v3 migrations,
│                          per-thread connections, WAL mode, write API and
│                          read/budget query API.
├── pricing.py           ← Cost estimation engine. Priority-chain lookup
│                          (custom YAML → built-in → `:free`→$0 → prefix match).
│                          All 5 token components. Google-symmetric normalization.
├── pricing_refresh.py   ← Auto-refresh from remote pricing APIs. PricingSource
│                          ABC, OpenRouterSource, GoogleAISource. Merge strategy
│                          preserves manual overrides.
├── budget.py            ← Budget verdict engine. Window math in local tz,
│                          verdict cache, anti-spam ledger, tool-gate helpers,
│                          /budget command.
├── stats.py             ← /stats command implementation. All subcommands:
│                          summary, cron, providers, models, raw.
├── moa.py               ← Mixture-of-Agents awareness. Resolves the `provider=
│                          "moa"` virtual-provider preset to its aggregator's
│                          real provider/model so the call is priced/attributed
│                          correctly. See `§ Mixture of Agents (MoA)`.
├── setup.py             ← /setup command + auto-setup on first load. Generates
│                          pricing.yaml and budget.yaml with defaults.
├── plugin.yaml          ← Plugin metadata: name, version, declared hooks.
│                          `provides_hooks` is declarative only (the loader does
│                          NOT filter against it). Keep it accurate so it matches
│                          the code, but enabling/disabling a hook is done in
│                          `__init__.py`. See `§ Plugin Discovery Gotcha` for the
│                          `name`-collision trap that bit us with PR #44.
├── dashboard/
│   ├── index.html       ← Standalone SPA. Chart.js, no build step, no auth.
│   └── serve.py         ← stdlib HTTP server, port 8765, --host flag.
├── tests/
│   ├── conftest.py      ← Autouse HERMES_HOME isolation (see Test Isolation).
│   └── test_*.py        ← 262 tests. All in-memory SQLite, no live gateway.
├── config.example.yaml  ← Annotated pricing.yaml example.
└── budget.example.yaml  ← Annotated budget.yaml example.
```

---

## The Hermes Plugin Model

Before reading the hook logic, understand the constraint space that was
discovered by auditing the Hermes Agent source:

### What a plugin CAN do

| Mechanism | Effect |
|-----------|--------|
| `pre_tool_call` returning `{"action":"block","message":...}` | Aborts the tool call. Returns error to model instead. The current in-flight model response is already complete and will be billed. |
| `cron.jobs.pause_job(job_id, reason)` | Prevents the cron job from running again. Does not stop the current run. |
| `pre_llm_call` returning `{"context": "..."}` | Injects text into the conversation context before the API call. Used for soft budget alerts. |
| Any read from the DB | Fully available in any hook via `db.py`. |

### What a plugin CANNOT do

| Attempted action | Why it doesn't work |
|-----------------|---------------------|
| Abort an in-flight model API call | No hook fires after the call starts and before it ends. `pre_api_request` and `pre_llm_call` both fire BEFORE; their return value cannot abort. |
| Get token data from `pre_llm_call` | This hook fires once per turn but before any API call. No token data is available here. |
| Get token data from `post_llm_call` | This hook fires after the tool loop. No token data. Wrong hook for cost capture. |
| Link a subagent's tokens back to its parent cron job | `subagent_stop` has no child `session_id`. `on_session_start` has no `parent_session_id`. The link is unrecoverable from hooks. |

**Source references** (verified against hermes-agent source):
- `pre_llm_call` return: `agent/conversation_loop.py:687-722` (context injection only)
- `pre_api_request` return: `agent/conversation_loop.py:1235-1255` (return discarded)
- `pre_tool_call` block: `hermes_cli/plugins.py:1666-1707` ✅
- `cron.jobs.pause_job`: `cron/scheduler.py` ✅
- `subagent_stop` no child id: `tools/delegate_tool.py:2269-2277`

---

## Hook Pipeline

The plugin registers 10 of the 16 available Hermes hooks. Here is what each
one does and why it was chosen:

```
Hook                    Purpose
────────────────────────────────────────────────────────────────────────────
on_session_start        Create runs row, extract cron_job_id from session_id.
                        Fired ONCE per new session (not per turn).

pre_api_request         Stash approx_input_tokens keyed by (session_id, call_count)
                        for the fallback estimator when usage=None.

post_api_request        PRIMARY TOKEN SOURCE. One call per individual API call
                        within a turn. Carries the usage dict with real token
                        counts. Calculates cost, records llm_calls row, updates
                        runs totals.

post_tool_call          Records tool name, success/failure, latency. Also records
                        a proxy row for delegate_task/subagent calls (no token
                        data there, just a count).

post_llm_call           Fires once per turn after the tool loop. NO token data.
                        Used only to keep runs.ended_at current during multi-turn
                        interactive sessions.

subagent_stop           Records a synthetic tool_call row ("delegate_task/subagent")
                        for proxy count of subagent invocations. NO token data
                        available in this hook.

on_session_end          Fires at the end of every run_conversation() call. Sets
                        final status: ok / error / interrupted.

on_session_finalize     Safety net for true session teardown (CLI atexit, gateway
                        expiry). Ensures status is "ok" if not already set.

pre_llm_call            (1) Attaches sender_id to the run for per-sender budgets.
                        (2) Injects one-time-per-window soft budget alert into
                        the conversation context.
                        (3) Injects one-shot free→paid transition warning when
                        the current model was previously seen as free but is
                        now incurring cost.

pre_tool_call           Hard budget enforcement. Returns {"action":"block",...}
                        if any scope is in hard breach. Also triggers cron pause.
```

**Why `post_api_request` is the primary token hook:** Hermes can make multiple
API calls per turn (retries, tool-call rounds, reasoning). Only `post_api_request`
fires per individual API call and carries the `usage` dict. The `pre_/post_llm_call`
hooks fire once per turn and have no token data. This design was discovered from
the source (not guessable from the hook names alone).

**Why not capture budget in `post_api_request`:** Token counts have just been
recorded but verdict cache is stale. The tool-gate in `pre_tool_call` is the
right place — it fires synchronously before the next tool and has a fresh view
of accumulated spend.

---

## Hook kwargs Reference

These are the actual kwargs confirmed from the Hermes Agent source. Only the
ones used by this plugin are listed; unknown kwargs are absorbed by `**_kw`.

### `on_session_start`
Source: `agent/conversation_loop.py:295-300`
```python
session_id: str      # unique; cron format: "cron_{job_id}_{YYYYMMDD_HHMMSS}"
model: str           # active model name
platform: str        # "cli" | "cron" | "telegram" | "discord" | ...
```
Fired **once** at the start of a brand-new session (not on each turn of an
interactive session, not on session continuation).

### `pre_llm_call`
Source: `agent/conversation_loop.py:702-711`
```python
session_id: str
user_message: str
conversation_history: list
is_first_turn: bool
model: str
platform: str
sender_id: str
```
Fires once per turn (before the API call loop). Return value used for context
injection only. **NO token data.**

### `pre_api_request`
Source: `agent/conversation_loop.py:1235-1253`
```python
task_id: str
session_id: str
api_call_count: int       # 0-indexed within this turn
approx_input_tokens: int  # APPROXIMATE (character-based estimate)
request_messages: list
model: str
provider: str
base_url: str
...
```
Fires before each individual API call. `approx_input_tokens` is used only as a
fallback estimator when `usage=None` in `post_api_request`.

### `post_api_request`  ← PRIMARY hook for tokens/cost/latency
Source: `agent/conversation_loop.py:3463-3482`
```python
task_id: str
session_id: str
platform: str
model: str
provider: str             # verbatim from gateway — NOT normalized
api_call_count: int
api_duration: float       # seconds; multiply by 1000 for ms
finish_reason: str
response_model: str       # model name as reported by the provider response
usage: dict | None        # CanonicalUsage (see below) or None when unavailable
assistant_content_chars: int
assistant_tool_call_count: int
```

`usage` dict (from `agent/usage_pricing.py::CanonicalUsage`):
```python
{
  "input_tokens": int,        # non-cached input tokens
  "output_tokens": int,
  "cache_read_tokens": int,
  "cache_write_tokens": int,
  "reasoning_tokens": int,
  "request_count": int,
  "prompt_tokens": int,       # = input + cache_read + cache_write
  "total_tokens": int,        # = prompt + output
}
```
`usage` is **None** when the provider returns no usage info (some streaming
providers, ACP mode). The fallback estimator in `__init__.py` kicks in,
tokens are estimated from `approx_input_tokens + assistant_content_chars/4`,
and the row is flagged `estimated=1`.

**IMPORTANT:** `prompt_tokens` is intentionally **ignored** in cost calculation
to avoid double-counting. `prompt_tokens = input + cache_read + cache_write`, so
using it plus the individual components would count them twice.

### `post_tool_call`
Source: `model_tools.py:994-1005`
```python
tool_name: str
args: dict
result: str        # JSON string (or plain text) returned by the tool
task_id: str
session_id: str
tool_call_id: str
duration_ms: int
```
Success/failure detection: attempt `json.loads(result)` and check for an
`"error"` key. If that fails (not valid JSON), fall back to
`result.startswith('{"error"')`. This is robust to tools that return plain text.

### `on_session_end`
Source: `agent/conversation_loop.py:4692-4700`
```python
session_id: str
completed: bool
interrupted: bool
model: str
platform: str
```
Fires at the end of every `run_conversation()` call — once per turn in
interactive CLI sessions, once per cron job execution. Status derivation:
`interrupted` → `"interrupted"`, `completed` → `"ok"`, else → `"error"`.

### `on_session_finalize`
Source: `cli.py:955`, `gateway/run.py:9646`
```python
session_id: str | None
platform: str
```
Fires on true session teardown. Safety net: sets status to `"ok"` if not already
set (the `on_session_end` → `on_session_finalize` sequence ensures at least one
of them fires).

### `subagent_stop`
Source: `tools/delegate_tool.py:2269-2277`
```python
parent_session_id: str
child_role: str
child_summary: str
child_status: str    # "completed" | "failed" | "error" | "interrupted" | "timeout"
duration_ms: int
```
**NO token or cost data.** We record a synthetic `tool_calls` row with
`tool_name="delegate_task/subagent"` for proxy count only.

---

## Database Layer

### Design choices

**SQLite, not Postgres/external:** The plugin runs inside the gateway process
with no server setup. SQLite is the only zero-dependency option that survives
gateway restarts and has ACID guarantees. No network hop.

**WAL mode:** Hermes cron jobs run in a `ThreadPoolExecutor`, so multiple jobs
write concurrently from different threads. WAL allows one writer + concurrent
readers simultaneously. This is the standard SQLite high-concurrency pattern.

**Per-thread connections:** `threading.local()` — each thread opens its own
connection to the same WAL DB file. The alternative (a single connection
protected by a `threading.Lock`) would serialize all writes and bottleneck
parallel cron jobs.

**`_schema_lock` DDL guard:** Even with per-thread connections, SQLite DDL
(`CREATE TABLE`, `ALTER TABLE`) raises `SQLITE_LOCKED` if two threads try to
migrate simultaneously on a fresh DB. `busy_timeout` does NOT retry `SQLITE_LOCKED`
errors. Holding a Python `threading.Lock()` while running `_ensure_schema` is
cheap and eliminates the race.

**`busy_timeout=30000` before `PRAGMA journal_mode=WAL`:** Switching an empty
(or freshly opened) DB to WAL requires a brief exclusive lock. If `busy_timeout`
is not set first and another thread connects at the same time, the WAL switch
fails immediately with "database is locked". The 30s timeout covers CI
environments with slow or network-backed filesystems.

**`synchronous=NORMAL`:** Safe for WAL mode (SQLite docs confirm this). Faster
than `FULL` while maintaining durability on unclean shutdown at the WAL
checkpoint level.

### Schema evolution

Migrations run on first connect per thread. Idempotent: check `schema_version`
table before applying.

| Version | What was added |
|---------|---------------|
| v1 | Base tables: `runs`, `llm_calls`, `tool_calls`, `schema_version` |
| v2 | `llm_calls`: `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `estimated`. `runs`: `parent_session_id`, `estimated_llm_calls` |
| v3 | `runs.sender_id`. New table: `budget_alerts` (anti-spam ledger) |
| v4 | `runs`: `cache_read_tokens`, `cache_write_tokens` (per-session/cron cache breakdown) |
| v5 | New table: `known_free_models` (free→paid transition tracking) |
| v6 | New table: `free_paid_transitions` (historical free→paid flips for the widget rendered inside `TelemetryPage`) |
| v7 | `llm_calls.provider_assumed` flag + `runs.provider_assumed_calls` counter (provider-assumed pricing visibility, issue #42) |
| v8 | New table: `model_unavailable_alerts` (404s captured via `api_request_error` — model removed/deprecated by provider) |
| v9 | Repair pass — re-adds any column from v2/v3/v4/v7 that was silently skipped by the old blanket `except OperationalError: pass` pattern when an `ALTER TABLE` hit a transient `SQLITE_LOCKED` (cross-process cron contention). Uses `_add_column_if_missing`; idempotent. |
| v10 | MoA (Mixture-of-Agents) attribution: `llm_calls.moa_preset` (preset name for a MoA aggregator call; NULL otherwise) + `runs.moa_calls` (per-session counter). See `§ Mixture of Agents (MoA)`. |

`_SCHEMA_VERSION` in `db.py` is the latest applied version — keep it in lockstep
with the highest `_migrate_vN`. `test_schema_idempotent` asserts the count of
`schema_version` rows equals `_SCHEMA_VERSION`, so adding a migration without
bumping the constant (or vice versa) fails CI.

### Adding a column or table — mandatory checklist

Any PR that adds, removes, renames, or retypes a column or table **MUST**
follow this checklist. Skipping a step has already wedged user DBs in
production once (the v7 `provider_assumed` incident); the rules below exist
specifically to prevent the recurrence.

1. **Write a new `_migrate_vN`.** Never edit `_ensure_schema` to alter the
   shape of an existing table — that only runs on fresh DBs, so every
   upgrading user keeps the old shape. New shape ⇒ new migration function.
2. **Bump `_SCHEMA_VERSION` to N** in `db.py` (kept in lockstep —
   `test_schema_idempotent` enforces this).
3. **Use `_add_column_if_missing`** for `ALTER TABLE ADD COLUMN`. Do **not**
   write a raw `ALTER` with a `try/except OperationalError: pass` — that is
   exactly the pattern that caused the v7 incident. The helper is idempotent
   (re-running it on an already-migrated DB is a no-op) and lets transient
   errors propagate so the migration is not marked applied prematurely.
4. **Guard `CREATE TABLE` with `IF NOT EXISTS`.** Same idempotency reason.
5. **Reads must tolerate the column being absent** if the code path can run
   *before* `_ensure_schema()` (rare, but `_get_conn` is the only entry —
   audit any new entry point that bypasses it).
6. **Writes that reference the new column must come AFTER the migration is
   registered.** If you add the column in v9 but the matching `INSERT`/`UPDATE`
   was deployed in the same release, you are fine — `_ensure_schema` runs at
   connect, before any write. But if you reference the column from a `SELECT`
   query in `stats.py` or a dashboard endpoint and the migration silently
   skipped, the query returns nothing or errors — write a test that exercises
   the upgrade path from the *previous* schema version (see
   `test_migrate_v9_repairs_missing_column_from_wedged_v7` as the template).
7. **Add a row to the migration table above** with `What was added` filled in
   and a link to the issue / PR.
8. **Add a per-version assertion test**: `test_schema_vN_columns` or
   `test_schema_vN_recorded` (see existing tests as the pattern).
9. **Never drop or rename a column in a single migration.** SQLite before
   3.35 has no `DROP COLUMN`, and even modern SQLite forbids it inside a
   transaction with foreign keys. If a column truly must go, do it across
   two releases: deprecate (stop writing) in vN, then remove (table-rebuild
   migration) in vN+1 only after verifying no installed version still
   writes it.
10. **Never reuse a `_SCHEMA_VERSION` number.** Forward-only. If a migration
    was buggy in production, write `_migrate_v(N+1)` to repair it (the v9
    repair pass is the canonical example) — never edit `_migrate_vN`
    in-place to fix the bug, because users who already ran the buggy version
    have `schema_version=N` and will skip the corrected code.

### Migration ALTER contract (post-v9)

Never wrap an `ALTER TABLE ADD COLUMN` in a blanket
`except sqlite3.OperationalError: pass` followed by an unconditional
`INSERT INTO schema_version`. `OperationalError` also covers
`SQLITE_LOCKED` (which `busy_timeout` does **not** retry across processes — the
`_schema_lock` only protects threads of the same process; cron jobs run in
separate processes and *will* contend on first connect after an upgrade) and
I/O errors. Swallowing those and then marking the version applied permanently
wedges the DB without raising. Use `_add_column_if_missing` instead — it checks
`pragma_table_info` first and lets any real `OperationalError` propagate, so
the surrounding migration is NOT marked applied and the next connect retries.
This was the root cause of the v7 `provider_assumed` silent-skip incident.

### `_ensure_run_row` — lazy insert (added v0.3.1)

**Problem:** For sessions that start on a running gateway BEFORE the plugin is
enabled (or on session continuation where `on_session_start` doesn't fire), the
`UPDATE runs WHERE session_id = ?` calls in `record_llm_call` and `end_run` were
silent no-ops — no row existed. `/stats` and `/budget` under-reported.

**Solution:** `_ensure_run_row(session_id, ts)` does `INSERT OR IGNORE INTO runs
(session_id, started_at, status) VALUES (?, ?, 'running')` before every UPDATE.
If the row already exists (happy path from `on_session_start`), it's a no-op.

**Called by:** `record_llm_call` and `end_run`. Not needed in `start_run` (it
already uses `INSERT OR IGNORE`).

### `runs` table key columns

| Column | Notes |
|--------|-------|
| `session_id` | PK. CLI: `{YYYYMMDD_HHMMSS}_{uuid6}`. Cron: `cron_{job_id}_{YYYYMMDD_HHMMSS}`. |
| `cron_job_id` | Extracted from `session_id` via anchored regex (see Cron regex below). NULL for non-cron. |
| `estimated_llm_calls` | Count of calls where provider returned `usage=None`. Used for budget degradation. |
| `parent_session_id` | Schema column reserved for future parent-child attribution. **Never populated** — the Hermes hooks do not expose the link. |
| `sender_id` | Set from `pre_llm_call.sender_id`. First non-null wins (`COALESCE(sender_id, ?)`). |
| `model` / `provider` | Set from first `record_llm_call`. `COALESCE(model, ?)` so they're never overwritten. |

---

## Pricing Engine

### Lookup priority chain

`estimate_cost(usage, model, provider="")` resolves a price by trying these in order:

1. **Custom YAML** (`~/.hermes/telemetry/pricing.yaml`, `models:` section) — exact match, case-insensitive
2. **Built-in table** (`_DEFAULT_PRICING`) — exact match
3. **`:free` suffix rule** — any id ending in `:free` resolves to an explicit `$0`
   (`{"input": 0.0, "output": 0.0}`), **before** the prefix fallback. See below.
4. **Prefix fallback** — scans all of the above plus `_PREFIX_PRICING`, **longest prefix wins**
5. **Google symmetric form** — if the above misses, tries `gemini-X` ↔ `google/gemini-X`
6. **Unknown** → returns `$0.00`, logs a one-time WARNING

**`:free` suffix rule (issue #32):** OpenRouter advertises free-tier variants with
a `:free` suffix (e.g. `nvidia/nemotron-3-ultra-550b-a55b:free`). These are `$0` by
definition. The rule sits in `_lookup_form` **after** the two exact-match steps but
**before** the prefix scan, for two reasons: (1) otherwise a suffixed free id
inherits its paid base's price via prefix — the `nvidia/nemotron-3-ultra` seed would
price `…-550b-a55b:free` at the paid rate, billing a free call as paid; (2) returning
an explicit zero dict (not the unknown-model `None`) makes the call resolve as
known-free — no estimated-price warning, and it's recorded in `known_free_models` so
the free→paid alert fires when the gateway later drops the `:free` suffix. A user's
explicit `:free` entry (step 1) still overrides the rule.

Every candidate is first filtered by the **provider-aware guard** (below), so a
source-ineligible entry is skipped and the chain falls through to the next one.

**Why longest-prefix-wins:** `gpt-4o-mini` must not be matched by `gpt-4o` or
`gpt-4`. `o1-mini` must not be matched by `o1`. The sorted-by-length approach
(candidates sorted by `-len(prefix)`) ensures specific prefixes take precedence.

### Provider-aware lookup guard (added v0.4.1, issue #24)

**Problem:** `pricing.yaml` is auto-populated from the OpenRouter catalog —
every fetched entry carries `_source: openrouter`. But the same model name can
be served by a *different* provider at a *different* price. Two real cases:

- **Nous Portal** serves `qwen3.7-plus` (bare id) on a flat subscription ($0),
  while OpenRouter lists `qwen/qwen3.7-plus` (prefixed) at a real per-token rate.
- **NVIDIA NIM** serves `nvidia/nemotron-3-super-120b-a12b` at $0.10/$0.50 while
  OpenRouter lists the **same exact id** at $0.09/$0.45 (a true key collision).

A provider-blind, model-name-only lookup silently costs a Nous/NIM call with the
OpenRouter rate — a plausible-but-wrong number. `_source` was decorative until
this guard made it load-bearing.

**Rule** (`_source_eligible(source, provider)`):

| Entry source | Eligible for which calls |
|--------------|--------------------------|
| `_source: openrouter` | only `provider==""` (backward-compat/unknown) or a provider containing `"openrouter"` |
| no `_source` (`_DEFAULT_PRICING`, `_PREFIX_PRICING`, hand-added overrides) | **always** (provider-neutral) |
| other named source (e.g. `google-ai`) | **always** (direct-provider rate, reasonable for any caller of that id) |

`provider` is the verbatim string from `post_api_request` (e.g. `"nous"`,
`"openrouter"`; NIM **canonicalizes to `"nvidia"`** — aliases `nim`/`nvidia-nim`/
`nemotron` are normalized by Hermes `normalize_provider()` before the hook fires,
verified against `hermes_cli/providers.py`).

**Inverted safe-default for the lookup path (added v0.6.1, issue #42):** the
guard's original failure mode was "fail to zero" — if the only candidate was a
source-ineligible OpenRouter entry, the lookup returned `None` and the call
recorded `cost=0`. For a cost-tracking plugin this is the *worst* failure: a
real paid call (e.g. Nous Portal reselling `moonshotai/kimi-k2.6` at the
OpenRouter rate) silently reads as zero spend. The lookup now **errs on the side
of recording cost**: when no eligible candidate matches but a source-ineligible
one would have, `_lookup_form` returns that price tagged `_provider_assumed:
True`, and `estimate_cost` logs a **one-time WARNING per `(model, provider)`
pair** advising the user to pin the rate. Eligible matches, `_DEFAULT_PRICING`
seeds, the `:free` rule, and source-neutral / `_subscription` overrides all win
*first* — the assumed fallback only fires when the sole available price is one
the guard would otherwise reject. This means a genuine flat-sub / wrong-rate
collision must be protected by a source-neutral `_subscription` (or hand-added)
entry; without one, the call now over-counts-with-warning instead of
under-counting-silently — the deliberately safer default for a cost tracker.

**Why NIM seeds live in `_DEFAULT_PRICING` (code), not the YAML:** for the
same-id collision, the OpenRouter entry in `models:` is excluded for a
`provider="nvidia"` call, and the lookup falls through to the source-neutral
seed in `_DEFAULT_PRICING`. Keeping seeds in code also makes them immune to an
OpenRouter sync overwriting the shared key. The `:free` promo variants
(`nvidia/...:free`) carry no seed — they resolve to `$0.00` via the **`:free`
suffix rule** above (issues #12/#32). (Before that rule existed, a `:free` variant
of any *seeded* model was mis-priced at the seed's paid rate via prefix match — the
earlier "resolve to $0 via the unknown-model fallback" claim only held for models
with no seeded paid base.)

### Pricing metadata tags (`_`-prefixed)

Three `_`-prefixed keys live on pricing entries and are **stripped from the
price dict** by `_load_custom_pricing` (captured into parallel structures
`model_sources` / `subscription_models` so the price math never sees them):

| Tag | Meaning | Effect |
|-----|---------|--------|
| `_source` | Which source wrote the entry (`openrouter`, `google-ai`, ...) | Drives the provider-aware guard |
| `_estimated_price` | OpenRouter model with no fixed price (negative→$0) | Counted by `estimated_price_share`; degrades hard budget verdicts to soft under `warn_only` |
| `_subscription` | A **declared** $0 (flat-sub / free-tier) rate, hand-added | Distinguishes a genuine $0 from a lookup miss; tracked in `_meta.subscription_models`; **excluded** from `estimated_price_models` |

A fourth key, `_provider_assumed`, is **not** a stored YAML tag — it is a
runtime-only marker synthesized by `_lookup_form` (issue #42) when a
source-ineligible entry is applied as a best-effort estimate. It rides on the
resolved price dict purely so `estimate_cost` can fire its one-time
provider-assumed warning; `_load_custom_pricing` never reads or writes it.

The marker is also **surfaced** (not just logged): `post_api_request` calls
`pricing.is_provider_assumed(model, provider)` and persists a per-call boolean
to the DB — `llm_calls.provider_assumed` plus a `runs.provider_assumed_calls`
counter (schema **v6**, `db._migrate_v6`). It mirrors the per-call `estimated`
flag end to end: `/stats providers` shows an `Asm%` column, and the dashboard
(both the standalone `serve.py` and the Hermes plugin) exposes it as an `Asm?`
column in Requests and an `Assumed` count in Providers (`provider_assumed_calls`
/ `provider_assumed_pct` in the API). **Deliberately unlike `_estimated_price`,
a provider-assumed cost does NOT degrade budget verdicts** — the resold-at-the-
same-rate case is usually the *correct* number, so it counts as real spend for
enforcement (the issue's "err on the side of recording cost"); the flag exists
only to make the assumption visible so the user can pin the rate.

**Adding a subscription/flat-rate model:** enter it under the provider's
**native (bare) id** with `input: 0.0`, `output: 0.0`, `_subscription: true`.
The bare id is never returned by OpenRouter, so it never collides with an
auto-fetched entry and survives every refresh untouched.

### Google-symmetric normalization (added v0.4.0)

**Problem:** The gateway reports model IDs differently depending on routing path:
- Direct Google AI: `gemini-3.5-flash` (no prefix)
- Via OpenRouter: `google/gemini-3.5-flash`

A single pricing YAML entry would only cover one form. Having the user maintain
both was error-prone.

**Solution:** `_google_alt_form(model_lc)` maps the pair symmetrically:
- `gemini-X` → try also `google/gemini-X`
- `google/gemini-X` → try also `gemini-X`

**Deliberately google-specific:** `anthropic/`, `meta-llama/`, `openrouter/` are
NOT normalized. On OpenRouter, `anthropic/claude-sonnet-4-6` costs differently
than direct Anthropic — stripping the prefix would give wrong prices.

### Per-component cost split (R1)

Cost is computed as:
```
(input_tokens × input_price
 + output_tokens × output_price
 + cache_read_tokens × cache_read_price
 + cache_write_tokens × cache_write_price
 + reasoning_tokens × reasoning_price) / 1_000_000
```

`prompt_tokens` is **intentionally ignored** — it equals `input + cache_read + cache_write`
in the Hermes canonical usage dict, so using it would double-count those components.

Cache prices are derived from multipliers when not explicit:
- `cache_read = input × 0.10` (configurable in YAML `defaults:`)
- `cache_write = input × 1.25`
- `reasoning` defaults to `output` price

### Cache-invalidation on hot-reload

`_custom_pricing` is a module-level `None`-initialized cache. `reload_custom_pricing()`
sets it back to `None`, forcing the next read to re-parse the YAML. This is
called by the auto-refresh cycle and by the `/setup` command, so new prices
take effect immediately without a gateway restart.

---

## Budget Engine

### The enforcement reality (A4 audit finding)

The budget engine was designed AFTER auditing what Hermes hooks can actually do.
The audit classified every enforcement primitive:

| Primitive | Blocks? | Mechanism |
|-----------|---------|-----------|
| `pre_llm_call` return | ❌ No | Context injection only |
| `pre_api_request` return | ❌ No | Return discarded |
| `pre_approval_request` / `post_approval_response` | ❌ No | "observers only" |
| `pre_tool_call` return `{"action":"block",…}` | ✅ Yes | Aborts tool, sends error to model |
| `cron.jobs.pause_job(job_id, reason)` | ✅ Yes (future runs) | Pauses cron job |

**Consequence:** there is NO true mid-call abort. When a hard breach is detected:
1. The current in-flight model response completes (and is billed).
2. The next tool call is blocked by `pre_tool_call`.
3. The agent loop eventually terminates because it can't execute tools.
4. For cron: the job is paused for future runs.

**Soft alert** (once per window): context injected via `pre_llm_call`.  
**Hard gate** (every tool call): `pre_tool_call` returns `{"action":"block",…}`.

### Verdict cache (TTL = 5s)

`pre_tool_call` fires on **every** tool call — potentially dozens per turn.
Re-querying SQLite on each call would be expensive. A 5-second TTL cache is
safe because spend only changes when a new `post_api_request` is recorded
(which can't happen while a tool call is running synchronously).

The cache is keyed by `(scope, scope_id)`. A `reload_config()` call clears it
(e.g., after `/budget set`).

### Anti-spam ledger (`budget_alerts` table)

Soft alerts fire **once per window per scope** — not on every `pre_llm_call`.
`try_budget_alert()` does `INSERT OR IGNORE` into `budget_alerts` and returns
`True` only on first insert (checked via `rowcount > 0`). The UNIQUE constraint
on `(scope, scope_id, window, period_key, level)` is the database-level guarantee.

### Verdict degradation

When spend rests on estimated rows (`estimated=1` or `_estimated_price: true`
models), a hard verdict **degrades to soft** under `on_estimated.mode: warn_only`
(the default). Rationale: you shouldn't block work based on numbers that might
be significantly wrong.

`on_estimated.mode: enforce` opts into strict enforcement regardless.

### Window math: local timezone

Daily/monthly windows are computed in the user's local timezone. A cron job at
11:59 PM and another at 12:01 AM count against different daily windows. The start
timestamp is converted back to UTC ISO before querying the DB (the DB stores UTC).

### Scope resolution

| Scope | How spend is aggregated |
|-------|------------------------|
| `global` | All `runs` rows in the window |
| `per_cron_job` | `runs WHERE cron_job_id = ?` — excludes subagent spend |
| `per_sender` | `runs WHERE sender_id = ?` |

`per_cron_job` deliberately excludes subagent spend — there's no way to
attribute it. Document this to users: use `global` for a cap that captures
delegated work.

---

## Free→Paid Model Transition Alert

When a model that was previously seen as free (explicit $0 in pricing) starts
incurring cost, the plugin injects a one-shot warning into the conversation so
the operator is aware of the change.

### Detection flow (live)

1. `post_api_request` fires with `cost == 0.0` AND `is_explicitly_priced(model,
   provider)` returns `True` → `record_free_model(model, provider)` inserts the
   pair into `known_free_models` (INSERT OR IGNORE).
2. A later `post_api_request` fires with `cost > 0.0`. The plugin checks
   `is_known_free_model(model, provider)` — if `True`, it queues a pending alert
   in `_pending_free_paid_alerts: dict[str, tuple[str, float]]` (module-level in
   `__init__.py`), mapping `session_id → (model, cost)`.
3. The next `pre_llm_call` for that session pops the pending alert and injects a
   one-shot warning into the conversation context. The alert fires exactly once per
   detection event (pop-on-consume).

`pre_llm_call` now handles two independent injection paths: budget alerts (one per
window per scope, anti-spam via `budget_alerts` table) and free→paid alerts (one
per detection event, state held in `_pending_free_paid_alerts`).

### Dashboard persistence (`free_paid_transitions`, schema v6)

The in-memory `_pending_free_paid_alerts` dict is ephemeral — consumed by
`pre_llm_call` and lost on plugin reload. To give the dashboard a historical
view of "which models flipped", every detection in `post_api_request` also
calls `db.record_free_paid_transition(model, provider, session_id, cost)`
which `INSERT OR IGNORE`s into the v6 `free_paid_transitions` table.
PRIMARY KEY `(model, provider)` — only the FIRST flip is recorded; later
paid calls on the same pair are no-ops. The dashboard endpoint
`GET /tier-transitions?window_hours=72` reads from this table (read-only,
`PRAGMA query_only=ON`). The widget (`TierTransitionsWidget`) is rendered
**inside `TelemetryPage`**, NOT via `registerSlot`: no shell slot in the
verified catalogue (`sessions:top`, `cron:top`, `header-right`,
`analytics:bottom`) fits a "tier change" surface, and registering an
unknown slot name is a silent no-op. The widget hides itself when no
transitions fall in the window, so the page is unchanged during the happy
path.

### `known_free_models` table schema

```
model       TEXT  — model identifier (as received from post_api_request)
provider    TEXT  — provider identifier, or '' for wildcard rows
first_seen_at TEXT — ISO-8601 UTC timestamp of first $0 observation
PRIMARY KEY (model, provider)
```

Every `(model, provider)` pair that is ever seen at an explicit $0 cost is
recorded here. A `provider=''` wildcard row is used for the backfill path (see
below).

### `is_known_free_model` lookup semantics

`is_known_free_model(model, provider)` returns `True` if either of these rows
exists in `known_free_models`:

- An exact `(model, provider)` row (live-recorded pair)
- A wildcard `(model, '')` row (backfill-seeded pair)

The wildcard covers sessions from before the model was first seen live, and
handles cases where the same model is served by multiple providers and all are
expected to be free.

### Backfill mechanism (pre-v5 upgrade path)

On every plugin load, `register()` calls `get_known_free_models()` (from
`pricing.py`) to retrieve all models with explicit `input=0.0` AND `output=0.0`
in the pricing table, then calls `backfill_known_free_models(models)` (from
`db.py`) which inserts `(model, provider='')` wildcard rows for each — INSERT OR
IGNORE, so it's safe to call on every load. Users who upgrade from pre-v5 get
immediate coverage without needing to replay historical sessions.

### `is_explicitly_priced` distinction

`is_explicitly_priced(model, provider) -> bool` in `pricing.py` returns `True`
only when the model has an explicit pricing entry in the pricing table (even if
that entry is $0). This distinguishes a genuine free model from an unknown model
that fell through to the `$0.00` unknown-model fallback. Without this guard, any
unrecognized model would be recorded as "free" and trigger spurious alerts when
the pricing table is later populated.

### Id-change transitions (`is_free_tier_transition`, issue #32)

A `known_free_models` row is keyed on the model id seen at $0. But some providers
move a model from free to paid by **changing the id** — dropping a `:free` suffix
(or renaming the promo to its paid base) — so the first paid call arrives under a
different id than the recorded `:free` row, and a plain `is_known_free_model`
lookup misses.

`is_free_tier_transition(model, provider)` in `db.py` bridges that gap. For an
incoming paid `model` it returns `True` if a stored `<id>:free` row matches via
either:

1. **Bare rename** — `model + ":free"` is a known-free row
   (`nvidia/nemotron-3-ultra` ← `nvidia/nemotron-3-ultra:free`).
2. **Suffixed paid id** — a stored `<base>:free` whose `<base>` is a prefix of
   `model` at a token boundary (`-`, `:`, `/`, `_`), so
   `nvidia/nemotron-3-ultra-550b-a55b` ← `nvidia/nemotron-3-ultra:free` but an
   unrelated `…-ultrablend` does not false-positive.

`post_api_request` checks `is_known_free_model(...) OR is_free_tier_transition(...)`
on the `cost > 0.0` branch. The concrete case: the `nvidia/nemotron-3-ultra:free`
promo ends 2026-06-18 and bills as `nvidia/nemotron-3-ultra`; the paid price is
seeded in `_DEFAULT_PRICING` so `cost > 0` once billing starts, and the reverse
lookup connects the paid id back to the recorded `:free` row to fire the alert.

**No user-side config is required** (changed by issue #32): while the promo is
live, the `:free` id resolves to `$0` via the `:free` suffix rule and is recorded
as known-free automatically — so the `:free` row exists for the reverse lookup to
match, with no `_subscription` entry needed. This holds for whichever free form
the gateway sends: the short `nvidia/nemotron-3-ultra:free` (matched on the paid
side by **bare rename**) and the OpenRouter long form
`nvidia/nemotron-3-ultra-550b-a55b:free` (matched by **bare rename** of its own
suffix-dropped paid id `…-550b-a55b`). An explicit `_subscription` entry is still
honored and overrides the rule, but is no longer necessary.

---

## Pricing Auto-Refresh

### Architecture

`PricingSource` is an ABC with a single `fetch() -> dict[str, dict]` method.
Two concrete sources ship:

| Source | Mechanism | Frequency |
|--------|-----------|-----------|
| `OpenRouterSource` | HTTP GET to `openrouter.ai/api/v1/models` (no auth) | Every 24h |
| `GoogleAISource` | Constant table in code (`LAST_VERIFIED = "2026-06-05"`) | Manual quarterly |

`GoogleAISource` is a constant because Google has no structured pricing API.
The `LAST_VERIFIED` date is an explicit reminder to bump it quarterly.

### Merge strategy

The `refresh_pricing()` function:
1. Loads existing `pricing.yaml`
2. Fetches all sources
3. **Never overwrites manual entries** — detects them via `_meta.auto_models`
   (a set of model IDs that were auto-fetched in a previous cycle)
4. Updates auto-fetched models when prices change
5. Writes `_meta.auto_models`, `_meta.estimated_price_models`, `_meta.last_refresh`

**Manual entry detection:** if a model is in `models:` but NOT in
`_meta.auto_models`, it's considered manual — preserved and logged as an
override warning if the remote price differs.

### Estimated-price models

OpenRouter represents models without fixed pricing (auto-routing, experimental)
with **negative prices** in their API. The plugin normalizes these to `$0.00`
and flags them with `_estimated_price: true`. The budget engine checks for these
models via `estimated_price_share()` and degrades hard verdicts under `warn_only`.

### Sentinel file

The 24h refresh cadence is tracked via a sentinel file at
`~/.hermes/telemetry/.pricing_refresh`. Touch it to force a refresh on next
plugin load. Delete it to trigger auto-refresh.

---

## Subagent Architecture

Hermes creates child `AIAgent` instances for `delegate_task`. Each child:
- Auto-generates its own `session_id` (format: `{YYYYMMDD_HHMMSS}_{uuid6}`)
- Calls `run_conversation()` which fires the full hook lifecycle
- Records its own `runs` row independently

**Verified finding (A3):** `delegate_task` result in `model_tools.py:999`
contains `{results:[{tokens, model, api_calls, status, ...}], total_duration_seconds}` —
**no child `session_id`**. `subagent_stop` also doesn't carry a child id.

**Consequence:** `/stats` global totals already include subagent tokens (they
appear as separate `runs` rows). But parent-child attribution is impossible from
hooks alone. `runs.parent_session_id` was added as a schema column for future
use, but it is **never populated** in any version through v0.4.0.

**Practical implication for budgets:** `per_cron_job` scope undercounts
delegated spend. Use `global` scope to cap total cost including subagents.

---

## Mixture of Agents (MoA)

MoA is a Hermes **virtual provider** (added upstream after v0.7). A named MoA
*preset* bundles:

- **`reference_models`** — a list of `{provider, model}` slots (proposers). They
  run first, without tools, on a trimmed advisory view of the conversation.
- **`aggregator`** — a single `{provider, model}` slot. It is the **acting
  model**: it writes the assistant response and emits tool calls, using the
  reference outputs as private context.

Config lives in `config.yaml` under `moa:` (`hermes_cli/moa_config.py`,
`resolve_moa_preset`). Verified against `NousResearch/hermes-agent@main`.

### How MoA surfaces to telemetry (the two-path problem)

There are **two** execution paths, and they behave differently:

| Path | Trigger | What runs | What fires `post_api_request` |
|------|---------|-----------|-------------------------------|
| **Preset-as-model** | `/model <preset> --provider moa` | `MoAClient` facade: references + aggregator via auxiliary `call_llm`; the aggregator response is returned as the main model response | **Once**, with `provider="moa"`, `model="<preset>"`, `response_model`=aggregator id, `usage`=**aggregator only** |
| **`/moa <prompt>` one-shot** | `decode_moa_turn` in `conversation_loop.py:827` | references + aggregator via auxiliary `call_llm`, output injected as context; then the **real main model** runs normally | fires for the **real main model**, not MoA |

**The hard constraint (verified):** reference-model and one-shot-aggregator
calls go through `agent/auxiliary_client.py::call_llm`, which contains **no hook
dispatch** (no `invoke_hook`). So:

- **Reference-model tokens are never captured** — same class of blind spot as
  subagent per-cron attribution. Unrecoverable from hooks.
- In the preset-as-model path, the **aggregator** *is* captured (it becomes the
  main response), but the hook reports `provider="moa"` and `model="<preset>"` —
  neither is a real, priceable identifier.

### What the plugin does (`moa.py`, schema v10)

`post_api_request` detects `provider == "moa"` and resolves the preset
(`model` kwarg = preset name) via `moa.resolve_preset()` →
`hermes_cli.moa_config.resolve_moa_preset` (reads Hermes' live config;
`load_config()` honors `HERMES_HOME`, so it respects test isolation). Then:

1. **Re-attribute provider.** The call is recorded under the aggregator's
   **real** provider (e.g. `openrouter`), not `moa`. Without this the
   provider-aware pricing guard rejects the aggregator's true rate and falls
   back to a noisy `provider_assumed` estimate.
2. **Re-attribute model.** `effective_model = response_model or <aggregator
   model> or model` — never the bare preset name.
3. **Tag the row.** `llm_calls.moa_preset = "<preset>"` and `runs.moa_calls++`
   (schema v10) so `/stats` and the dashboard can flag that the recorded cost is
   a **lower bound** (references untracked).

If the preset can't be resolved (no `hermes_cli`, unknown preset), the call
falls back to the raw hook values (`provider="moa"`) but is **still recorded**
and still tagged with the preset — never dropped, never crashes (the resolver
swallows every error).

### What is NOT done (and why)

- **Reference cost is not estimated.** No token counts are available for
  reference calls, and the display callbacks (`moa.reference`/`moa.aggregating`)
  route through `tool_progress_callback`, not a plugin hook. A rough opt-in
  estimate (aggregator input tokens × each reference's price) is a possible
  future phase, explicitly flagged `estimated=1`; deliberately deferred to keep
  the "real vs estimated" contract honest.
- **Budgets:** the aggregator cost counts as real spend (correct). Because it is
  now attributed to the aggregator's real provider, it is no longer
  `provider_assumed`, so it enforces normally instead of degrading.

---

## Cron Job Identification

There is **no `cron_job_id` kwarg** in any hook.

The extraction strategy: when `platform == "cron"`, the `session_id` follows
the format `cron_{job_id}_{YYYYMMDD_HHMMSS}` (confirmed in
`cron/scheduler.py:1392`).

**Anchored regex (R4):** `^cron_(?P<job_id>.+)_\d{8}_\d{6}$`

A naive `split("_")[1]` would break on job IDs that contain underscores
(e.g., `cron_my_job_2_20260101_120000` → `job_id` would be `my` instead of `my_job_2`).
The regex captures everything between the leading `cron_` and the trailing
`_{8digits}_{6digits}`.

On mismatch, a WARNING is logged and `cron_job_id` is set to NULL — surfaced
loudly rather than silently wrong. The comment `# If this test breaks...` is an
intentional canary.

---

## Test Isolation Contract

**Tests never read or write the real `~/.hermes`.** This is enforced, not just convention.

### The `isolate_hermes_home` fixture

An autouse pytest fixture in `conftest.py` redirects `HERMES_HOME` to a fresh
per-test temp directory. `HOME` and `USERPROFILE` are also pinned to the same
temp dir as a safety net for any `Path.home()` fallback.

### The poison-file test (`test_isolation.py`)

`test_isolation.py` plants a poisoned sentinel file in a decoy home directory and
asserts that no code path reads it. This is a fail-closed check: any new code that
reaches outside `HERMES_HOME` will break this test before it breaks production data.

**Rule for new code:** always locate Hermes files via `os.environ.get("HERMES_HOME",
Path.home() / ".hermes")`, never via `Path.home()` directly.

### Pricing fixture

`tests/fixtures/pricing.yaml` is a committed fixture used by pricing tests.
This prevents test results from depending on whatever `~/.hermes/telemetry/pricing.yaml`
your machine happens to have. New tests that need specific pricing data should
seed it in the per-test temp dir, not rely on real files.

### Where do tests write?

```
/tmp/pytest-of-<user>/pytest-<N>/<test_name><i>/telemetry/{telemetry.db,pricing.yaml,...}
```

pytest's built-in `tmp_path` fixture mints this per test. Override the base dir
with `pytest --basetemp=DIR`.

---

## Design Decisions by Version

### v0.1.0 — Initial scaffold

- 9 hooks registered (no `subagent_stop` yet).
- SQLite schema v1: `runs`, `llm_calls`, `tool_calls`.
- `post_api_request` identified as the primary token hook (not `post_llm_call`).
- Cron ID anchored-regex extraction (R4) decided upfront.

### v0.2.0 — Budget + Dashboard

- **Budget engine** added. The A1-A4 audit (see [The Hermes Plugin Model](#the-hermes-plugin-model) above) drove the design:
  tool-gate is the only real enforcement mechanism.
- **Schema v2 migration:** added `cache_read_tokens`, `cache_write_tokens`,
  `reasoning_tokens`, `estimated` on `llm_calls`; `parent_session_id`,
  `estimated_llm_calls` on `runs`.
- **Subagent stop** hook added. Decision: record proxy `tool_call` row only,
  never attempt to attribute tokens (would double-count).
- **`budget_alerts` table** (schema v3 migration): anti-spam ledger for
  one-time-per-window alerts.
- **Dashboard:** stdlib HTTP server, zero external deps. `--host 0.0.0.0` flag
  for headless deployments, with explicit warning on non-loopback binding.
- **Provider label = verbatim:** stored as-is from `post_api_request.provider`.
  NOT normalized. Everything routed through OpenRouter shows as "openrouter".
  This was a deliberate decision — the routing path is meaningful observability data.

### v0.3.0 — Setup wizard

- **Auto-setup on first load:** if `pricing.yaml` or `budget.yaml` are missing,
  the plugin generates defaults non-interactively. Guarded by
  `HERMES_TELEMETRY_NO_SETUP=1` for CI/tests.
- **`/setup` slash command:** three modes — `auto` (fetches OpenRouter), `minimal`
  (built-in defaults only), `skip`.
- **Pricing auto-refresh** from OpenRouter API. `PricingSource` ABC for
  extensibility. `_meta.auto_models` to detect and preserve manual overrides.

### v0.3.1 — Gemini pricing + orphan-run fix

- **Gemini 3.x/2.5 family added** to `_DEFAULT_PRICING` and `_PREFIX_PRICING`.
- **Deprecated Gemini entries removed**: `gemini-1.5-*`, `gemini-2.0-*` (sunset).
- **Generic `gemini` prefix removed** from `_PREFIX_PRICING` (was Flash 1.5
  pricing for any unknown Gemini variant — silently mis-priced new models by
  ~6.5×). Unknown Gemini variants now surface as `unknown-model` warnings.
- **`_ensure_run_row` lazy insert (orphan-run fix):** sessions that join a
  running gateway after the plugin loads never receive `on_session_start`.
  All subsequent UPDATE calls were silent no-ops. Fix: `INSERT OR IGNORE`
  stub row before every UPDATE in `record_llm_call` and `end_run`.

### v0.4.0 — Google AI pricing source + symmetric lookup

- **`GoogleAISource`** in `pricing_refresh.py`: direct Google AI Studio pricing
  as a constant table (no structured API). `LAST_VERIFIED` date for manual
  quarterly refresh. Registered alongside `OpenRouterSource`.
- **Symmetric Google lookup** in `pricing._lookup_base`: `gemini-X` and
  `google/gemini-X` resolve to the same entry. Two-pass: try the literal ID
  first, then `_google_alt_form(model_lc)`. This is deliberately Google-specific
  — other provider prefixes carry distinct pricing semantics and must not be stripped.

### v0.4.1 — Provider-aware pricing + NVIDIA NIM seeds

- **Provider-aware lookup guard** (issue #24): `estimate_cost` gains an optional
  `provider` arg threaded through `_resolve_pricing` → `_lookup_base` →
  `_lookup_form`. `_source_eligible` rejects an `_source: openrouter` entry for a
  non-OpenRouter call so the OpenRouter rate never leaks onto a Nous/NIM call.
  `provider=""` preserves the old provider-blind behaviour (backward compatible).
  See [Provider-aware lookup guard](#provider-aware-lookup-guard-added-v041-issue-24).
- **`_subscription` tag** (Option A for Nous flat-sub): declared $0, kept distinct
  from a lookup miss and from `_estimated_price`. Tracked in
  `_meta.subscription_models`.
- **NVIDIA NIM seeds** (issue #12 Phase 1): five Nemotron models added to
  `_DEFAULT_PRICING` (source-neutral, immune to OpenRouter sync). The same-id
  OpenRouter collision is resolved by the guard falling through to the seed.
  Phase 2 (`NVIDIANIMPricingSource` API auto-sync) was deliberately dropped — NIM
  bills against account tier, not a uniform public per-token list, so the seed
  table is the durable answer.
- **`_SCHEMA_VERSION` bump 3→4**: the `_migrate_v4` migration (cache tokens on
  `runs`) shipped without bumping the constant, leaving `test_schema_idempotent`
  red. Fixed here as part of documenting schema v4.

### v0.5.0 — Free→paid model transition alert (issue #16, Block A)

- **`known_free_models` table** (schema v5): records every `(model, provider)`
  pair seen at explicit $0 cost. INSERT OR IGNORE — append-only, never deleted.
- **`is_explicitly_priced(model, provider)`** in `pricing.py`: distinguishes a
  genuine $0 entry from an unknown-model `$0.00` fallback. Without this, every
  unrecognized model would be flagged as "free" and generate spurious alerts.
- **`record_free_model` / `is_known_free_model`** in `db.py`: write and read
  the `known_free_models` table respectively. `is_known_free_model` checks both
  the exact `(model, provider)` row and a wildcard `(model, '')` row.
- **Backfill on load**: `register()` seeds `known_free_models` with `provider=''`
  wildcard rows for all explicitly-$0 models from the pricing table. INSERT OR
  IGNORE, runs every load. Covers pre-v5 installs automatically.
- **`_pending_free_paid_alerts`** (module-level `dict` in `__init__.py`): maps
  `session_id → (model, cost)`. Populated by `post_api_request` when a
  previously-free model is seen at cost > 0. Consumed (popped) by `pre_llm_call`.
- **`pre_llm_call`** now handles two injection paths: budget soft-alerts (anti-spam
  via `budget_alerts` table) and free→paid one-shot alert (pop-on-consume from
  `_pending_free_paid_alerts`). Both inject via the `{"context": "..."}` return.
- **253 tests** (was 233): `test_init.py` gained free→paid detection, queueing,
  injection, and backfill tests; `test_db.py` and `test_pricing.py` gained
  corresponding unit tests.

### Unreleased — Free→paid id-change handling (issue #32)

- **`is_free_tier_transition(model, provider)`** in `db.py`: reverse-looks-up a
  stored `<id>:free` row when a provider moves a model to paid under a *different*
  id (dropped `:free` suffix or suffixed paid id at a token boundary). Wired into
  `post_api_request` as `is_known_free_model(...) OR is_free_tier_transition(...)`.
- **`:free` suffix → $0 rule** in `_lookup_form`: any `…:free` id resolves to an
  explicit `$0` before the prefix scan (see *Lookup priority chain*). This both
  (a) stops a seeded model's `:free` variant from being mis-billed at its paid rate
  via prefix, and (b) records the `:free` id as known-free with no estimated-price
  warning — so the transition alert seeds itself with **no user config**. This
  *replaced* the earlier design's manual-`_subscription` requirement. It also
  surfaced and fixed a latent bug: `nvidia/nemotron-3-super-120b-a12b:free` (a
  pre-existing 0.4.1 seed) had been resolving to the paid `$0.09/$0.45` rate.
- **`nvidia/nemotron-3-ultra` paid seed** in `_DEFAULT_PRICING` (OpenRouter rate,
  NIM-direct pending). Catches the bare id (exact) and the `…-550b-a55b` form
  (prefix), making `cost > 0` after the 2026-06-18 promo end — which fires the
  alert. The free `…:free` forms are unaffected (handled by the suffix rule above).
- **263 tests** (was 253): `test_db.py` gained 7 `is_free_tier_transition` cases;
  `test_pricing.py` gained the ultra paid-seed + `:free`-suffix → $0 (bare and
  OpenRouter-long-form), `super:free` regression, and explicit-override cases.

### v0.8.0 — Mixture-of-Agents (MoA) attribution

- **`moa.py`** (new module): resolves the `provider="moa"` virtual-provider
  preset to its aggregator's real `provider`/`model` via
  `hermes_cli.moa_config.resolve_moa_preset`. Defensive — swallows every error
  and falls back to the raw hook values.
- **`post_api_request`** now detects MoA calls and records them under the
  aggregator's real provider/model (fixing the `provider_assumed` misfire) and
  tags the row with the preset name.
- **Schema v10** (`_migrate_v10`): `llm_calls.moa_preset` + `runs.moa_calls`.
- **Reference-model tokens remain uncaptured** (auxiliary `call_llm` fires no
  hooks) — documented as a Known Limitation; MoA cost is a lower bound and is
  flagged as such in `/stats` and both dashboards.
- Verified against `agent/moa_loop.py`, `agent/auxiliary_client.py`,
  `agent/agent_init.py`, `agent/conversation_loop.py`, `hermes_cli/moa_config.py`.

---

## Metrics: Real vs Estimated

| Metric | Status | Source |
|--------|--------|--------|
| Tokens in (non-cached) | ✅ Real | `post_api_request.usage.input_tokens` |
| Tokens out | ✅ Real | `post_api_request.usage.output_tokens` |
| Cache read tokens | ✅ Real | `post_api_request.usage.cache_read_tokens` |
| Cache write tokens | ✅ Real | `post_api_request.usage.cache_write_tokens` |
| Reasoning tokens | ✅ Real | `post_api_request.usage.reasoning_tokens` |
| API call latency | ✅ Real | `post_api_request.api_duration` (seconds → ms) |
| Tool call latency | ✅ Real | `post_tool_call.duration_ms` |
| Model name | ✅ Real | `post_api_request.response_model or model` |
| Provider name | ✅ Real (verbatim) | `post_api_request.provider` |
| Platform | ✅ Real | `on_session_start.platform` |
| Cron job ID | ✅ Real (parsed) | `session_id` regex extraction |
| Session duration | ✅ Real (wall time) | `started_at` → `ended_at` (last turn) |
| Tool success/failure | ✅ Real | Parse `result` JSON for `"error"` key |
| Subagent count | ✅ Real (proxy) | `subagent_stop` hook count |
| Cost (USD) | ⚠️ Estimated | Local pricing table × token counts |
| Tokens when `usage=None` | ⚠️ Estimated, flagged | `approx_input_tokens + chars/4`, row marked `estimated=1` |
| Subagent cost (global) | ✅ Real | Child runs fire own hooks → independent `runs` rows |
| Subagent cost (per-cron-job) | ❌ Not available | No parent→child link in any hook |
| MoA aggregator cost | ✅ Real (re-attributed) | Aggregator usage from `post_api_request`, priced under the aggregator's real provider/model resolved from the preset (`moa.py`) |
| MoA reference-model cost | ❌ Not available | References run via auxiliary `call_llm`, which fires no hooks — tokens unrecoverable (recorded MoA cost is a lower bound) |

**Cost is always an estimate** — computed from a local pricing table, not from
a provider billing API. Users can override prices via `~/.hermes/telemetry/pricing.yaml`.

---

## CI/CD Pipeline

### CI (`.github/workflows/ci.yml`)

Runs on push and PR to `main`. Jobs:

1. **`lint`** — `ruff format --check .` then `ruff check .` (Python 3.12)
2. **`test`** — `pytest tests/ -v --tb=short`, matrix Python 3.8–3.12.
   `needs: lint` so a format miss fails the whole matrix.

Local equivalent (matches CI exactly):
```bash
ruff format --check . && ruff check . && pytest tests/ -v
```

### Release (`.github/workflows/release.yml`)

Triggered on push of a `v*.*.*` tag. Steps:
1. Verify that the tag version matches `pyproject.toml` `[project].version`.
   If they differ, the release fails. Both `__init__.py.__version__` and
   `plugin.yaml.version` must also be bumped (this check only covers `pyproject.toml`
   vs the tag — see RELEASING.md for the full checklist).
2. Create GitHub Release (no build artifact — Hermes installs directly from the repo).

### Pre-commit hook

`.githooks/pre-commit` runs the same three checks (format + lint + tests).
Enable with `git config core.hooksPath .githooks`.

---

## Known Limitations

### Enforcement gaps

- **No true mid-call abort.** The in-flight response completes and is billed.
  The tool-gate stops *further* work, not the current call.
- **Text-only sessions.** A session generating only text (no tool calls) never
  hits `pre_tool_call`. No enforcement possible. A `pre_llm_call` abort for
  cron jobs would require Hermes to honor it (it currently doesn't).
- **`on_estimated.mode: warn_only` is the default.** Hard verdicts based on
  estimated data degrade to soft. Users with reliable providers should set
  `mode: enforce`.

### Subagent attribution

- `per_cron_job` budgets undercount delegated spend. The `global` budget is the
  only reliable cap for total cost including subagents.
- `runs.parent_session_id` is in the schema but never populated. If Hermes adds
  a `parent_session_id` kwarg to `on_session_start` in the future, the column
  is ready.

### MoA reference-model tokens

- A MoA turn runs N reference models plus the aggregator per iteration, but only
  the **aggregator** fires a hook. Reference-model tokens are never captured
  (auxiliary `call_llm` fires no hooks), so a MoA session's recorded cost is a
  **lower bound**. `/stats` and the dashboard flag MoA calls (`moa_calls` /
  `moa_preset`) so the gap is visible. See `§ Mixture of Agents (MoA)`.

### MoA one-shot (`/moa <prompt>`) is entirely invisible — by Hermes' design

- The `/moa` slash command (the one-shot path, `decode_moa_turn` →
  `conversation_loop.py:827`) runs **both** the reference models **and** the
  aggregator through the auxiliary `call_llm` path, then injects their synthesis
  as context into the **real** main model call. Only that final main-model call
  fires `post_api_request`. So on this path telemetry records **zero** MoA
  cost — not even the aggregator — and the `moa_calls` / `moa_preset` markers are
  never set. This is not a plugin gap we can close: no hook fires for either MoA
  call type here (verified against `agent/moa_loop.py::aggregate_moa_context`
  and `agent/auxiliary_client.py`, which has no hook dispatch).
- The re-attribution in v10 (`moa.py`) therefore applies **only** to the
  preset-as-model path (`/model <preset> --provider moa`), where the aggregator
  *is* the returned main response and does fire the hook. Selecting a preset as
  your model is the only way MoA usage reaches telemetry at all.

### Pricing

- Auto-refresh covers OpenRouter models (via API) and Google AI Studio (static
  table). Anthropic, OpenAI direct-API pricing requires manual `pricing.yaml`
  entries or a new `PricingSource` subclass.
- `GoogleAISource` is a constant table. It will drift without manual quarterly
  updates. See `LAST_VERIFIED` in `pricing_refresh.py::GoogleAISource`.
- Gemini tiered-pricing models (`gemini-2.5-pro`, `gemini-3.1-pro-preview`) use
  the ≤200k context tier. Usage above 200k is undercounted.

### DB retention

`telemetry.db` grows without bound. No automatic purge. For >100K rows, consider
manual cleanup.

---

## User Config Files

```
~/.hermes/telemetry/
├── telemetry.db          ← SQLite (WAL, schema v5)
├── telemetry.log         ← Plugin log (DEBUG+, includes one-time warnings)
├── pricing.yaml          ← User price overrides + auto-refreshed models
├── budget.yaml           ← Guardrails config
└── .pricing_refresh      ← Sentinel: mtime = last successful refresh
```

All paths derived from `os.environ.get("HERMES_HOME", Path.home() / ".hermes")`.
**Never use `Path.home()` directly** — breaks test isolation.

---

## PluginContext API

```python
ctx.register_hook(hook_name: str, callback: Callable) -> None
ctx.register_command(name: str, handler: Callable, description: str = "", args_hint: str = "") -> None
ctx.register_cli_command(
    name: str,
    help: str,
    setup_fn: Callable,
    handler_fn: Callable | None = None,
    description: str = "",
) -> None
ctx.register_tool(name, toolset, schema, handler, ...) -> None
```

Handler signature for slash commands: `fn(raw_args: str) -> str | None`
(sync or async — both supported).

### `register_cli_command` details (verified against `hermes_cli/plugins.py`)

Creates a `hermes <name> ...` terminal subcommand (distinct from in-session slash
commands, which use `register_command`).

- **`setup_fn`** — receives an `argparse` subparser (the `ArgumentParser` object added
  by `add_subparsers().add_parser(name, ...)`). Use it to call `.add_argument()` or
  add further sub-subparsers. Return value is ignored.
- **`handler_fn`** — if provided, registered via `subparser.set_defaults(func=handler_fn)`.
  When Hermes dispatches the command it calls `args.func(args)`, so `handler_fn`
  receives the parsed `argparse.Namespace`. If `None`, the caller is expected to wire
  `set_defaults(func=...)` itself inside `setup_fn`.
- **`description`** — optional long description stored in the command registry; not
  passed to argparse automatically.
- **Return value** — `None`. Registers metadata in `_manager._cli_commands[name]`;
  does not immediately add to the live parser (the manager wires parsers at startup).

---

## Valid Hooks Reference

All hooks available in `VALID_HOOKS` (`hermes_cli/plugins.py`):

```
pre_tool_call, post_tool_call, transform_terminal_output, transform_tool_result,
transform_llm_output, pre_llm_call, post_llm_call, pre_api_request, post_api_request,
api_request_error, on_session_start, on_session_end, on_session_finalize,
on_session_reset, subagent_start, subagent_stop, pre_gateway_dispatch,
pre_approval_request, post_approval_response
```

Hooks not used (and why):
- `transform_*` — output mutation, not needed for telemetry
- `on_session_reset` — fired by `/reset`; would be useful for clearing session
  state without restart, but not yet wired
- `subagent_start` — only `subagent_stop` is consumed (no token data on start)
- `pre_gateway_dispatch` / `pre_approval_request` / `post_approval_response` —
  documented as "observers only"; cannot block or modify

### Plugin Discovery Gotcha — duplicate `name` in `~/.hermes/plugins/` silently shadows

Hermes' loader (`hermes_cli/plugins.py::discover_plugins`) recursively scans every
subdirectory of `~/.hermes/plugins/` for a `plugin.yaml`, parses each one, and
indexes them by their declared `name`. **Two directories whose manifests share the
same `name` collide on that key — the later one parsed wins, the earlier one is
dropped without any warning or error.** Filesystem order decides who "wins"
(alphabetical in practice), so a backup directory left next to the active plugin
will usually shadow it.

This bit us with `api_request_error` (issue #43, PR #44). The server had:

```
~/.hermes/plugins/
├── hermes-telemetry/                    ← current (v0.7.0, has the fix)
└── hermes-telemetry.bak.1781730291/     ← old backup, both declare name: hermes-telemetry
```

The `.bak` was loaded instead of the active dir. Its older `__init__.py` did not
call `register_hook("api_request_error", ...)`, so `has_hook("api_request_error")`
returned False and the dispatcher in `run_agent.py::_invoke_api_request_error_hook`
silently short-circuited on every 404. Editing `plugin.yaml` or `__init__.py` in
the active dir had zero effect — Hermes was reading the other dir entirely.

**Rules of thumb:**
- Never keep a backup of a plugin **inside** `~/.hermes/plugins/`. Move it out
  (`mv ~/.hermes/plugins/foo.bak ~/foo.bak`) or rename its `name:` in the manifest
  so it gets a distinct key.
- The `provides_hooks` list in `plugin.yaml` is **declarative only** — the loader
  does not filter registrations against it. Keep it accurate anyway so the
  manifest matches the code, but do not rely on adding/removing entries there to
  enable/disable a hook.
- To verify which path Hermes is actually loading, run with debug logging and
  look for the `Loading plugin '<name>' ... path=<dir>` line:

```bash
cd ~/.hermes/hermes-agent && python3 -c "
import logging; logging.basicConfig(level=logging.DEBUG)
from hermes_cli import plugins as p; p.discover_plugins()
print('api_request_error registered:', p.has_hook('api_request_error'))
" 2>&1 | grep -E "Loading plugin 'hermes-telemetry'|api_request_error registered"
```

### `api_request_error` — model-unavailable detection (issue #43)

Fires at the moment of a non-retryable client error from
`agent/conversation_loop.py`, before the exception is diluted into the
`RuntimeError` that surfaces in `cron.scheduler`. Verified kwargs:

| kwarg            | Notes                                                                |
|------------------|----------------------------------------------------------------------|
| `session_id`     | The active session                                                   |
| `api_kwargs`     | Dict; the requested model id lives in `api_kwargs["model"]`          |
| `error_type`     | Exception class name, e.g. `"NotFoundError"` for a 404               |
| `error_message`  | Full message including the status code and the model id              |
| `status_code`    | HTTP status (404 for model-not-found)                                |
| `retryable`      | False for 404s                                                       |
| `reason`         | Classifier enum, e.g. `"model_not_found"`                            |
| `retry_count`, `max_retries`, `task_id`, `turn_id`, `api_request_id`, `api_call_count`, `api_start_time` | Other context fields, kept via `**_kw` |

The plugin filters to `status_code == 404 AND retryable is False` and:

1. Upserts `model_unavailable_alerts` keyed on `(model, provider)`. Repeated
   404s bump `occurrences` and refresh `last_seen_at`; `first_seen_at` is
   preserved.
2. Queues a pending entry in `_pending_model_unavailable_alerts[session_id]`
   so the next `pre_llm_call` injects a one-shot warning that names the
   model, provider, status, and occurrence count.

Sibling to free→paid: same family of provider-side changes (deprecation,
`:free` promo end) but the call fails entirely instead of just billing.

---

*Source references verified against `git clone --depth=1 https://github.com/NousResearch/hermes-agent`.*  
*Inspected: `hermes_cli/plugins.py`, `agent/conversation_loop.py`, `model_tools.py`,*
*`cron/scheduler.py`, `tools/delegate_tool.py`, `agent/usage_pricing.py`.*

---

## Dashboard Plugin Surface

In addition to the standalone HTML dashboard (`dashboard/serve.py`, port 8765),
`hermes-telemetry` also ships as a plugin for the official Hermes web
dashboard. The two surfaces co-exist; they share the SQLite DB but **zero
Python code**.

### File layout (verified against the Hermes loader)

The Hermes dashboard discovers plugins by scanning
`~/.hermes/plugins/<name>/dashboard/manifest.json`
(`hermes_cli/web_server.py::_discover_dashboard_plugins`, lines 11333-11434
of `NousResearch/hermes-agent@main`). Because this repo is cloned into
`~/.hermes/plugins/hermes-telemetry/`, the plugin files MUST live inside
`dashboard/` — the same directory as `serve.py` and `index.html`. The
discovery rule is non-negotiable.

```
dashboard/
├── serve.py         ← standalone surface (stdlib http.server, port 8765)
├── index.html       ← standalone SPA (Chart.js via CDN)
├── manifest.json    ← plugin manifest
├── plugin_api.py    ← plugin backend (FastAPI APIRouter; loaded by Hermes)
└── dist/index.js    ← plugin frontend (IIFE; no build step)
```

### Why a single file for the plugin backend

The Hermes loader imports `plugin_api.py` via
`importlib.util.spec_from_file_location(module_name, api_path)`
(`hermes_cli/web_server.py:11856-11881`). The module is **not** registered
as part of any package, so relative imports (`from . import _db`) fail at
load time. Hence `plugin_api.py` is self-contained — all DB helpers,
budget math, and FastAPI routes live in one file. No `_db.py` sibling.

### Why the standalone and plugin share no code

The user contract is: the standalone dashboard MUST keep working unchanged
when the plugin evolves. Co-locating in `dashboard/` was forced by the
loader, but the two are independent products:

| Surface | Entry point | Server | Auth |
|---------|-------------|--------|------|
| Standalone | `dashboard/serve.py` | stdlib `BaseHTTPRequestHandler` | none (loopback default) |
| Plugin | `dashboard/plugin_api.py` | Hermes FastAPI app | Hermes session cookie |

`tests/test_dashboard_plugin_isolation.py` enforces:
- `plugin_api.py` does not import `serve` (in any module form).
- `serve.py` does not import `plugin_api`.
- No third Python file in `dashboard/` is imported by both surfaces (would
  re-couple them).
- Manifest `version` equals the package `__version__` (lockstep release).

### Read-only DB access

`plugin_api.py` opens `telemetry.db` with `PRAGMA query_only=ON`. The
plugin is observability-only — capture still flows through the runtime
hooks (`__init__.py`). The test `test_db_connection_is_read_only` asserts
that any `INSERT` via the plugin connection raises `OperationalError`.

### Manifest contract

```json
{
  "name": "hermes-telemetry",
  "label": "Telemetry",
  "icon": "Activity",
  "version": "<must equal __version__>",
  "tab": { "path": "/telemetry", "position": "after:analytics" },
  "slots": ["sessions:top", "cron:top", "header-right", "analytics:bottom"],
  "entry": "dist/index.js",
  "api": "plugin_api.py"
}
```

Verified fields:
- `tab.position` accepts `"end"`, `"after:<path>"`, `"before:<path>"`
  (`web_server.py:11382-11391`).
- `slots` is **documentation only**. The real binding happens in the JS
  bundle via `window.__HERMES_PLUGINS__.registerSlot(...)`
  (extending-the-dashboard.md, line 696).
- `api` is validated by `_safe_plugin_api_relpath` (`web_server.py:11296-11330`);
  absolute paths or `..` traversal cause backend mount to be skipped (the
  static assets still load). This is fix for GHSA-5qr3-c538-wm9j.

### Slot widgets

Page-scoped slots render only on the named built-in page. The slot
catalogue (`extending-the-dashboard.md:590-600`) was verified against
source; the four slots we register are:

| Slot | Widget |
|------|--------|
| `sessions:top` | Last-run summary card (cost · tokens · model). |
| `cron:top` | 7-day cron cost + failure badge. |
| `header-right` | 24h spend + budget level (variant=destructive on hard breach). |
| `analytics:bottom` | Daily cost line chart (Chart.js via CDN, graceful degradation). |

### Slot names are NOT free-form — verify before adding new ones

The shell only renders slots whose names appear in its catalogue
(`extending-the-dashboard.md:590-600`). Registering an unknown slot via
`registerSlot()` is a silent no-op: the widget loads but nothing on the
page ever mounts it. **Do not invent slot names** — `alerts:top`,
`warnings:top`, etc. do not exist. The four above are the entire
verified catalogue as of this writing.

If you need a new visible surface and none of the four fit, render the
widget **inside `TelemetryPage`** (the plugin's own tab) instead — that
page is fully under our control. The free→paid transitions widget is
rendered this way (see `dist/index.js`, `TelemetryPage`), not via
`registerSlot`, precisely because no shell slot fit.

When new slots are added upstream, re-verify the catalogue at
`https://raw.githubusercontent.com/NousResearch/hermes-agent/main/docs/extending-the-dashboard.md`
and update this table.

The SDK does not currently expose an `useActiveSession` hook, so
`sessions:top` shows the most recent run instead of the per-row session.
When that hook lands upstream, swap the implementation — the backend
endpoint `/session/{session_id}` is already in place.

### Chart.js delivery

Chart.js is loaded on-demand from `cdn.jsdelivr.net/npm/chart.js@4` inside
the IIFE. If the CDN is blocked, `analytics:bottom` renders a degraded
"Chart.js unavailable" message and links the user to the tabular view in
the `/telemetry` tab. We deliberately do not bundle Chart.js — no build
step is the right answer for a hand-edited IIFE.

### Update path

Both surfaces are upgraded with a single `git pull` in
`~/.hermes/plugins/hermes-telemetry`. The manifest version is pinned to
`__version__` by `test_plugin_version_matches_package`, so a release tag
implicitly ships both surfaces in lockstep.
