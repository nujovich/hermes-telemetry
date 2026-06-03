# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-06-02

### Added
- First-time setup wizard (`/setup` slash command) — interactive and non-interactive modes
- Auto-generate pricing from OpenRouter API fetch + built-in defaults (~30 models)
- Manual pricing protection: models with `openrouter/` prefix are not overwritten by auto-refresh
- YAML serializer fallback (stdlib-only, no PyYAML required)

## [0.2.0] - 2026-06-01

### Added
- Dashboard UI (`dashboard/`) — local HTTP server, stdlib + Chart.js, port 8765
- Pricing auto-refresh from OpenRouter API (`pricing_refresh.py`)
- Provider breakdown in `/stats providers` command
- User legend: "Provider key" (nous vs openrouter routing path explained)
- Database persistence layer (`db.py`) — SQLite WAL mode
- Session + cron job telemetry: tokens, cost, latency, tool calls
- Budget enforcement: soft (warn at 80%) and hard (block at 100%) thresholds
- Estimated price flag (`_estimated_price: true`) for models without fixed pricing
- Subagent cost reconciliation hook (`subagent_stop`)
- POC verification scripts (`poc_setup.py`, `poc_setup_cmd.py`)

### Changed
- Stats output: provider field reflects routing path (not normalized)
- Negative pricing models: excluded from auto-generated pricing

## [0.1.0] - 2026-05-31

### Added
- Initial plugin scaffold (`__init__.py`, `plugin.yaml`)
- Telemetry hooks: `on_session_start`, `pre_api_request`, `post_api_request`,
  `post_tool_call`, `post_llm_call`, `on_session_end`, `on_session_finalize`,
  `pre_llm_call`, `pre_tool_call`
- `/stats` slash command — aggregate and session-level stats
- `/budget` slash command — budget status and configuration
- Pricing table (`pricing.py`) — manual model pricing with cache_read/cache_write support
- Budget config (`budget.py`) — global, per-cron-job, per_sender scopes
- Config examples (`config.example.yaml`, `budget.example.yaml`)
- pytest suite with conftest, test_db, test_budget, test_pricing, test_setup, test_stats_providers, test_subagent_reconciliation
- MIT License
- README with architecture, usage, screenshots

[0.3.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nujovich/hermes-telemetry/releases/tag/v0.1.0
