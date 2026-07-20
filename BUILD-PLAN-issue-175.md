# BUILD-PLAN — hermes-telemetry Desktop Dashboard plugin

**Card:** https://github.com/nujovich/hermes-radar/issues/175

## Decision (from card comments)

- Nadia chose **Option A** — full Hermes Desktop plugin with budget status in the
  UI plus quick actions (open dashboard, set budget cap, pause cron jobs).
- Nadia also noted: "hermes-telemetry ya cuenta con un plugin para agregar el tab a
  la interfaz web. Hacer lo mismo para desktop" — mirror the existing web dashboard
  plugin for Desktop.

## Source verification (mandatory, AGENTS.md)

Verified against `NousResearch/hermes-agent@main` before design:

- Desktop plugin frontend contract: `apps/desktop/src/contrib/plugin.ts`
  (`HermesPlugin` default export, `PluginContext` with `register`/`rest`/`socket`/
  `storage`/`i18n`), `apps/desktop/src/contrib/types.ts` (`Contribution` shape),
  `apps/desktop/src/contrib/plugins.ts` (discovery: bundled glob +
  `$HERMES_HOME/desktop-plugins/<id>/plugin.js` disk door).
- Desktop backend door: `ctx.rest(path)` resolves to `/api/plugins/<id>/...`.
  The plugin id comes from `plugin.yaml` `name: hermes-telemetry`, so the **existing
  `dashboard/plugin_api.py` already serves the Desktop plugin** (same `hermes dashboard`
  backend the Desktop app spawns). No backend discovery changes are required.
- This Python repo has no TSX build/test harness, exactly like the prebuilt,
  untested-in-repo `dashboard/dist/index.js` web plugin. The Desktop TSX is a
  contract-verified source artifact; the testable work in this repo is the backend.

## Milestones

- [ ] Milestone 1 — Add `/desktop` backend endpoint aggregating spend-vs-budget,
      last-run status, and session count for the Desktop panel, plus
      `/desktop/open-dashboard` action stub (returns the standalone dashboard URL);
      reuse existing `/cron` for pause action. Add tests.
- [ ] Milestone 2 — Add `desktop/plugin.tsx` HermesPlugin (TSX) registering a
      panel contribution that polls `/desktop` and renders budget status, plus an
      "Open Dashboard" quick action calling `/desktop/open-dashboard`.
- [ ] Milestone 3 — Add Desktop quick actions: set budget cap (writes budget.yaml
      daily/monthly limits via a new `POST /desktop/budget`) and pause cron jobs
      (calls `cron.jobs.pause_job` through a new `POST /desktop/cron/{id}/pause`).
- [ ] Milestone 4 — Document Desktop install via the `$HERMES_HOME/desktop-plugins`
      disk door in README + AGENTS.md; add an install helper / manifest note.

## How to test

```bash
ruff format --check . && ruff check . && pytest tests/ -q
# Milestone 1 specifically:
pytest tests/test_dashboard_plugin_api.py -q -k desktop
```
