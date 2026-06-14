# BUILD PLAN — Issue #4

**Card:** [nujovich/hermes-radar#4](https://github.com/nujovich/hermes-radar/issues/4)
**Title:** tokentelemetry: remote access + per-step traces for CLI sessions
**Board:** hermes-radar (competitive-watch)

## Decisión de Nadia

Agregar al backlog con baja prioridad. Evaluar gaps primero antes de implementar features nuevas.

## Milestones

- [ ] Milestone 1: Evaluar gaps — hermes-telemetry tiene dashboard local (HTML servido localmente); no tiene remote access, data-dir configurable, ni QR bootstrap. Evaluar si los usuarios de hermes-telemetry necesitan remote access.
- [ ] Milestone 2: Feature backlog — data-dir configurable (baja complejidad, útil para setups multi-disco); remote access (alta complejidad, considerar solo si hay demanda).
