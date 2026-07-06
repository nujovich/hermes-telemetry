# BUILD PLAN — Card #164

Card: https://github.com/nujovich/hermes-radar/issues/164

## Decision

Nadia chose **Option B** — Chip-aware local power only.

## Milestones

- [ ] Milestone 1 — Add `local_power.py` module with Apple Silicon detection (sysctl hw.machine) and wattage estimation
- [ ] Milestone 2 — Wire into pricing.py: when model is local (ollama/llama.cpp), use chip-aware wattage as cost basis
- [ ] Milestone 3 — Add /stats local-power subcommand showing estimated local cost