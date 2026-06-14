# BUILD PLAN — Issue #4
**Card:** [nujovich/hermes-radar#4](https://github.com/nujovich/hermes-radar/issues/4)
**Title:** tokentelemetry: remote access + per-step traces for CLI sessions
**Board:** hermes-radar (competitive-watch)

## Nadia's Decision
Add to the backlog with low priority. Evaluate gaps first before implementing new features.

## Milestones
- [ ] Milestone 1: Evaluate gaps — hermes-telemetry has a local dashboard (HTML served locally); it has no remote access, no configurable data-dir, and no QR bootstrap. Assess whether hermes-telemetry users actually need remote access.
- [ ] Milestone 2: Feature backlog — configurable data-dir (low complexity, useful for multi-disk setups); remote access (high complexity, only consider if there's demand).
