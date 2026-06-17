# BUILD #8 — Agent intelligence features

Card: https://github.com/nujovich/hermes-radar/issues/8

Decision: Option A — implement all three analytical capabilities.

## Milestones

- [ ] Milestone 1 — Agent Efficiency Score: add efficiency_score() query computation to stats.py, expose in /stats efficiency subcommand, and wire into dashboard
- [ ] Milestone 2 — AI Smell Detection: add smell_detector.py with heuristics to detect anti-patterns (context rotation, loop traps, tool thrashing, high error rates, massive sessions), add /stats smells subcommand, and wire alerts into dashboard
- [ ] Milestone 3 — Burn Rate Forecasting: extend budget.py with burn_rate_projection() using moving-window spend data, add /budget forecast subcommand, and wire forecast panel into dashboard
