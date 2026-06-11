# README: Test Count + Provider Badge Update

**Scope:** Documentation only — no code changes.

## Changes

### Test count: 94 → 210

| Location | Change |
|----------|--------|
| README badge | `Tests-94%20passing` → `Tests-210%20passing` |
| README "94 tests pass" row (validation table) | → `210 tests pass` |
| README "Test suite (94 tests)" header | → `Test suite (210 tests)` |
| README test file table | Rewrite with all 13 files and real counts (see below) |

New test table:

| File | Tests | Notes |
|------|-------|-------|
| `test_pricing.py` | 40 | |
| `test_telemetry_cli.py` | 32 | New — CLI subcommands, text + JSON output |
| `test_db.py` | 29 | |
| `test_setup.py` | 21 | |
| `test_budget.py` | 20 | |
| `test_stats_providers.py` | 14 | |
| `test_pricing_refresh.py` | 13 | |
| `test_init.py` | 10 | |
| `test_subagent_reconciliation.py` | 9 | |
| `test_dashboard.py` | 9 | |
| `test_stats_models.py` | 8 | |
| `test_pricing_hot_reload.py` | 3 | |
| `test_isolation.py` | 2 | |
| **Total** | **210** | |

### Providers: OpenRouter ONLY

| Location | Change |
|----------|--------|
| README badge | `Providers-OpenRouter%20%7C%20OpenAI%20%7C%20Anthropic` → `Providers-OpenRouter` |
| README dashboard description | `(nous / openrouter / anthropic)` → `(openrouter)` |
| README pricing table description (line ~490) | Remove provider list, → "Prices auto-fetched from the OpenRouter API." |
