# hermes-telemetry

Observability plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — captures tokens, estimated cost, latency, and tool usage per session and cron job, persists to local SQLite, and exposes a `/stats` slash command.

**Design principle:** observability is invisible to the model. Everything goes through hooks. The only user-facing surface is `/stats`.

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
| Estimated cost (USD) | ⚠️ Estimated via local pricing table |
| Subagent invocations | ✅ Counted (no token breakdown) |
| Tokens when provider omits usage | ❌ Recorded as 0 |

Cost is always an *estimate*. No external pricing API is called. See [Custom pricing](#custom-pricing) to override.

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
└── pricing.yaml      ← optional pricing overrides
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

Tests cover: schema creation, idempotent migrations, write/read, aggregation queries, concurrent WAL writes (10 threads × 5 writes), pricing calculation, prefix matching, unknown models, and custom YAML overrides.

---

## Roadmap

### Phase 2 — Budget guardrails (not yet implemented)

The stub at `pre_llm_call` in `__init__.py` is the extension point:

```python
# TODO(fase-2): budget enforcement
# 1. Query db.stats_summary(window_hours=current_period) for cost_usd
# 2. Load budget limit from ~/.hermes/telemetry/config.yaml
# 3. If cost_usd >= limit, block further API calls via pre_tool_call
```

The `db.py` API is designed to be reused by phase-2 — `stats_summary()` and `cost_by_job()` already expose the aggregates needed for budget enforcement. No schema changes will be required.

---

## License

MIT — see [LICENSE](LICENSE).
