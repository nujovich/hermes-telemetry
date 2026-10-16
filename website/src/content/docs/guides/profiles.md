---
title: Profiles and shared home
description: Share one telemetry database, budget, and pricing file across Hermes profiles.
---

By default each Hermes profile keeps its own telemetry. This page explains how to consolidate several profiles into a single cost center, and what that does and does not change.

## The default: one telemetry directory per profile

The telemetry directory resolves from your Hermes home. For the default profile that is `~/.hermes/telemetry/`; for a named profile it is that profile's own home. Each profile therefore has its own `telemetry.db`, `budget.yaml`, and `pricing.yaml`.

## Shared telemetry home (`HERMES_TELEMETRY_HOME`)

If you run several profiles and want one **shared cost center**, with one `telemetry.db`, one `budget.yaml`, and one `pricing.yaml` that every profile reads and writes, set the opt-in `HERMES_TELEMETRY_HOME` to a common directory in each profile's environment:

```bash
export HERMES_TELEMETRY_HOME=~/.hermes-shared
```

Telemetry paths resolve with this precedence:

1. `HERMES_TELEMETRY_HOME`
2. `HERMES_HOME`
3. `~/.hermes`

The files always live under a `telemetry/` subdirectory of the chosen root. When the variable is unset, nothing changes: each profile keeps its own profile-tagged telemetry directory.

:::note
`HERMES_TELEMETRY_HOME` relocates **telemetry files only**: `telemetry.db`, `budget.yaml`, and `pricing.yaml`. It never moves Hermes's own `state.db` or `cron/` directory, which stay on `HERMES_HOME`.
:::

Both [dashboards](/hermes-telemetry/guides/dashboard/) honor the variable, and the plugin dashboard adds a profile filter when the shared database holds profile data.

## Consolidating profiles with `sync-profiles`

Setting `HERMES_TELEMETRY_HOME` by hand in every profile's `.env` gets tedious and error-prone once you have more than a couple of profiles. The `sync-profiles` command points every Hermes profile at one shared home so they all read the same `pricing.yaml` and `budget.yaml` and write to the same `telemetry.db`.

Run it **from the default profile**, whose home (`~/.hermes`) is the shared target:

```bash
# Dry run (default): prints the plan, changes nothing
hermes telemetry sync-profiles

# Apply: writes HERMES_TELEMETRY_HOME into each profile's .env
hermes telemetry sync-profiles --apply

# Limit to specific profiles, or use a custom shared home
hermes telemetry sync-profiles coder writer --apply
hermes telemetry sync-profiles --telemetry-home ~/.hermes-shared --apply

# Machine-readable report
hermes telemetry sync-profiles --json
```

The standalone `hermes-telemetry sync-profiles` accepts the same arguments.

### What it does, and what it deliberately does not

- **It writes only** a single `HERMES_TELEMETRY_HOME` line into each profile's `.env`. The write is atomic and idempotent; existing lines, comments, and an `export ` prefix are preserved. Hermes loads `<home>/.env` before it loads plugins, so the setting reaches each profile's process on its own, with no manual `source` step.
- **It treats `config.yaml` as read-only.** It warns when a profile has not enabled the plugin (`hermes plugins install hermes-telemetry -p <name> --enable` if the plugin is not installed, or `hermes plugins enable hermes-telemetry -p <name>` if it is) but never edits the file.
- **It is going-forward only.** It does not backfill rows that a profile already wrote to its own pre-consolidation `telemetry.db`.

:::caution
With no profile names, `--apply` targets **every** profile under `~/.hermes/profiles/*`. Pass explicit names to narrow it, and always read the dry run first. If you run it from a named profile, `--apply` refuses unless you also pass `--yes`.
:::

## Profile attribution and multiplexed gateways

Consolidation, meaning one database, one pricing file, and one budget, works however your profiles run. Per-profile **attribution** is a separate matter. It is the `profile` tag behind the dashboard's profile filter and the `per_profile` budgets, and it is accurate only when each profile runs in its **own process**, such as a dedicated per-profile gateway or cron. A single multiplexed gateway that serves several profiles from one process may tag some runs with the wrong profile, typically `default`, because the profile is derived from that process's `HERMES_HOME`.

Runs whose profile could not be resolved keep a null profile and are excluded from `per_profile` spend and budgets.

## Known gaps with a shared home

- The [setup wizard](/hermes-telemetry/getting-started/setup-wizard/) still looks for its files under `HERMES_HOME`. Create the shared `pricing.yaml` and `budget.yaml` directly, or with `/budget set`.
- The 24-hour pricing refresh timer is tracked per profile, so each profile may trigger its own refresh. This is harmless, because every refresh merges into the one shared file.

For the design details, see [Canonical telemetry home](/hermes-telemetry/internals/telemetry-home/).
