---
title: Introduction
description: What hermes-telemetry does and what it measures.
---

hermes-telemetry is an observability and budget plugin for
[Hermes Agent](https://github.com/NousResearch/hermes-agent). Hermes runs autonomously across
sessions, platforms, and cron jobs, so it can keep spending when nobody is watching. The plugin
runs inside the runtime, records usage per session and cron job, and enforces budget limits
by blocking further tool calls once a hard limit is breached.

## What it does

- Captures tokens, cost, latency, and tool calls per session and cron job.
- Persists everything to a local SQLite database.
- Enforces spend budgets through Hermes hooks: a soft breach injects a one-time notice, a hard
  breach blocks every further tool call and pauses the cron job. Hermes cannot abort a model call
  already in flight, so that response still completes and is billed.
- Exposes `/stats` and `/budget` slash commands.

## What it measures

| Metric | Source | Real or estimated |
| --- | --- | --- |
| Tokens in / out per API call | `post_api_request.usage` | Real (from provider) |
| Cache read / write tokens | `post_api_request.usage` | Real (from provider) |
| Reasoning tokens | `post_api_request.usage` | Real (from provider) |
| API call latency | `post_api_request.api_duration` | Real |
| Tool call latency and success/failure | `post_tool_call` | Real |
| Session / cron job wall time | `started_at` to `ended_at` | Real |
| Model and provider name | `post_api_request` | Real |
| Platform (cli / cron / telegram / ...) | `on_session_start.platform` | Real |
| Cron job ID | Parsed from `session_id` | Real |
| Subagent invocation count | `subagent_stop` hook | Real (proxy) |
| Cost (USD) | Local pricing table x tokens | Estimated |
| Tokens when the provider returns `usage=None` | Fallback approximation | Estimated, flagged |
| MoA reference-model tokens | No hook fires (auxiliary call path) | Not captured |

Cost is always an estimate computed from a locally maintained pricing table. No external pricing
API is called. When a provider returns no usage data, tokens are approximated and the row is
flagged as estimated.

## Documentation status

This site is new and its content is still being migrated. Until that is done, the complete
reference lives in the repository:

- [README.md](https://github.com/nujovich/hermes-telemetry/blob/main/README.md): installation, configuration, commands, and architecture.
- [ONBOARDING.md](https://github.com/nujovich/hermes-telemetry/blob/main/ONBOARDING.md): design decisions, hook behavior, and schema evolution.
