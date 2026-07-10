BUILD PLAN -- Issue #157: Tiered storage analytics with date/range filters

Card: https://github.com/nujovich/hermes-radar/issues/157
Decision: Option A (tiered storage completo, matching tokentelemetry capabilities)

## Milestones

- [ ] Milestone 1 -- Add tiered storage tables to db.py (daily_rollups, weekly_rollups, monthly_rollups) with migration to schema v10
- [ ] Milestone 2 -- Implement upsert_rollups() triggered on end_run + periodic compaction
- [x] Milestone 3 -- Add retention config (plugin config: retention_days per tier) with auto-prune
- [ ] Milestone 4 -- Extend /stats with --granularity flag (day|week|month) and combined agent/model filters
- [ ] Milestone 5 -- Extend dashboard API with bucketed endpoints and tier-aware queries