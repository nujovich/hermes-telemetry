---
title: Configuration and data location
description: Where hermes-telemetry keeps its files, and which settings and environment variables change its behavior.
---

## Data location

All telemetry state lives in a `telemetry/` directory under your Hermes home:

```text
~/.hermes/telemetry/
├── telemetry.db        SQLite database (WAL mode)
├── telemetry.log       plugin log: errors, debug output, one-time warnings
├── pricing.yaml        your price overrides plus auto-fetched prices
├── budget.yaml         your spend guardrails
├── enforcement_mode    sidecar written at startup so the dashboards know the mode (enforce or observe)
└── .pricing_refresh    sentinel: its modification time is the last pricing refresh
```

If `pricing.yaml` and `budget.yaml` do not exist, the plugin still works. It falls back to its built-in prices and runs with budgets disabled, and the [setup wizard](/hermes-telemetry/getting-started/setup-wizard/) creates defaults on first load.

The database grows over time and nothing purges old rows automatically. See [Known limitations](/hermes-telemetry/reference/known-limitations/#database-retention).

## Plugin setting

The plugin has one setting, `mode`, configured through Hermes:

```yaml
plugins:
  entries:
    hermes-telemetry:
      settings:
        mode: enforce   # default; set to observe for telemetry-only
```

`enforce` keeps budget blocking active. `observe` records and reports without enforcing. The value is read when the plugin registers, so a change needs a restart of the Hermes process that owns the plugin. See [Setup wizard](/hermes-telemetry/getting-started/setup-wizard/#enforcement-mode).

## Environment variables

| Variable | Effect |
| --- | --- |
| `HERMES_HOME` | The Hermes home. Telemetry lives in `$HERMES_HOME/telemetry/`. Defaults to `~/.hermes`. |
| `HERMES_TELEMETRY_HOME` | Opt-in shared telemetry root. Outranks `HERMES_HOME` for `telemetry.db`, `budget.yaml`, and `pricing.yaml`. See [Profiles and shared home](/hermes-telemetry/guides/profiles/). |
| `HERMES_TELEMETRY_NO_SETUP` | Set to `1` to skip the first-load auto-setup. |
| `HERMES_PYTHON` | Python interpreter used by the `hermes-telemetry` launcher. Defaults to the interpreter Hermes uses. |

Telemetry paths resolve with the precedence `HERMES_TELEMETRY_HOME`, then `HERMES_HOME`, then `~/.hermes`.

:::note
With `HERMES_TELEMETRY_HOME` set, the database, `budget.yaml`, `pricing.yaml`, `enforcement_mode`, `telemetry.log`, and the `.pricing_refresh` sentinel all move to the shared root. Hermes's own `state.db` and `cron/` are never relocated.
:::

## File formats

- [`pricing.yaml`](/hermes-telemetry/guides/pricing/#pricingyaml): per-model prices, defaults, and metadata tags.
- [`budget.yaml`](/hermes-telemetry/guides/budgets/#budgetyaml): limits per scope, thresholds, and estimated-data handling.

For the full list of files and the database schema, see [User config files](/hermes-telemetry/internals/user-config-files/) and [Database layer](/hermes-telemetry/internals/database-layer/) in the Internals section.
