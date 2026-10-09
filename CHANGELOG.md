# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **DeepSeek current ids priced at retired V3 rates (#121).** `deepseek-flash`,
  `deepseek-v4-flash` and `deepseek-v4-pro` had no built-in entry and fell to the
  bare `deepseek` prefix (0.27/1.10 plus a generic 0.10x cache read), overstating
  cost for `custom:<name>` providers, which never get a Hermes core snapshot.
  They now carry explicit off-peak rates matching Hermes core (Flash 0.15/0.60,
  cache read 0.003; Pro 0.66/1.98, cache read 0.022; cache write = input), and
  `deepseek-chat` / `deepseek-reasoner` and the bare `deepseek` prefix follow
  Flash. `deepseek-v3` / `deepseek-r1` are unchanged. Vendor-qualified `deepseek/...` ids
  are aggregator ids (Nous / OpenRouter) and intentionally keep the prior 0.27/1.10
  fallback, since their authoritative price is the core snapshot. Peak-hour (2x)
  pricing is tracked in #122.

## [0.9.0] - 2026-10-07

### Upgrade notes

- **Schema v13 → v17.** Four forward-only migrations run automatically on first
  load: `v14` adds the `pricing_snapshots` table (#71); `v15` adds
  `pricing_snapshots.resolved_model` (#72); `v16` adds the
  `endpoint_payload_cache` and `model_efficiency_cache` tables (#53); `v17` is a
  data-only repair that deletes false `known_free_models` rows and the
  `free_paid_transitions` rows they produced (#117). On an install that was not
  affected by #89, `v17` deletes nothing.
- **Module rename `setup.py` → `setup_wizard.py` (#118).** The setup wizard was
  renamed so setuptools no longer executes it as a build script. Anything that
  imports it directly (custom scripts, forks, docs) must switch to
  `setup_wizard`; the `/setup` command and `hermes telemetry setup` are
  unchanged.
- **New runtime dependency: `pyyaml>=6.0` (#118).** It is now declared in
  `pyproject.toml`, so pip-based installs pull it in. Hosts that load the plugin
  from a checkout into an existing Hermes venv need PyYAML available there.
- **Observation mode is opt-in; `enforce` stays the default (#114).** Upgrading
  keeps today's budget guardrails. Only an explicit
  `plugins.entries.hermes-telemetry.settings.mode: observe` turns enforcement
  off.
- **Costs now prefer the core-sourced tariff (#94).** Where Hermes core has
  already resolved a price for a `(provider, model)` pair, it outranks
  `pricing.yaml`. Totals for models whose `pricing.yaml` entry disagreed with the
  core tariff can therefore change after upgrade (see the Changed section).

### Added — Core-sourced pricing snapshots (#71, #72)

- New `core_pricing.py` seam persists an append-per-change history of the tariffs
  Hermes core resolves per `(provider, model)` (via
  `agent.usage_pricing.get_pricing_entry`), with provenance: `source`,
  `source_url`, `pricing_version`, `fetched_at`. Capture happens in
  `post_api_request`, is throttled to once per 6 hours per `(provider, model)`
  per process, and is fail-open: if core is absent or cannot resolve the model,
  nothing is recorded and the hook is unaffected.
- Dated model ids (e.g. `deepseek/deepseek-v4-pro-20260423`) miss the provider
  `/models` catalog, which lists only canonical names. Capture now retries under
  the canonical name (a trailing `-YYYYMMDD` is stripped, including before
  `:free`) and stores the snapshot under the raw name, with the canonical name in
  the new `resolved_model` column.
- **Schema v14** (`pricing_snapshots` + `idx_pricing_snapshots_model`) and
  **v15** (`pricing_snapshots.resolved_model`).

### Added — `hermes telemetry pricing backfill` (#73)

- Dry-run by default; `--apply` writes, `--json` for machine output. Seeds one
  current pricing snapshot for every `(provider, model)` in `llm_calls` that has
  none yet, so historical dated ids that never get a live snapshot gain coverage.
  It is a coverage seed, not a historical reconstruction: rows carry the tariff
  core resolves today. Idempotent, fail-open, no schema change.

### Added — `hermes telemetry pricing drift` (#75)

- Dry-run by default; compares `pricing.yaml` input/output rates against the
  latest core snapshot per model and reports entries beyond `--threshold`
  (default 1%). `--apply` merges the core rates back into `pricing.yaml`
  (tagging each repaired entry `_source: core-snapshot`) and hot-reloads;
  `--model` scopes to one model, `--json` for machine output.
- Skips `_subscription: true` entries and treats `_provider_assumed` guesses as
  "no local price" rather than drift. If models in `llm_calls` have no snapshot,
  the report points at `pricing backfill` so a clean run is not a false all-clear.
  No schema change.

### Added — Observation-only telemetry mode (opt-in) (#114)

- New plugin setting `plugins.entries.hermes-telemetry.settings.mode` with
  choices `enforce` (default) and `observe`. Observation mode keeps collecting
  and reporting telemetry but does not auto-create `budget.yaml`, inject budget
  notices, block tools via `pre_tool_call`, or pause cron jobs. Existing
  installs keep today's guardrails on upgrade: missing `mode`, invalid or
  non-string values, and older Hermes hosts without `ctx.get_config` all resolve to `enforce`.
  The budget file watcher still runs in both modes so `/budget` stays fresh.
  When observe is active with a `budget.yaml` present, `/budget` and the
  dashboard budget panel plainly say limits are **not enforced**.

### Added — Dashboard EN/RU language toggle (#92)

- Standalone dashboard (`dashboard/index.html`) is EN-first with an EN↔RU toggle:
  EN strings are the source of truth, `dashboard/i18n_ru.js` is the RU overlay
  (one key per line, leaf text only), `dashboard/i18n.js` holds the shipped
  `i18n_t`/`__DYN`/`__RU` logic shared by the page and `dashboard/i18n.test.js`.
  Default `en` with `<html lang="en">`, `document.documentElement.lang` synced on
  load and toggle, in-place re-render preserving drilldown/filters/scroll,
  persistence key `hermes_telemetry_lang`. Status badge class resolves from the
  raw value; EN path is identity, RU the only override. Known limitation: the
  plugin widget (`dashboard/dist/index.js`) stays English.

### Added — `profile` scope in `budget set` (#79)

- `budget set` now accepts `global | cron_job | sender | profile`, in chat
  (`/budget set <scope> [<id>] <window> <usd>`, e.g.
  `/budget set profile faro monthly 50`) and in the standalone CLI
  (`hermes-telemetry budget set <scope> <window> <usd> [--id <name>]`). With an
  id it writes `budgets.per_<scope>.overrides.<id>`; without one, the scope's
  `default` bucket. The id-override form also works for `cron_job` and `sender`.
  Previously `profile` was rejected as "Unknown scope" even though enforcement,
  forecast, and manual YAML already honored it.

### Changed — Core-sourced pricing snapshots are now the primary cost source (#94)

- `estimate_cost()` now prefers the tariff Hermes core itself resolved for a
  `(provider, model)` pair (already captured in `pricing_snapshots` by
  `post_api_request`, read locally from SQLite — no new network calls) over
  `pricing.yaml`/`_DEFAULT_PRICING`. A declared `_subscription: true` entry and
  the `:free` suffix rule still outrank the core snapshot; a cold-start pair
  with no snapshot yet falls back to the existing `pricing.yaml` chain
  unchanged. `hermes telemetry pricing drift`/`pricing backfill` remain useful
  for auditing `pricing.yaml` itself but are no longer required to keep costs
  accurate. An incomplete core snapshot (missing `input` and/or `output`) is
  ignored rather than priced at `$0`. See
  `ONBOARDING.md § Pricing Engine → Lookup priority chain`.

### Changed — Standalone dashboard caches heavy endpoints (#53)

- Model efficiency, providers, provider health, and the daily token / daily
  model charts are cached in SQLite and refreshed in the background, so the
  standalone dashboard stays responsive on large databases; the heavy
  model-efficiency fetch is deferred until the Breakdown page renders.
  **Schema v16** adds `endpoint_payload_cache` and `model_efficiency_cache`.
  `PRAGMA journal_mode=WAL` is now serialized with schema initialization,
  removing a lock race between concurrent first connections. Telemetry
  collection, budgets, and pricing are unchanged.

### Fixed — `pyyaml` declared as a runtime dependency (#113, #118)

- `pyyaml` is imported at runtime by the pricing, budget, and stats loaders but
  was missing from `[project].dependencies`, so a clean `pip install` broke on
  first load. It is now declared (`pyyaml>=6.0`). Package discovery was also
  fixed for non-uv build frontends by mapping the repo root to
  `hermes_telemetry` explicitly, and the wizard module was renamed from
  `setup.py` to `setup_wizard.py` so setuptools no longer tries to exec it as a
  build script. `tests/test_packaging.py` guards all three.

### Fixed — `/budget` status block did not list per-profile budgets (#70, #95)

- `/budget set profile <id> ...` and `/budget forecast ... profile <id>` already
  worked, but plain `/budget` never showed per-profile status — only `global`,
  `cron_job`, and `sender` scopes rendered. Added `db.list_profile_ids()`
  (mirrors `list_cron_job_ids`/`list_sender_ids`) and a `Profiles:` section in
  `_status_block()`, closing the command-surface parity gap between `set`,
  `forecast`, and `status` for the `profile` scope. Only profiles seen in runs
  within the last 30 days appear, matching the existing cron-job/sender window.

### Fixed — Date-range label collapsed to "X to X" when only `--from` was given (#37, #93)

- `hermes-telemetry stats --from <date>` used to auto-fill the missing `--to`
  with the current timestamp, so the rendered label read as a same-day range
  (e.g. "2026-06-16 to 2026-06-16") instead of an open-ended one.
  `_resolve_date_range` now leaves `date_to` as `None` when `--to` is
  omitted, matching the label behavior the `/stats --from` slash command
  already had. `stats._date_range_label` renders that case as `since <date>`.
  The DB layer already treated a missing `date_to` as an unbounded upper
  limit, so query results are unchanged — only the label and the JSON
  `date_to` field (now `null` instead of an auto-filled timestamp) reflect
  the real input.

### Fixed — Paid models recorded as free on zero-token calls, then raising false free→paid alerts (#89, #117)

- `post_api_request` added a model to `known_free_models` whenever a call cost
  $0 and the model had any explicit price. A paid model (e.g. `deepseek-v4-pro`)
  whose call happened to use zero tokens therefore qualified, and every later
  real call to it queued a false free→paid alert. Detection now goes through
  the new `pricing.is_free_model()`, which is true only for a model priced at
  input=0 AND output=0.
- Schema v17 is a data-only repair of installs that were already affected. It
  deletes `known_free_models` rows unless there is evidence the model was
  genuinely free: a `provider=''` wildcard row, or an `llm_calls` row for the
  same model and provider at $0 that used tokens. It also deletes the
  `free_paid_transitions` rows those false entries produced. No columns or
  tables change, and on an unaffected install the migration deletes nothing.

### Fixed — `pricing drift --apply` could pin a stale price across providers (#85)

- When two providers (e.g. `custom` and `nous`) share an API and both have
  snapshots for the same canonical model, `--apply` used to take the
  alphabetically-first provider's snapshot, which could overwrite the current
  price with an older one (issue #84). Drifted entries are now ordered by
  snapshot recency, so the most recent snapshot wins; the multi-provider warning
  is kept and reworded. No schema change.

### Fixed — `pricing_refresh` ignored `HERMES_TELEMETRY_HOME` (#78)

- The auto-refresh resolved `pricing.yaml` from an import-time constant based on
  `HERMES_HOME` only, so on a consolidated install it could write to a
  per-profile file while the cost path read the consolidated one. It now resolves
  `paths.get_pricing_path()` at call time, like every other `pricing.yaml`
  reader and writer.

### Fixed — Dashboard ignored an explicit UTC viewer timezone on Python 3.8 (#74)

- Without `zoneinfo` (Python 3.8), `_dashboard_viewer_tz` fell back to the
  server's local timezone even when the viewer asked for `UTC`. UTC is now always
  honored via `timezone.utc`; other IANA zones still need `zoneinfo`.

### Fixed — Unbounded `DISTINCT JOIN` in the model-efficiency query (#82)

- The standalone dashboard's model-efficiency query joined without bounding the
  time window, causing CPU spikes on large databases. The join is now limited to
  the requested window, and the background cache refresh no longer short-circuits
  itself (supersedes #76).

### Fixed — Standalone `hermes-telemetry` entry point was not executable (#88)

- `hermes-telemetry` and `.githooks/pre-commit` were tracked as mode `100644`, so
  the documented standalone install failed with `Permission denied` and the
  pre-commit hook was silently ignored by git. Both are now tracked `100755`,
  with a regression test.

### Fixed — `plugin.yaml` did not declare the `subagent_start` hook (#103)

- `register()` registers `subagent_start` but `plugin.yaml` did not list it under
  `provides_hooks`, so `hermes plugins validate` failed. It is now declared; no
  runtime behavior changes.

### Internal

- CI: fork PRs now run via `pull_request_target` (#86), with an explicit
  fork-checkout opt-in, read-only `contents: read` permissions, and a SECURITY
  note documenting the invariants that keep it safe (#87). Ruff is pinned via
  `.ruff-version` so a new release cannot turn CI red on its own, and `*.md` is
  excluded from `ruff format` (both landed with #88).
- Tests: a pinning test for the dated `:free` slug against the paid base model
  (#90, issue #54). No behavior change; `pricing.py` is untouched.
- Docs: corrected the dashboard slot catalogue and recorded the Hermes session
  model in `ONBOARDING.md` and `CLAUDE.md` (#96); corrected the attribution of the
  v0.8.0 agent-intelligence work to #40 instead of #8 (#109).

## [0.8.0] - 2026-07-09

### Fixed — `/stats models` mislabeled known-free $0 rows as "no price entry"

- A `$0.00` row resolved by the `:free` suffix rule (including the dated ids a
  gateway sends, e.g. `tencent/hy3-20260706:free`) or a built-in $0 seed was
  bucketed as `no price entry`, wrongly implying a lookup miss and prompting the
  user to run `/setup pricing auto`. The Notes column consulted only
  `subscription_models` and ignored `is_explicitly_priced`, so it could not tell a
  known-free model from a genuine unknown. `stats._models_block` now splits $0
  rows three ways — `subscription/free-tier` > `free tier` (`:free` suffix or
  built-in $0 seed) > `no price entry` — with a matching footer line. Documented
  the `:free`-vs-`_subscription` distinction (and the exact-id requirement for a
  `_subscription` declaration) in the README free-tier notice and ONBOARDING.

### Added — Agent intelligence: efficiency scoring, smell detection, burn-rate forecasting (#40)

Three read-only analytical capabilities over existing telemetry (no new capture,
no schema change), each exposed through the slash command, the CLI, and both
dashboards.

- **Agent Efficiency Score (0-100)** — `db.efficiency_runs()` scores each completed
  session from token productivity, error status, and turn count; `/stats efficiency`
  and `hermes-telemetry stats efficiency`. The error penalty is keyed on the real
  run statuses (`error`/`interrupted`) — the subagent-only `failed`/`cancelled`
  never reach a run, so keying on them scored failed runs as healthy.
- **AI Smell Detection** — new `smell_detector.py` with five heuristics (context
  rotation, loop trap, tool thrashing, high error rate, massive session);
  `/stats smells` and CLI. Best-effort: a broken detector is logged and skipped.
- **Burn-rate Forecasting** — `budget.burn_rate_projection()` learns a 14-day
  moving-window spend rate and projects window-end spend against the configured
  limit; `/budget forecast` and CLI, for the `global`/`cron_job`/`sender`/`profile`
  scopes.
- **Dashboards** — all three surfaced in both the standalone dashboard
  (`/api/efficiency`, `/api/smells`, `/api/budget/forecast`) and the Hermes plugin
  (`/efficiency`, `/smells`, `/forecast`). The standalone forecast route, which
  previously raised `ImportError` under the standalone loader and never resolved a
  non-global limit, was reimplemented inline with a correct config-key → engine
  scope mapping.

### Fixed

- Standalone dashboard now serves Chart.js from a vendored local asset at
  `dashboard/vendor/chart.umd.min.js` and uses jsDelivr only as a fallback.
  This removes the main cause of `Chart is not defined` when the CDN is
  unreachable or filtered.

### Added — Mixture-of-Agents (MoA) attribution (#55)

Hermes added a `moa` virtual provider: selecting a MoA preset runs N reference
models plus an aggregator. The aggregator is the acting model, but
`post_api_request` reports `provider="moa"` and `model="<preset>"` — neither
priceable — while the usage belongs to the aggregator.

- **`moa.py`** (new): resolves a preset name to its aggregator's real
  `provider`/`model` via `hermes_cli.moa_config.resolve_moa_preset`. Fully
  defensive — never raises, falls back to raw hook values.
- **`post_api_request`** re-attributes MoA calls to the aggregator's real
  provider/model, fixing the spurious `provider_assumed` pricing fallback, and
  tags the row with the preset name.
- **Schema v10** (`_migrate_v10`): `llm_calls.moa_preset` + `runs.moa_calls`.
- **`/stats summary`** and both dashboards surface `moa_calls` / `moa_preset`,
  each noting that reference-model tokens are untracked (cost is a lower bound).
- Reference-model cost is deliberately **not** estimated (no token counts
  available), and the one-shot `/moa <prompt>` path runs through the auxiliary
  (no-hook) client, so it is invisible to telemetry by Hermes' design — both
  documented as Known Limitations.

Verified against `NousResearch/hermes-agent@main`: `agent/moa_loop.py`,
`agent/auxiliary_client.py`, `hermes_cli/moa_config.py`.

### Added — Attribute async/nested subagent cost to `per_cron_job` budgets (#49)

Subagent (`delegate_task`) spend is now attributed to the parent cron job's
`per_cron_job` budget, closing the gap where child cost landed only in `global`
totals and was invisible to per-cron-job budgets.

- New `subagent_edges` table (schema **v11**) records each parent→child delegation
  edge, keyed by `child_session_id` (the only correlation key present in both
  `subagent_start` and `subagent_stop`). Populated by the newly-registered
  `subagent_start` hook (fires synchronously before async dispatch) and finalized
  by `subagent_stop` (`stopped_at` + `child_status`), with a backfill path if
  `subagent_start` was missed.
- `db.spend_by_scope("cron_job", …)` resolves the whole delegation subtree via a
  recursive CTE (seed from cron root sessions, walk edges down), so async
  (`background=true`) and nested delegation attribute to the **root** cron job.
  `global` is unchanged, so the same cost rows are only regrouped — no double
  counting. `estimated_price_share` made subtree-aware for the same scope so the
  budget hard→soft degradation stays consistent.
- Query-time `unattributed_child_cost` diagnostic (no stored state) surfaced in
  `/stats cron`; the `/budget cron` note updated to reflect the new attribution.

Phase 1 is reporting/aggregation correctness; in-path enforcement of a runaway
async child mid-flight is deferred to Phase 2.

### Added — Per-profile scoping and Global disclosures in the dashboard plugin (#66)

The plugin dashboard is now legible about scope: every surface either respects
the selected Hermes profile or is explicitly labeled **Global**. Previously the
per-profile selector only scoped Summary/Runs/Requests/Providers/Cron, while
Efficiency, AI smells, the last-run and cron-7d widgets, and the budget section
silently stayed global.

- **Profile-scoped endpoints** — `/efficiency` and `/smells` now accept an
  optional `?profile=` (filtering on `runs.profile`; smells qualifies it as
  `r.profile` across its `tool_calls`→`runs` join), joining the endpoints that
  already supported it.
- **Per-profile budget display** — `/budget` emits per-profile scope entries
  (`profile:<name>/<window>` with a `scope_id`, override-else-default limits)
  alongside the global ones when `budgets.per_profile` is configured. Display
  only; the budget engine already modeled per-profile scopes.
- **Scope disclosures** — a `ScopeBadge` on every surface shows the active scope
  (the profile name, or "All profiles") or "Global" for surfaces that cannot be
  scoped (burn-rate forecast, tier-change and model-unavailable widgets).
- **Own selectors for off-page widgets** — the last-run (`sessions:top`) and
  cron-7d (`cron:top`) slot widgets render on other shell pages and cannot see
  the Telemetry tab's selector, so each carries its own profile selector.

Plugin surface only (the standalone dashboard is a later effort); no schema
change (`runs.profile` already exists).

## [0.7.0] - 2026-06-20

### Added — Dashboard widget for model-unavailable alerts

Surfaces the 404s captured by the `api_request_error` hook on the
telemetry dashboard, sibling to the existing free→paid widget:

- New endpoint `GET /model-unavailable?window_hours=72` in
  `dashboard/plugin_api.py`, returning rows from `model_unavailable_alerts`
  newest `last_seen_at` first. Missing-table is treated as empty so the
  dashboard stays functional on pre-v8 installs.
- New `ModelUnavailableWidget` in `dashboard/dist/index.js`, rendered
  inside `TelemetryPage` next to `TierTransitionsWidget`. Same happy-path
  contract — renders nothing when the table is empty in the window so the
  card only appears when there is something to act on. Destructive badge
  if the latest alert is within 24h, outline otherwise.
- 3 new tests in `tests/test_dashboard_plugin_api.py`: empty case,
  recorded alert roundtrip, window filtering.

Rendered inside `TelemetryPage` rather than via `registerSlot` for the
same reason as the free→paid widget: the Hermes shell slot catalogue
(`sessions:top`, `cron:top`, `header-right`, `analytics:bottom`) doesn't
include an alerts slot, and unknown slot names are silently dropped.

### Docs — `api_request_error` shadowed in production by a stale backup dir

Real 404s in production (cron `nvidia-free-paid-probe` hitting
`nvidia/nemotron-3-ultra:free` after the free promo ended on 2026-06-18)
never reached the handler shipped in PR #44: no row was written to
`model_unavailable_alerts` and no warning was injected. Root cause was
**not** in this repo. A backup directory `hermes-telemetry.bak.1781730291`
left alongside the active plugin in `~/.hermes/plugins/` declared the same
`name: hermes-telemetry` in its `plugin.yaml`. Hermes' loader indexes
plugins by `name`; on collision the later-parsed manifest wins silently,
and filesystem order meant the `.bak` (pre-PR #44, no `register_hook`
for `api_request_error`) was loaded instead of the active dir. Resolution
on the server was a one-line `mv` of the backup out of `~/.hermes/plugins/`.

Documented the trap in `ONBOARDING.md § Plugin Discovery Gotcha` so the
next backup-next-to-plugin scenario gets caught at the doc, not after a
week of silent 404s. Also declared `api_request_error` in
`plugin.yaml:provides_hooks` — that field is purely declarative (the
loader does not filter against it), but it should match the registered
hooks for accuracy.

### Added — Model-unavailable alert via `api_request_error` (issue #43)

Surfaces 404s (model removed/deprecated by the provider) the same way the
free→paid alert surfaces price flips: a one-shot in-context warning on the
next `pre_llm_call`, plus a persisted row for the dashboard.

- New hook handler `api_request_error` (registered in `__init__.py`) filters
  to `status_code == 404` AND `retryable is False`. The Explorer-style "model
  not found" signal is captured at the source in
  `agent/conversation_loop.py` — long before the error is diluted into the
  `RuntimeError` that hits `cron.scheduler`.
- Schema **v7** adds `model_unavailable_alerts (model, provider, error_code,
  error_message, first_seen_at, last_seen_at, occurrences)` with PK
  `(model, provider)`. Repeated 404s for the same pair bump `occurrences`
  and refresh `last_seen_at` via `INSERT ... ON CONFLICT DO UPDATE`.
- New DB helpers: `record_model_unavailable()`, `get_model_unavailable()`,
  `recent_model_unavailable(window_hours)`.
- New in-process queue `_pending_model_unavailable_alerts[session_id]`; the
  next `pre_llm_call` injects a warning that names the model, provider,
  status code, and occurrence count, then clears the entry (one-shot per
  session, same pattern as the free→paid alert).
- 11 new tests: `test_db.py` covers upsert + idempotency + provider
  scoping + window filtering + long-message roundtrip; `test_init.py`
  covers the filter (404 non-retryable only), the queue path, one-shot
  injection, and occurrence counting against the DB.

Concrete trigger: the `nvidia/nemotron-3-ultra:free` cutoff at 8 PM ET on
2026-06-18. `hermes-agent` does NOT silently re-route a 404 — it raises
`NotFoundError` and the cron job ends. Pre-#43, telemetry recorded nothing
about the failure; the user found out only by greppping `agent.log`.

> **Schema note:** rebased on top of #42 — this migration is now **v8** (the
> v7 slot belongs to the `provider_assumed` columns added by #42).

### Fixed

- **Provider-aware pricing guard no longer records `cost=0` on real paid calls**
  (issue #42). When the only matching price for a call was an OpenRouter-sourced
  entry served by a *different* provider (e.g. Nous Portal reselling
  `moonshotai/kimi-k2.6` at the OpenRouter rate), the guard rejected it and the
  call silently recorded zero spend — the worst failure mode for a cost tracker.
  The lookup now applies the rate as a best-effort estimate, tags it
  `_provider_assumed`, and logs a one-time WARNING per `(model, provider)` pair
  advising the user to pin the rate. Source-neutral, `_subscription`, and
  `_DEFAULT_PRICING` seed entries still take precedence, so the flat-sub and
  NIM same-id collisions remain correctly priced. See
  `ONBOARDING.md § Provider-aware lookup guard`.

### Added — Provider-assumed pricing visibility (issue #42)

- Calls priced with an assumed rate are now persisted and surfaced, not just
  logged:
  - DB schema **v7** adds `llm_calls.provider_assumed` and a
    `runs.provider_assumed_calls` counter (`db._migrate_v7`), mirroring the
    existing `estimated` per-call flag.
  - `pricing.is_provider_assumed(model, provider)` predicate (parallel to
    `is_explicitly_priced`); `post_api_request` flags each affected row.
  - `/stats providers` gains an `Asm%` column; the dashboard (standalone and
    Hermes plugin) shows an `Asm?` column in Requests and an `Assumed` count in
    Providers (`provider_assumed_calls` / `provider_assumed_pct` in the API).
  - Assumed cost counts as **real spend** for budgets — it does not degrade hard
    verdicts (unlike estimated usage), since the resold-at-same-rate case is
    usually the correct number.

### Added — Free→paid model transition widget in `/telemetry`

A new `TierTransitionsWidget` rendered at the top of `TelemetryPage`
surfaces models that flipped from $0 to a paid charge within the last
72h. Builds on the existing detection in `post_api_request` (issues
#16/#32) by persisting every flip to a new `free_paid_transitions`
table, so the dashboard has a historical record beyond the one-shot
in-memory alert. Verified end-to-end against a live Hermes install.

- **Schema v6**: new `free_paid_transitions(model, provider,
  detected_at, session_id, first_paid_cost_usd, first_free_seen_at)`
  table. PRIMARY KEY `(model, provider)` — first flip per pair wins;
  later paid calls are no-ops via `INSERT OR IGNORE`.
- **`post_api_request` persistence**: in addition to queueing the
  one-shot in-context alert, every detected flip now calls
  `db.record_free_paid_transition(...)` so the dashboard sees it on
  reload.
- **Read-only endpoint** `GET /api/plugins/hermes-telemetry/tier-transitions?window_hours=72`
  returns `{ window_hours, rows: [...] }`. Missing-table is treated
  as "nothing flipped yet" so pre-v6 installs keep working.
- **Widget**: badge turns `destructive` if any flip is within 24h;
  shows up to 5 rows (`model · provider · was free for Nd · first
  charge $X`). Hides itself when no flips fall in the window — the
  page is unchanged on the happy path.
- **No new shell slot**: rendered inside `TelemetryPage`, not via
  `registerSlot()`. The Hermes shell only mounts slots from its
  catalogue (`sessions:top`, `cron:top`, `header-right`,
  `analytics:bottom`); registering an unknown slot name is a silent
  no-op. CLAUDE.md and ONBOARDING.md now record this rule so it isn't
  re-learned the hard way.

## [0.6.0] - 2026-06-16

### Added — Hermes dashboard plugin surface

`hermes-telemetry` now ships as a **Hermes dashboard plugin** in addition
to the existing standalone HTML dashboard. The Hermes web dashboard
auto-discovers the plugin from the same install path — a `git pull`
brings both surfaces up to date in lockstep. Verified against
`NousResearch/hermes-agent@main` (`hermes_cli/web_server.py` loader and
`extending-the-dashboard.md` SDK contract).

- **Dedicated `/telemetry` tab** with six sub-views: Summary, Runs,
  Requests, Providers, Cron, Budgets. Rendered as a single IIFE
  (`dashboard/dist/index.js`) using the Hermes Plugin SDK — no build
  step, no bundler, no new dependencies.
- **Four shell slots** populated via `registerSlot()`:
  - `sessions:top` — pinned card with the last run that had real
    activity (cost, tokens, model). Skips empty / aborted sessions.
  - `cron:top` — 7-day cron cost + `N FAILED` destructive badge.
  - `header-right` — compact 24h spend with semáforo against the global
    daily cap (badge variant flips to `destructive` on hard breach).
  - `analytics:bottom` — daily cost chart (Chart.js loaded from CDN with
    graceful degradation when unreachable).
- **Read-only FastAPI router** at `/api/plugins/hermes-telemetry/*`:
  `/health`, `/summary`, `/token-breakdown`, `/runs`, `/requests`,
  `/providers`, `/cron`, `/session/{id}`, `/budget`. Opens
  `telemetry.db` with `PRAGMA query_only=ON`; the plugin never writes.
- **Theme-aware** by design: uses SDK components and shadcn-tokenised
  Tailwind classes, so a user's installed theme / skin repaints the tab
  and slots automatically.

### Added — tooling

- `tools/seed_demo_data.py` — seeds an isolated `HERMES_HOME` with
  ~56 realistic sessions for screenshots / demos. Refuses to run against
  `~/.hermes` to protect production data.

### Changed

- `.github/workflows/ci.yml` gains a **version-sync check** that
  enforces `pyproject.toml == __init__.py == plugin.yaml ==
  dashboard/manifest.json`, plus `node --check` over the plugin IIFE so
  a JS syntax error fails CI.
- `.github/workflows/release.yml` extends the tag-vs-pyproject guard to
  all four version sources — a release tag cannot ship with a stale
  `dashboard/manifest.json`.
- README grows a "Hermes Dashboard Plugin" section with embedded
  screenshots (`docs/plugin/01-…` through `09-…`) and a side-by-side
  comparison of the two dashboard surfaces.
- ONBOARDING grows a "Dashboard Plugin Surface" section with verified
  source references for the loader contract and the
  `_safe_plugin_api_relpath` security rule (GHSA-5qr3-c538-wm9j).

### Decisions / non-goals

- The standalone dashboard (`dashboard/serve.py`, port 8765) is
  untouched — it remains the headless-friendly surface for cron-only
  deployments and SSH workflows. The two surfaces share the SQLite DB
  and **zero Python code**; enforced by
  `tests/test_dashboard_plugin_isolation.py`.
- `dashboard/plugin_api.py` is a single self-contained file. The Hermes
  loader imports it via `importlib.util.spec_from_file_location`, so
  relative imports are not available at load time.
- The `slots` array in `manifest.json` is documentation only — the real
  binding happens in the JS bundle via `registerSlot()`.

## [0.5.1] - 2026-06-16

### Added
- Free→paid transition detection now handles the **id-change** case (issue #32):
  when a provider drops a `:free` suffix (or renames a promo to its paid base)
  so the paid call arrives under a different model id than the recorded `:free`
  row. `db.is_free_tier_transition(model, provider)` matches a stored
  `<id>:free` row both for the bare rename (`nemotron-3-ultra:free` →
  `nemotron-3-ultra`) and for a suffixed paid id at a token boundary
  (`nemotron-3-ultra-550b-a55b`). Wired into `post_api_request` alongside the
  existing `is_known_free_model` check.
- **`:free` suffix pricing rule** (issue #32): any model id ending in `:free`
  (the OpenRouter free-tier convention, e.g.
  `nvidia/nemotron-3-ultra-550b-a55b:free`) now resolves to an explicit `$0` in
  `pricing._lookup_form`, **before** the prefix scan. This means a `:free` id is
  recorded as known-free (no estimated-price warning) and seeds the free→paid
  alert automatically — no manual `pricing.yaml` entry required. A user's
  explicit `:free` entry still overrides the rule.
- `nvidia/nemotron-3-ultra` paid seed in `_DEFAULT_PRICING` (OpenRouter rate
  pending NIM-direct confirmation). Covers both the bare id and the suffixed
  `…-550b-a55b` form via prefix, so cost becomes non-zero once the `:free`
  promo ends 2026-06-18 — which is what fires the transition alert.

### Fixed
- `:free` promo variants of any **seeded** model were billed at the seeded
  **paid** price via prefix match instead of `$0` (issue #32). Pre-fix,
  `nvidia/nemotron-3-super-120b-a12b:free` resolved to the paid `$0.09/$0.45`
  rate, and adding the `nvidia/nemotron-3-ultra` seed (above) regressed
  `nvidia/nemotron-3-ultra-550b-a55b:free` from `$0` to the paid rate. The new
  `:free` suffix rule short-circuits before the prefix scan, so free-tier calls
  correctly cost `$0` during the promo. Corrects the earlier (0.4.1) claim that
  `:free` variants "resolve to $0 via the unknown-model fallback" — they did not
  for any model with a seeded paid base.

### Notes
- **Free Nemotron Ultra users**: no action required. Calls under any
  `…nemotron-3-ultra…:free` id resolve to `$0` automatically and seed the
  free→paid alert, which fires when the `:free` promo ends 2026-06-18 and the
  gateway starts billing the bare/suffixed paid id. A manual `_subscription`
  `$0` entry in `pricing.yaml` is still honored (and overrides the rule) but is
  no longer needed.

## [0.5.0] - 2026-06-15

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

[0.9.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.7.0...v0.8.0
[0.5.1]: https://github.com/nujovich/hermes-telemetry/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/nujovich/hermes-telemetry/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.3.1...v0.4.0
[0.3.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/nujovich/hermes-telemetry/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nujovich/hermes-telemetry/releases/tag/v0.1.0
