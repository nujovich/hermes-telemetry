# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Free→paid model transition alert (issue #16, Block A). When a model that
  was previously seen at explicit `$0` (subscription or zero-price entry in
  `pricing.yaml`) starts incurring cost, a one-shot warning is injected into
  the next `pre_llm_call` context so the agent can surface it to the user.
  The alert fires once per session per transition and is cleared immediately
  after injection.
- `known_free_models` SQLite table (schema v5) persists every `(model,
  provider)` pair observed at explicit `$0`, enabling cross-session detection.
  `is_explicitly_priced(model, provider)` distinguishes genuinely-free models
  from unknown-model `$0` fallbacks so only real free-tier models are tracked.
- Backward-compatible backfill: on every plugin load, `register()` scans all
  explicitly-`$0` models from `pricing.yaml` and `_DEFAULT_PRICING` and seeds
  `known_free_models` with a `provider=''` wildcard row. Pre-v5 users who
  already had subscription models are covered automatically on first upgrade
  without requiring a prior `$0` call in the new version.
- `pricing.get_known_free_models()` — returns all model names with explicit
  `input=0.0 AND output=0.0` from custom and default pricing; used by the
  backfill and available for external inspection.
- `db.backfill_known_free_models(models)` — inserts `(model, provider='')` 
  wildcard rows; `INSERT OR IGNORE` makes it safe to call on every load.

## [0.4.1] - 2026-06-14

### Added
- Provider-aware pricing lookup guard (issue #24). A pricing entry tagged
  `_source: openrouter` in `pricing.yaml` is now only applied to calls the
  OpenRouter provider actually served (or calls with no provider, for
  backward compatibility). This stops an OpenRouter rate from silently
  costing a call another provider served — e.g. the OpenRouter Qwen price
  leaking onto a Nous Portal call. Source-less entries (`_DEFAULT_PRICING`,
  the prefix table, hand-added overrides) stay provider-neutral.
- NVIDIA NIM seed prices in `_DEFAULT_PRICING` (issue #12, Phase 1): five
  Nemotron models (`nemotron-3-super-120b-a12b`, `nemotron-super-49b`,
  `nemotron-70b-instruct`, `nemotron-nano-12b-vl`, `nemotron-nano-9b`).
  Seeds are source-neutral and live in code, so they survive an OpenRouter
  refresh and win over a same-id OpenRouter entry for `provider=nvidia`
  calls. `:free` promo variants resolve to `$0` via the unknown-model
  fallback and need no entry.
- `_subscription: true` pricing tag — flags a flat-rate / subscription model
  so it resolves to `$0` **without** the unknown-model warning, distinct
  from a missing price. Lets users price e.g. a Nous-served model at `$0`
  even when an OpenRouter entry for the same id exists.
- `provider` parameter on `pricing.estimate_cost()` (threaded through
  `_resolve_pricing` / `_lookup_base` / `_lookup_form`), passed from the
  `post_api_request` hook. Defaults to `""`, preserving the previous
  provider-blind behaviour for existing callers.

### Changed
- `pricing_refresh.py` now emits `subscription_models` in the YAML `_meta`
  block and excludes those models from `estimated_price_models`.
- Unknown-model pricing warnings are now de-duplicated per `(model,
  provider)` pair instead of per model, so the same id can warn separately
  under each provider that lacks a price for it.

### Fixed
- `_SCHEMA_VERSION` was stuck at `3` despite the `_migrate_v4` migration
  (adds `cache_read_tokens` / `cache_write_tokens` to `runs`), so the
  recorded schema version never matched the applied migrations. Bumped to
  `4`.
- Dashboard failed to import on Python 3.8/3.9 due to PEP 604 `str | None`
  union syntax — added `from __future__ import annotations` to
  `dashboard/serve.py`.

## [0.4.0] - 2026-06-09

### Added
- `GoogleAISource` in `pricing_refresh.py` — direct Google AI Studio pricing
  for `provider=google` calls (bare `gemini-*` model ids, no `google/`
  prefix). Ships a constant table mirroring
  https://ai.google.dev/gemini-api/docs/pricing with a `LAST_VERIFIED`
  date for manual quarterly refresh. Registered alongside
  `OpenRouterSource`, so the same auto-refresh cycle now populates
  pricing for both routing paths. Addresses #9.

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

[0.4.1]: https://github.com/nujovich/hermes-telemetry/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.3.1...v0.4.0
[0.3.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nujovich/hermes-telemetry/releases/tag/v0.1.0
