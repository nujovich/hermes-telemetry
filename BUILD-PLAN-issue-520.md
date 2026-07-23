# BUILD-PLAN — Card #520: Loop telemetry

**Card:** https://github.com/nujovich/hermes-radar/issues/520
**Decision:** Nadia approved loop telemetry implementation.

## Context

TokenTelemetry (competitor) added loop detection via scheduling tool calls (CronCreate, ScheduleWakeup, CronDelete), per-session loop facts with lifecycle state machine, loop badge/Card in trace UI, Recurring loops analytics section, and footprint attribution separating loop fire-response turns from total session tokens.

Hermes-telemetry must implement equivalent capabilities.

## Milestones

- [ ] Milestone 1 — Add loop detection engine in `loop_detector.py`: scan session tool calls for scheduling patterns (CronCreate, ScheduleWakeup, CronDelete), detect loop type and cadence
- [ ] Milestone 2 — Add `loop_facts` table to db.py with migration: per-session loop facts with lifecycle state machine (active/expired/cancelled/unknown), recomputed on each request
- [ ] Milestone 3 — Add loop badge to trace header + LoopCard component: surface loop prompt, cadence, fire count, timestamps in trace UI
- [ ] Milestone 4 — Add Recurring loops analytics section to dashboard: tiles (active/expired/runs/cost) + per-loop list
- [ ] Milestone 5 — Add footprint attribution: separate loop fire-response token counts from total session tokens in analytics
