# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- `pricing._lookup_base` now resolves `gemini-X` and `google/gemini-X` to
  the same entry symmetrically (issue #2 follow-up). Direct-Google and
  OpenRouter-routed callers no longer need both forms present in the
  pricing data to get a hit. Normalization is google-specific — other
  provider prefixes (`anthropic/`, `meta-llama/`, `openrouter/`) keep
  their existing semantics and are never stripped.

## [0.3.1] - 2026-06-06

### Added
- Gemini 3.x and 2.5 family pricing in `_DEFAULT_PRICING` (issue #2):
  `gemini-3.5-flash`, `gemini-3.1-pro-preview`, `gemini-3.1-flash-lite`,
  `gemini-3-flash-preview`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`.
  All entries include explicit `cache_read` matching Google's published rates.
- Family-specific Gemini prefix entries in `_PREFIX_PRICING` to cover dated
  variants (e.g. `gemini-3-flash-preview-20251217`).

### Changed
- `gemini-2.5-pro` entry now includes explicit `cache_read: 0.125`.
- Direct-Google Gemini lookups (no `google/` prefix) now resolve to correct
  prices — previously fell through to the legacy generic `gemini` prefix and
  were priced as Flash 1.5, underestimating cost by ~6.5x for Gemini 3 Flash.

### Fixed
- `runs` rows are now lazy-created from `record_llm_call` and `end_run` when
  `on_session_start` was missed (issue #3). Affected any deployment where the
  plugin was enabled on a gateway with already-running chat-platform sessions:
  the bot's `session_id` never received the start hook, so all subsequent
  `UPDATE runs WHERE session_id = ?` calls were silent no-ops and `/stats` /
  `/budget` under-reported. The new private `_ensure_run_row` helper mirrors the
  `INSERT OR IGNORE` pattern already used by `start_run`, so the happy path is
  unchanged (no duplicate rows, `platform` / `cron_job_id` preserved).

### Removed
- Deprecated Gemini entries from `_DEFAULT_PRICING`: `gemini-1.5-pro`,
  `gemini-1.5-flash`, `gemini-2.0-flash` (removed from Google's pricing page;
  2.0 family sunset 2026-06-01).
- Generic `gemini` catch-all prefix in `_PREFIX_PRICING` (was Flash 1.5 pricing
  for any unknown gemini-* variant — silently mis-priced new models). Unknown
  Gemini variants now surface as `unknown-model` warnings instead.

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
