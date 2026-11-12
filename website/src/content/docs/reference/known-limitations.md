---
title: Known limitations
description: What hermes-telemetry cannot capture or enforce, and why.
---

## Enforcement gaps

- **No mid-call abort.** A plugin cannot cancel an in-flight model call. The response that is already generating completes and is billed. The tool gate (`pre_tool_call`) stops *subsequent* work at the next tool boundary.
- **Runaway text-only sessions.** A session that generates text without calling any tool never reaches the tool gate, so budgets cannot stop it.
- **Estimated data softens hard verdicts by default.** Under `on_estimated.mode: warn_only`, a hard verdict that rests on estimated data is degraded to soft. Set `mode: enforce` if your provider reports reliable usage. See [Budgets](/hermes-telemetry/guides/budgets/#estimated-data-and-budget-degradation).

## Subagent attribution

- Child agents (`delegate_task`) run as their own sessions and fire their own hooks. Their tokens are captured independently and included in `global` totals.
- `per_cron_job` budgets and reports attribute delegated subagent spend back to the cron job that started the delegation chain. `global` remains the catch-all cap, including for any child whose link to its parent could not be resolved.
- A detached async child still self-enforces only against the `global` scope while it runs. Pausing a runaway child mid-flight through its parent's cron budget is not implemented.

## Mixture of Agents (MoA)

- Selecting a MoA preset **as your model** (`/model <preset> --provider moa`) runs several reference models plus an aggregator on every iteration. Only the **aggregator** fires a hook. The plugin re-attributes it to the aggregator's real provider and model and tags the call with the preset name (`moa_calls` and `moa_preset`, shown in `/stats` and the dashboard). **Reference-model tokens are never captured**, because they run through a Hermes auxiliary call path that fires no hooks. A MoA session's recorded cost is therefore a **lower bound**.
- The one-shot `/moa <prompt>` command is entirely invisible to telemetry, by Hermes's design. It runs both the references and the aggregator through the auxiliary path and injects their synthesis into the real main-model call. Only that final call is recorded, so MoA cost, aggregator included, is not captured, and `moa_calls` and `moa_preset` are not set. Select the preset as your model if you want MoA usage tracked.

See [Mixture of Agents](/hermes-telemetry/internals/mixture-of-agents-moa/) for the details.

## Pricing

- Automatic refresh covers OpenRouter models (through its API) and Google AI Studio (through a built-in table). Direct Anthropic and OpenAI prices need manual `pricing.yaml` entries, a new `PricingSource`, or a core pricing snapshot.
- The Google table is constant and drifts without manual updates.
- Gemini models with tiered pricing use the tier for contexts up to 200k tokens. Usage above that is undercounted.
- A private OpenAI-compatible endpoint with no cached model metadata resolves to no core snapshot, so it falls back to `pricing.yaml`.

## Profile attribution

Per-profile tags are accurate only when each profile runs in its own process. A single multiplexed gateway may tag some runs with the wrong profile. See [Profiles and shared home](/hermes-telemetry/guides/profiles/#profile-attribution-and-multiplexed-gateways).

## Database retention

`telemetry.db` grows without bound, and no automatic purge exists, with one exception: the failed-attempt ledger (`llm_call_errors`) is pruned by age, 30 days by default (see [`HERMES_TELEMETRY_ERROR_RETENTION_DAYS`](/hermes-telemetry/reference/configuration/#environment-variables)). For high-frequency cron jobs or more than about 100,000 rows in the other tables, consider manual cleanup of old rows. A general retention policy is not implemented yet.

## Gateway restart required

Enabling the plugin takes effect only after a gateway restart. Cron runs that started before the restart have no telemetry.

For the engineering view of these limits, see [Known limitations](/hermes-telemetry/internals/known-limitations/) in the Internals section.
