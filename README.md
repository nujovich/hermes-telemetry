# hermes-telemetry

Observability + budget plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — captures tokens, estimated cost, latency, and tool usage per session and cron job, persists to local SQLite, and exposes `/stats` and `/budget` slash commands. Budgets can warn and gate spend per scope (global / cron job / sender).

**Design principle:** observability is invisible to the model. Everything goes through hooks. The only user-facing surface is `/stats` and `/budget`.

---

## What it measures

| Metric | Availability |
|--------|-------------|
| Tokens in / out per API call | ✅ Real (from `post_api_request` hook) |
| API call latency | ✅ Real (ms) |
| Tool call latency & success/failure | ✅ Real |
| Session / cron job wall time | ✅ Real |
| Model & provider name | ✅ Real |
| Platform (cli / cron / telegram / …) | ✅ Real |
| Estimated cost (USD), with cache/reasoning split | ⚠️ Estimated via local pricing table |
| Subagent token cost (in global totals) | ✅ Real — child agents are recorded as their own runs |
| Subagent cost attributed to a parent cron job | ❌ Not available (no parent→child link in hooks) |
| Tokens when provider omits usage | ⚠️ Estimated and flagged (never silently 0) |

Cost is always an *estimate*. No external pricing API is called. See [Custom pricing](#custom-pricing) to override. When the provider returns no usage data, tokens are estimated from a pre-request approximation + response length and the row is flagged as estimated, so `/stats` and `/budget` show a `~` and an "estimated data" percentage rather than silently undercounting.

---

## Installation

Hermes plugins are **opt-in** — you must both install and enable the plugin.

```bash
# Install from GitHub (Hermes looks in ~/.hermes/plugins/)
hermes plugins install nujovich/hermes-telemetry

# Enable it
hermes plugins enable hermes-telemetry
```

Or manually:

```bash
git clone https://github.com/nujovich/hermes-telemetry ~/.hermes/plugins/hermes-telemetry
hermes plugins enable hermes-telemetry
```

Restart Hermes (or your gateway) after enabling.

---

## Usage

### `/stats` slash command

```
/stats              → last 24h summary (sessions, tokens, cost, top tools)
/stats today        → same as /stats
/stats week         → last 7 days
/stats month        → last 30 days
/stats cron         → breakdown by cron_job_id (last 7 days)
/stats cron week    → cron breakdown, last 7 days
/stats cron month   → cron breakdown, last 30 days
/stats raw [N]      → last N raw run records (default 20)
```

Example output:

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
  bash                               51         3     340ms
  write_file                         28         0      18ms
```

### Data location

```
~/.hermes/telemetry/
├── telemetry.db      ← SQLite database (WAL mode)
├── telemetry.log     ← plugin log (errors / debug)
├── pricing.yaml      ← optional pricing overrides
└── budget.yaml       ← optional spend budgets (see Budgets below)
```

---

## Custom pricing

Create `~/.hermes/telemetry/pricing.yaml`:

```yaml
# USD per 1 million tokens
my-private-model:
  input: 0.50
  output: 1.50

# Override a built-in entry
claude-sonnet-4-6:
  input: 3.00
  output: 15.00
```

Model names are matched case-insensitively. Prefix matching is used as a fallback for unknown model variants (e.g. `claude-sonnet` matches `claude-sonnet-5-x-future`). Unknown models log a one-time warning and record cost as `$0.00`.

See `config.example.yaml` in this repo for a complete example.

---

## Running tests

```bash
cd hermes-telemetry
pip install pytest pyyaml
pytest tests/ -v
```

Tests cover: schema creation, idempotent migrations (v1→v3), write/read, aggregation queries, concurrent WAL writes (10 threads × 5 writes), pricing calculation with cache/reasoning split, prefix matching, unknown models, custom YAML overrides, cron session parsing, and the full budget engine (soft/hard/degraded verdicts, anti-spam, cron pause). No live Hermes is required.

---

## Budgets

Spend guardrails reuse the same SQLite telemetry — **zero network calls**. Create `~/.hermes/telemetry/budget.yaml`:

```yaml
budgets:
  global:
    daily_usd:   5.00
    monthly_usd: 100.00
  per_cron_job:
    default:
      daily_usd: 1.00
    overrides:
      my_nightly_job: { daily_usd: 3.00 }
  per_sender:            # by sender_id (multi-user gateways)
    default:
      daily_usd: 2.00
thresholds:
  soft_pct: 0.80         # warn at 80%
  hard_pct: 1.00         # enforce at 100%
on_estimated:
  mode: warn_only        # spend based on estimated rows never hard-cuts
```

No file → budgets are simply disabled.

### `/budget` slash command

```
/budget                          → status of every scope (spent / limit / %)
/budget cron                     → per-cron-job budgets, with soft/hard flags
/budget set global daily 5.00    → set or raise a limit (persists + hot-reloads)
```

### What enforcement can and cannot do

Hermes does **not** expose a way to abort an in-flight model call from a plugin (verified against the agent source — `pre_llm_call` / `pre_api_request` returns can't cancel a call). So enforcement is honest about its reach:

- **Soft (≥ `soft_pct`):** a one-time-per-window notice is injected into the conversation. It will not repeat every turn (anti-spam ledger in a `budget_alerts` table).
- **Hard (≥ `hard_pct`):** a **tool-gate** — the `pre_tool_call` hook blocks every subsequent tool, which ends the agentic loop at the next tool boundary. The model response already in flight still completes and is billed; what's prevented is *further* tool-driven work. For `platform == "cron"`, the offending job is additionally **paused** (future runs) via `cron.jobs.pause_job`.
- **Estimated spend:** when a verdict rests on rows where the provider returned no usage (`estimated=1`), a hard breach is **degraded to soft** under `on_estimated.mode: warn_only` — a budget built on estimates shouldn't hard-cut.

### Attribution honesty

Per-cron-job budgets **exclude subagent (`delegate_task`) cost**: child agents run as their own sessions and Hermes exposes no parent→child link in any hook, so their spend can't be attributed to the parent job without double-counting. Subagent cost **is** included in the **global** total. For a tope that captures delegated spend, use the `global` budget. (`/budget cron` prints this caveat.)

---

## License

MIT — see [LICENSE](LICENSE).
