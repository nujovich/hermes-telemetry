---
title: Setup wizard
description: The first-load auto-setup, the /setup command, and how to re-run it.
---

hermes-telemetry creates sensible defaults for `pricing.yaml` and `budget.yaml` the first time it loads. You can also configure either file at any time with the `/setup` slash command.

## Enforcement mode

The plugin setting `mode` controls whether budget limits are enforced:

```yaml
plugins:
  entries:
    hermes-telemetry:
      settings:
        mode: enforce   # default; set to observe for telemetry-only
```

| Mode | Behavior |
| --- | --- |
| `enforce` (default) | Budget guardrails are active: soft notices, hard tool blocks, and cron pauses. Auto-setup may create `budget.yaml`. |
| `observe` | Telemetry and `/budget` reporting only. The plugin does not create a budget automatically, inject budget notices, block tools, or pause cron jobs. |

`enforce` is the default so that existing installs keep budget blocking after an upgrade. A missing, invalid, or unrecognized `mode`, and Hermes hosts that cannot read plugin settings, all resolve to `enforce`. The mode is read when the plugin registers, so changing it requires restarting the Hermes process that owns the plugin.

In `observe` mode, if a `budget.yaml` exists, `/budget` and the dashboard budget panel state plainly that the limits are not enforced.

## Auto-setup on first load

When the plugin loads and `pricing.yaml` or `budget.yaml` is missing, it generates defaults without prompting:

- **Pricing.** Merges the built-in seed table (34 models from Anthropic, OpenAI, DeepSeek, Google, Meta, and Nous) with every fixed-price model from the OpenRouter models API. If the network fetch fails, only the built-in seed is written. New prices take effect immediately, with no gateway restart.
- **Budget** (`enforce` mode only). Writes a conservative global budget of `$5.00` per day and `$100.00` per month, with an 80% soft threshold and a 100% hard threshold. In `observe` mode no budget file is created.

Existing files are never overwritten. To skip auto-setup entirely, set `HERMES_TELEMETRY_NO_SETUP=1` in the environment of the Hermes process.

## The `/setup` command

```text
/setup                     show which files exist
/setup pricing auto        built-in defaults + OpenRouter fixed-price models
/setup pricing minimal     built-in defaults only (no network)
/setup pricing skip        leave pricing unconfigured
/setup budget default      global budget: $5/day, $100/month
/setup budget custom       print the /budget set commands for your own limits
/setup budget skip         no budget (costs are tracked, nothing is enforced)
```

### Pricing options

| Option | Models | Network |
| --- | --- | --- |
| `auto` | Built-in seed plus all OpenRouter fixed-price models | Yes (OpenRouter API) |
| `minimal` | Built-in seed only | No |
| `skip` | None. Unrecognized models record `$0.00` cost | No |

### Budget options

| Option | Behavior |
| --- | --- |
| `default` | Global `$5.00` per day and `$100.00` per month. Soft warning at 80%, hard block at 100% while `mode: enforce` is active. |
| `custom` | Prints the `/budget set` commands to configure limits manually. |
| `skip` | Costs are tracked but never enforced. |

## Re-running setup

Only the **first-load auto-setup** skips files that already exist. The `/setup pricing auto`, `/setup pricing minimal`, and `/setup budget default` commands **overwrite** `pricing.yaml` or `budget.yaml` unconditionally, so running them discards hand edits such as `_subscription` tags, custom prices, and custom limits. Back the file up first, or edit it directly (or use `/budget set`) when you only want to change a value.

To start over from the defaults, back up and delete the file, then run the matching command:

```bash
# Reprice from scratch
rm ~/.hermes/telemetry/pricing.yaml
```

```text
/setup pricing auto
```

```bash
# Reset the budget
rm ~/.hermes/telemetry/budget.yaml
```

```text
/setup budget default
```

:::note
Pricing changes take effect immediately. Budget file changes are picked up by the plugin's file watcher when `watchdog` is installed. Without it, restart the gateway after editing `budget.yaml` by hand.
:::

:::note
The wizard resolves `pricing.yaml` and `budget.yaml` through the same path logic as the rest of the plugin, so the shared [`HERMES_TELEMETRY_HOME`](/hermes-telemetry/guides/profiles/) override is honored. Writes to `budget.yaml` take the same lock as `/budget set`, and merge into whatever is already in the file.
:::
See [Configuration and data location](/hermes-telemetry/reference/configuration/) for the full file layout and [Pricing](/hermes-telemetry/guides/pricing/) and [Budgets](/hermes-telemetry/guides/budgets/) for the file formats.
