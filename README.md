# hermes-telemetry

Observability + budget guardrails for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Captures tokens, estimated cost, latency, and tool usage per session and cron job. Persists to local SQLite. Exposes `/stats` and `/budget` slash commands.

**Design principle:** observability is invisible to the model. Everything goes through hooks. The only user-facing surface is `/stats` and `/budget`.

---

## Table of Contents

- [What It Measures](#what-it-measures)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Slash Commands](#slash-commands)
  - [/stats](#stats)
  - [/budget](#budget)
- [Configuration](#configuration)
  - [pricing.yaml](#pricingyaml)
  - [budget.yaml](#budgetyaml)
- [Architecture](#architecture)
  - [Hook Pipeline](#hook-pipeline)
  - [Database Schema](#database-schema)
  - [Concurrency Model](#concurrency-model)
- [Budget Enforcement](#budget-enforcement)
  - [How It Works](#how-it-works)
  - [Enforcement Levels](#enforcement-levels)
  - [Estimated Data and Budget Degradation](#estimated-data-and-budget-degradation)
- [Provider Probe: Verifying Your Provider](#provider-probe-verifying-your-provider)
- [Proof of Concept](#proof-of-concept)
  - [Setup](#setup)
  - [Pricing Capture](#pricing-capture)
  - [Budget Enforcement Test](#budget-enforcement-test)
  - [Cron Job Cost Comparison](#cron-job-cost-comparison)
  - [Results Summary](#results-summary)
- [Running Tests](#running-tests)
- [Data Location](#data-location)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What It Measures

| Metric | Source | Real or Estimated |
|--------|--------|-------------------|
| Tokens in / out per API call | `post_api_request.usage` | ✅ Real (from provider) |
| Cache read / write tokens | `post_api_request.usage` | ✅ Real (from provider) |
| Reasoning tokens | `post_api_request.usage` | ✅ Real (from provider) |
| API call latency | `post_api_request.api_duration` | ✅ Real (ms) |
| Tool call latency & success/failure | `post_tool_call` | ✅ Real |
| Session / cron job wall time | `started_at` → `ended_at` | ✅ Real |
| Model & provider name | `post_api_request` | ✅ Real |
| Platform (cli / cron / telegram / …) | `on_session_start.platform` | ✅ Real |
| Cron job ID | Parsed from `session_id` | ✅ Real |
| Subagent invocation count | `subagent_stop` hook | ✅ Real (proxy) |
| **Cost (USD)** | Local pricing table × tokens | ⚠️ **Estimated** |
| Tokens when provider returns `usage=None` | Fallback approximation | ⚠️ **Estimated, flagged** |

Cost is always an **estimate** computed from a locally-maintained pricing table. No external pricing API is called. When the provider returns no usage data, tokens are estimated from a pre-request approximation + response length and the row is flagged as `estimated=1`, so `/stats` and `/budget` show a `~` prefix and an "estimated data" percentage.

---

## Installation

Hermes plugins are **opt-in** — you must both install and enable the plugin.

### Option A: Install from GitHub

```bash
hermes plugins install nujovich/hermes-telemetry
hermes plugins enable hermes-telemetry
```

### Option B: Manual install

```bash
git clone https://github.com/nujovich/hermes-telemetry ~/.hermes/plugins/hermes-telemetry
hermes plugins enable hermes-telemetry
```

**Important:** restart the Hermes gateway after enabling:

```bash
hermes gateway restart
```

> **Note:** Plugin changes only take effect after a gateway restart. The gateway loads the plugin registry at startup. If you enable a plugin and cron jobs don't appear in `/stats cron week`, this is the most likely cause.

---

## Quick Start

1. Install and enable the plugin (see above)
2. Restart the gateway
3. Run any session, then type `/stats` to see captured data
4. Optionally configure `pricing.yaml` and `budget.yaml` (see below)

That's it. The plugin captures data automatically — no agent action required.

---

## Slash Commands

### `/stats`

```
/stats                  → last 24h summary (sessions, tokens, cost, top tools)
/stats today            → same as /stats
/stats week             → last 7 days
/stats month            → last 30 days
/stats cron             → breakdown by cron_job_id (last 7 days)
/stats cron week        → cron breakdown, last 7 days
/stats cron month       → cron breakdown, last 30 days
/stats cron today       → cron breakdown, last 24 hours
/stats providers        → per-provider: real vs estimated calls + cost (last 24h)
/stats providers week   → provider breakdown, last 7 days
/stats raw [N]          → last N raw run records (default 20, max 200)
```

**Example output (`/stats`):**

```
hermes-telemetry — last 24 h
============================================
  Sessions      : 14
  Success rate  : 92.9%  (ok=13, failed=1)
  API calls     : 47
  Tool calls    : 183
  Tokens in     : 1,240,500
  Tokens out    : 87,300
  Cost (est.)   : $0.004822
  Avg latency   : 1.2s
  Avg duration  : 48.3s

  Top tools:
  Tool                            Calls  Failures   Avg ms
  --------------------------------------------------------
  read_file                          92         0      12ms
  terminal                            51         3     340ms
  write_file                         28         0      18ms
```

**Example output (`/stats cron week`):**

```
hermes-telemetry — cron jobs (last 7 days)
========================================================================
  Job ID               Runs    OK  Fail     Tok-in    Tok-out         Cost   Avg dur
  --------------------------------------------------------------------------
  09dd0c24f29b            3     3     0   892,341    12,405    $0.314378     2.1m
  d68c2728b513            1     1     0   445,119     8,200    $2.225595     4.7m
```

**Example output (`/stats providers`):**

```
hermes-telemetry — providers (last 24 h)
========================================================================
  Provider                     Calls   Real   Est   Est%         Cost
  -------------------------------------------------------------------
  openrouter                      66     66      0     0%    $0.916782

  Est% = share of calls where the provider returned no usage data
  (tokens estimated locally).
  If Est% > 0 for your main provider, budget hard-verdicts may be
  degraded to soft under on_estimated.mode: warn_only.
```

### `/budget`

```
/budget                             → status of every scope (spent / limit / %)
/budget cron                        → per-cron-job budgets, with soft/hard flags
/budget set global daily 5.00       → set or raise a limit (persists + hot-reloads)
/budget set cron_job daily 1.00     → set default per-cron-job limit
/budget set sender daily 2.00       → set default per-sender limit
```

**Example output (`/budget`):**

```
hermes-telemetry — budget status
============================================================
  global                       $   0.1812 / $    2.00      9%  [daily]

  Legend:  (blank)=ok  !=soft (≥80%)  █=hard (≥100%)  ~est=estimated data
```

**Status flags:**

| Flag | Meaning |
|------|---------|
| (blank) | Within budget (`< 80%`) |
| `!` | Soft warning (≥ 80%) — notice injected into conversation |
| `█` | Hard breach (≥ 100%) — tool calls blocked, cron jobs paused |
| `~est` | Verdict based partly on estimated (usage=None) data |

---

## Configuration

Configuration lives in `~/.hermes/telemetry/`:

```
~/.hermes/telemetry/
├── telemetry.db      ← SQLite database (WAL mode)
├── telemetry.log     ← plugin log (errors / debug)
├── pricing.yaml      ← optional pricing overrides
└── budget.yaml       ← optional spend budgets
```

If these files don't exist, the plugin still works — it just uses defaults (all models at $0.00, budgets disabled).

### `pricing.yaml`

Override model prices in USD per 1 million tokens. Without overrides, unknown models log a one-time warning and record cost as `$0.00`.

**Full format:**

```yaml
models:
  # Free model
  "openrouter/owl-alpha":
    input: 0.00
    output: 0.00

  # Paid model with full cache/reasoning split
  "openrouter/anthropic/claude-sonnet-4-6":
    input: 3.00
    output: 15.00
    cache_read: 0.30
    cache_write: 3.75
    reasoning: 15.00

  # Minimal override (cache prices derived from multipliers)
  "openrouter/anthropic/claude-opus-4-7":
    input: 5.00
    output: 25.00

defaults:
  cache_read_multiplier: 0.10   # cache_read = input * 0.10 if not specified
  cache_write_multiplier: 1.25  # cache_write = input * 1.25 if not specified
```

**Matching rules (in order):**

1. Exact match (case-insensitive) against `models:` keys in your YAML
2. Exact match against the built-in pricing table (~35 models)
3. Longest-prefix match (e.g. `claude-sonnet` matches `claude-sonnet-4-6-future`)
4. Unknown → `$0.00` with a one-time warning in `telemetry.log`

The built-in table covers: Anthropic (Claude 3/4 family), OpenAI (GPT-4o, GPT-4, o1, o3, o4), DeepSeek, Gemini, Llama, and Hermes models. Prices sourced from official provider pages (May 2026).

### `budget.yaml`

Configure spend guardrails. No file → budgets disabled.

```yaml
budgets:
  global:
    daily_usd: 2.00
    monthly_usd: 50.00
  per_cron_job:
    default:
      daily_usd: 1.00
    overrides:
      daily_email_report:
        daily_usd: 3.00
  per_sender:
    default:
      daily_usd: 2.00
    overrides:
      premium_user_123:
        daily_usd: 5.00

thresholds:
  soft_pct: 0.80    # warn at 80% of limit
  hard_pct: 1.00    # enforce at 100%

on_estimated:
  mode: enforce     # warn_only | enforce
```

**Scope resolution:**

| Scope | How spend is calculated |
|-------|------------------------|
| `global` | All sessions + all cron jobs combined |
| `per_cron_job` | Sessions where `cron_job_id` matches (excludes subagent cost) |
| `per_sender` | Sessions from a specific sender (multi-user gateways) |

**Window math:** daily and monthly windows are computed in the user's local timezone. A cron job that runs at 11:59 PM and another at 12:01 AM count against different daily windows.

---

## Architecture

### Hook Plugin

The plugin registers 10 hooks (out of 16 available in Hermes) plus 2 slash commands:

```
Hook                      Purpose
─────────────────────────────────────────────────────────────
on_session_start          Create run row, extract cron_job_id
pre_api_request           Stash approx_input_tokens for fallback
post_api_request          PRIMARY: record tokens, cost, latency
post_tool_call            Record tool name, success, duration
post_llm_call             Refresh session end timestamp
subagent_stop             Record delegate_task proxy on parent
on_session_end            Set final status (ok/error/interrupted)
on_session_finalize       Safety net: ensure run is closed
pre_llm_call              Soft budget alerts + capture sender_id
pre_tool_call             Hard budget enforcement (tool-gate)
```

**Why `post_api_request` is the primary hook for tokens:** The Hermes conversation loop can make multiple API calls per turn (retries, reasoning models, tool calls). Only `post_api_request` carries the canonical `usage` dict with token counts and cost data. `pre_llm_call` fires once per turn with no token data. `post_llm_call` fires after the tool loop with no token data.

**Cron job identification:** There is no `cron_job_id` in any hook. The plugin extracts it from the `session_id`, which follows the format `cron_{job_id}_{YYYYMMDD_HHMMSS}` (confirmed in Hermes source). An anchored regex handles job IDs that contain underscores.

### Database Schema

SQLite with WAL mode, per-thread connections, schema v3:

**`runs`** — one row per session (CLI session or cron job execution):

| Column | Description |
|--------|-------------|
| `session_id` | Primary key (`{YYYYMMDD_HHMMSS}_{uuid6}` for CLI, `cron_{job_id}_{ts}` for cron) |
| `platform` | `cli`, `cron`, `telegram`, `discord`, etc. |
| `cron_job_id` | Extracted from session_id when platform=cron |
| `model` | Model name (updated from last API call) |
| `provider` | Provider name (e.g. `openrouter`, `anthropic`) |
| `started_at` / `ended_at` | ISO-8601 UTC timestamps |
| `status` | `running`, `ok`, `error`, `interrupted` |
| `tokens_in` / `tokens_out` | Accumulated across all API calls in the session |
| `cost_usd` | Accumulated estimated cost |
| `duration_ms` | Wall time (ms) via `julianday()` |
| `api_calls` / `tool_calls` | Counters |
| `parent_session_id` | Reserved for future parent-child linking (not populated in v0.2) |
| `estimated_llm_calls` | Count of calls where provider returned `usage=None` |
| `sender_id` | For per-sender budgets (set via `pre_llm_call`) |

**`llm_calls`** — one row per individual API call:

All of `runs` token/cost columns, plus `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `estimated` (boolean).

**`tool_calls`** — one row per tool execution:

`session_id`, `ts`, `tool_name`, `ok` (boolean), `latency_ms`.

**`budget_alerts`** — anti-spam ledger:

`scope`, `scope_id`, `window`, `period_key`, `level`, `fired_at`, `spent_usd`, `limit_usd`. Unique constraint prevents duplicate alerts.

### Concurrency Model

Cron jobs run in a `ThreadPoolExecutor` (Hermes `cron/scheduler.py`). Multiple jobs can write to the DB simultaneously from different threads.

**Design:** per-thread SQLite connections via `threading.local()`. Each thread opens its own connection to the same WAL-mode DB file. A serializable `_schema_lock` protects DDL migrations on first connect (WAL mode switch requires a brief lock that `busy_timeout` alone doesn't handle).

`busy_timeout=5000` ensures write collisions retry for 5 seconds before raising. `synchronous=NORMAL` balances durability with write performance (safe for WAL mode).

---

## Budget Enforcement

### How It Works

Every time the agent is about to do work, the plugin checks:

1. **`pre_llm_call`** (fires once per turn): evaluates all applicable budget scopes. If any has a `soft` or `hard` verdict that hasn't been alerted yet this window, injects a one-time notice into the conversation context (anti-spam via `budget_alerts` table). Captures `sender_id`.

2. **`pre_tool_call`** (fires before every tool): re-evaluates budgets. If any scope is in `hard` breach, returns `{"action":"block","message":...}` which aborts the tool call.

3. **For cron jobs with `hard` breach:** additionally calls `cron.jobs.pause_job` to pause future runs.

### Enforcement Levels

Hermes does **not** expose a way to abort an in-flight model call from a plugin. `pre_llm_call` / `pre_api_request` returns can't cancel a call. So enforcement is honest about its reach:

| Level | Trigger | Effect | Repeat? |
|-------|---------|--------|---------|
| **Soft** (≥ `soft_pct`) | Spend reaches 80% of limit (configurable) | One-time notice injected into conversation | Once per window per scope |
| **Hard** (≥ `hard_pct`) | Spend reaches 100% of limit | Every subsequent tool call is blocked | Every tool call until window resets |
| **Cron pause** | Any hard `cron_job` verdict | Job is paused for future runs | Once per window per scope |

The model response already in flight still completes and is billed. What's prevented is *further* tool-driven work.

### Estimated Data and Budget Degradation

When the provider returns `usage=None`, the plugin estimates tokens and flags the row as `estimated=1`. Since these estimates may be inaccurate, the budget engine offers a safety valve:

**`on_estimated.mode: warn_only` (default):** If a hard verdict rests partly on estimated rows, it is **degraded to soft** — the user gets a warning but tools aren't blocked. Rationale: a budget built on estimates shouldn't hard-stop work.

**`on_estimated.mode: enforce`:** Hard verdicts take effect regardless of estimate quality. Use this when you trust your provider's usage data (Est% = 0) or when estimates are acceptable.

The `/stats providers` command shows the `Est%` column so you can see at a glance whether your provider returns real usage data.

---

## Provider Probe: Verifying Your Provider Returns Real Usage

Run this **once** after enabling the plugin:

1. Run one short session (any minimal task works)
2. Execute `/stats providers`
3. Look at the `Est%` column for your provider:
   - **`0%`** → provider returns real usage data. Budget verdicts are based on real numbers. Set `on_estimated.mode: enforce` for strict enforcement. ✅
   - **`> 0%`** → provider omits usage in some responses. Those calls are estimated and flagged. Budget hard-verdicts will be degraded to soft under `warn_only`. The `telemetry.log` will have a **one-time WARNING** per provider. ⚠️

---

## Proof of Concept

The following PoC was executed live to validate the plugin end-to-end.

### Setup

- **Hermes gateway** running on Linux (WSL), model `openrouter/owl-alpha` (free tier)
- **Plugin:** hermes-telemetry v0.2.0, loaded in gateway process
- **DB:** `/home/nujovich/.hermes/telemetry/telemetry.db` (schema v3, WAL mode)
- **6 cron jobs** configured, 2 used for this PoC

### Pricing Capture

Added models to `~/.hermes/telemetry/pricing.yaml`:

```yaml
models:
  "openrouter/owl-alpha":
    input: 0.00    # Free model on OpenRouter
    output: 0.00
  "openrouter/anthropic/claude-sonnet-4-6":
    input: 3.00
    output: 15.00
    cache_read: 0.30
    cache_write: 3.75
  "openrouter/anthropic/claude-opus-4-7":
    input: 5.00
    output: 25.00
    cache_read: 0.50
    cache_write: 6.25
```

Set `on_estimated.mode: enforce` for deterministic enforcement.

### Budget Enforcement Test

**Step 1 — Trigger a hard breach:**

- Budget: `global.daily_usd: 0.001` ($0.001/day)
- Ran MCP Lead Gen job (model: `claude-sonnet-4-6`, ~$3/$15 per 1M)
- Result: job spent $0.1812 on first run → **18,120% of daily limit** → █ hard breach → **job auto-paused**

```
█ global    $0.1812 / $0.00    18120%  [daily]
                         ↑ (0.001 rounded to 0.00 in display)
```

**Step 2 — Raise budget and resume:**

```
/budget set global daily 2.00
```

This hot-reloads the budget config (value was previously cached at $0.001 in memory — edits to `budget.yaml` alone don't take effect without `/budget set` or gateway restart).

Result after `/budget set`:

```
  global    $0.1812 / $2.00    9%  [daily]
```

**Step 3 — Verify job runs normally:**

- MCP Lead Gen re-ran successfully under the $2.00 daily budget
- Second run confirmed: `state: scheduled`, `paused_at: null`

### Cron Job Cost Comparison

Poisoned two jobs with different priced models:

| Job | Model | Price (input/output) |
|-----|-------|---------------------|
| MCP Lead Gen | `claude-sonnet-4-6` | $3.00 / $15.00 per 1M |
| Marketing Highlights | `claude-opus-4-7` | $5.00 / $25.00 per 1M |
| Base sessions (CLI) | `owl-alpha` | $0.00 / $0.00 (free) |

**Results from SQLite (`/stats` after all runs):**

- **CLI sessions** (owl-alpha, free): ~1M tokens in → **$0.00**
- **MCP Lead Gen** (claude-sonnet-4-6): ~892K tokens in → **$0.314**
- **Marketing Highlights** (claude-opus-4-7): ~445K tokens in → **~$2.23** (opus is ~5-8x more expensive per token)

This demonstrates the core value proposition: **you can see exactly how much each cron job costs and compare models.**

### Results Summary

| Component | Status |
|-----------|--------|
| Token capture from provider | ✅ Real usage (`estimated=0`) |
| Cost estimation with pricing table | ✅ Accurate to pricing YAML |
| Cron job session tracking | ✅ Captured via `session_id` regex |
| Budget soft alerts | ✅ One-time context injection |
| Budget hard enforcement | ✅ Paused job at $0.001/day |
| Budget hot-reload via `/budget set` | ✅ Cache cleared, new limit active |
| Multi-model cost comparison | ✅ Sonnet vs Opus vs Free |
| 94 tests pass | ✅ |

---

## Running Tests

```bash
cd hermes-telemetry
pip install pytest pyyaml
pytest tests/ -v
```

**Test suite (94 tests):**

| File | Tests | Coverage |
|------|-------|----------|
| `test_db.py` | 15 | Schema v1→v3 migrations, CRUD, aggregations, concurrent WAL writes (10 threads × 5 writes) |
| `test_pricing.py` | 17 | Cache/reasoning split, no double-counting of `prompt_tokens`, YAML overrides, prefix matching, unknown model handling |
| `test_init.py` | 6 | Cron session ID regex, tool success/failure parsing |
| `test_budget.py` | 17 | ok/soft/hard verdicts, estimated-to-soft degradation, anti-spam ledger, cron pause, per-scope routing, `/budget set` hot-reload |
| `test_stats_providers.py` | 8 | Real vs estimated per provider, `/stats providers` output format, Nous warning dedup |
| `test_subagent_reconciliation.py` | 4 | Parent + child hook sequence, token reconciliation, no double-counting |

No live Hermes is required — all tests are self-contained with in-memory SQLite.

---

## Data Location

```
~/.hermes/telemetry/
├── telemetry.db        ← SQLite (WAL mode, ~70KB base + growth)
├── telemetry.log       ← Plugin log (errors, debug, one-time warnings)
├── pricing.yaml        ← Your model price overrides
└── budget.yaml         ← Your spend guardrails
```

The DB grows over time. For high-frequency cron jobs, consider periodic cleanup of old rows (not yet automated — see [Known Limitations](#known-limitations)).

---

## Known Limitations

**Enforcement gaps:**

- **No true mid-call abort.** `pre_llm_call` / `pre_api_request` cannot cancel an in-flight model call. The response that's already generating will complete and be billed. The tool-gate (`pre_tool_call`) stops *subsequent* work at the next tool boundary.
- **Runaway text-only sessions.** A session that generates text without calling any tools never hits the tool-gate. If this becomes a problem, a pre-flight check in `on_session_start` for cron jobs could abort before the first LLM call.

**Subagent attribution:**

- Child agents (`delegate_task`) run as their own sessions. Their tokens are captured independently and included in **global** totals. But there is no parent→child link in any hook — so `per_cron_job` budgets **exclude** subagent cost. Use the `global` budget for a cap that captures delegated work.

**Pricing staleness:**

- `pricing.yaml` is manually maintained. A new model not in the table falls through to `$0.00` with a one-time warning. No auto-sync from provider APIs yet.

**DB retention:**

- `telemetry.db` grows without bound. No automatic purge of old rows. For >100K rows, consider manual cleanup or a retention policy (not yet implemented).

**Gateway restart required:**

- Enabling the plugin takes effect only after gateway restart. Cron runs that started before the restart won't have telemetry.

---

## Troubleshooting

**`/stats cron week` shows "No cron runs in the last 7 days":**

The gateway loaded before the plugin was enabled. Restart the gateway:
```bash
hermes gateway restart
```
Then re-run a cron job.

**`/budget` shows `$0.00` as the limit:**

The limit is cached in memory at gateway start. If you edited `budget.yaml` directly, the cache is stale. Use `/budget set global daily <amount>` to hot-reload, or restart the gateway.

**Cost is $0.00 for all sessions:**

Your model isn't in the pricing table. Check `telemetry.log` for a one-time warning like:
```
hermes-telemetry: unknown model 'openrouter/some-model' — cost recorded as $0.00
```
Add it to `pricing.yaml`.

**Provider Est% > 0:**

Your provider returns `usage=None` for some/all calls. Tokens are estimated. Check `/stats providers` to see which providers are affected. If Est% is 100% for your main provider, all spend is estimated and budget hard-verdicts degrade to soft under `warn_only` mode.

**Plugin not loading at all:**

Check `telemetry.log` for errors. Common causes:
- Missing `pyyaml` in the gateway's venv: `pip install pyyaml`
- Plugin not in `plugins.enabled` in config.yaml
- Syntax error in `pricing.yaml` or `budget.yaml`

---

## License

MIT — see [LICENSE](LICENSE).
