# hermes-telemetry — Design & Implementation Notes

---

## Project Status (for continuing iteration)

### Repo
`nujovich/hermes-telemetry` — `main` branch, latest commit Phase 3.1.
Hermes source inspected at `/home/user/scratch/hermes-agent-src`.

### What is Built (Phases 1–3.2)

| Module | What it does |
|--------|----------|
| `db.py` | SQLite WAL, per-thread connections, schema v3. Tables: `runs`, `llm_calls`, `tool_calls`, `budget_alerts`. v2 columns: cache/reasoning tokens + `estimated` on `llm_calls`, `parent_session_id`/`estimated_llm_calls` on `runs`. v3 column: `runs.sender_id`. New function: `stats_by_provider(window_hours)`. |
| `pricing.py` | `estimate_cost(usage: dict, model: str) → float`. Per-component split (input / output / cache_read / cache_write / reasoning). Fallback via multipliers (0.10× / 1.25×). YAML override `~/.hermes/telemetry/pricing.yaml` (`models:` + `defaults:` format). Built-in table includes `owl-alpha` (Nous Portal, free). |
| `__init__.py` | Hooks: `on_session_start`, `pre_api_request` (stash approx tokens), `post_api_request` (real tokens; falls back to estimate when `usage=None`, marked `estimated=1`; **one-time WARNING if provider contains "nous" and `usage=None`**), `post_tool_call`, `post_llm_call`, `on_session_end`, `on_session_finalize`, `subagent_stop`, `pre_llm_call` (soft alert + `sender_id`), `pre_tool_call` (hard gate). Anchored regex for cron job_id. `_nous_estimated_warned` module-level set for warning dedup. Auto-setup on first load via `setup.run(interactive=False)`. |
| `stats.py` | `/stats [today\|week\|month\|cron\|raw [N]]`. **New: `/stats providers`** — per-provider table with columns total/real/estimated/est%/cost. Shows `~$cost` if estimated rows exist + estimation percentage. |
| `budget.py` | Budget engine. Scopes: `global` / `cron_job` / `sender`. Windows: `daily` / `monthly` (local timezone). Verdicts: `ok` / `soft` / `hard`. Hard degrades to soft if `estimated` + `on_estimated.mode: warn_only`. Anti-spam via `budget_alerts`. Cron pause via `cron.jobs.pause_job`. `/budget [cron \| set <scope> <window> <usd>]`. |
| `setup.py` | First-time setup wizard. Auto-runs on first plugin load. Generates `pricing.yaml` (30+ built-in models + optional OpenRouter fetch) and `budget.yaml` (global $5/day, $100/month). Also available as `/setup` slash command. |
| `pricing_refresh.py` | Auto-refresh from OpenRouter API. Merge strategy: manual entries preserved, auto-refreshed models updated. |
### User config files
```
~/.hermes/telemetry/
├── telemetry.db        ← SQLite (WAL, schema v3)
├── telemetry.log
├── pricing.yaml        ← price overrides (see config.example.yaml)
└── budget.yaml         ← guardrails (see budget.example.yaml)
```

### Tests: 115 passing
```
tests/test_db.py                      — schema v1→v3, writes, aggregations, concurrent WAL
tests/test_pricing.py                 — cache/reasoning split, no double-counting, YAML, prefixes
tests/test_init.py                    — cron regex, _is_tool_ok
tests/test_budget.py                  — ok/soft/hard engine, estimated degradation, anti-spam,
                                        cron pause, per-scope routing, /budget set
tests/test_subagent_reconciliation.py — A1: full parent+child hook sequence, assert tokens
                                        db_child == result_child (exact count), no proxy rows
tests/test_stats_providers.py         — A2: stats_by_provider real/estimated, /stats providers
                                        output format, Nous warning one-time deduplicated
tests/test_setup.py                   — setup wizard: auto/minimal/skip, pricing + budget,
                                        command handler, idempotency, owl-alpha in defaults
```

### Verifications A1 / A2 (Phase 3.1)

**A1 — Subagent reconciliation:**
- **Verified via integration test (simulated, not live).**
- Full hook sequence (parent start → parent API call → child start → child API calls → child end → post_tool_call(delegate_task) → subagent_stop → parent end) was simulated in `test_subagent_reconciliation.py`.
- **Result: `db_child_tokens == result_child_tokens` ✅** With plugin loaded in child, tokens captured once. Parent does NOT accumulate child tokens.
- **`post_tool_call(delegate_task)` does NOT generate `llm_calls` rows ✅** — zero proxy rows.
- **Global total is correct as long as child has the plugin loaded.** If child runs WITHOUT the plugin, `db_child_tokens ≈ 0` — silent undercount. How to verify live: after a session with `delegate_task`, `/stats raw 5` should show TWO runs (parent + child). If only one appears, child doesn't have the plugin.

**A2 — Nous Portal probe:**
- **Instrument built and tested against synthetic data ✅**
- `/stats providers` shows `Est%` column per provider — if Nous Portal returns `usage=None`, it shows `100%` in that column.
- `telemetry.log` receives a WARNING the first time a Nous Portal row arrives with `usage=None` (not repeated).
- **Live run pending** (requires real session against Nous Portal). Procedure in README "Provider probe" section. If `Est% == 0` → Portal returns real usage ✅. If `Est% > 0` → budgets operate on estimates and hard degrades to soft under `mode: warn_only`.

### Known Limitations (documented)
- **Subagent → job not attributable:** `delegate_task` does not return child `session_id` in any hook. The **global** total is correct (child agents register their own runs). The `per_cron_job` scope undercounts delegated spend.
- **No true hard-stop:** `pre_llm_call`/`pre_api_request` cannot abort a model call. Real enforcement is: soft alert (context injection) + tool-gate (`pre_tool_call` block) + `pause_job`. The in-flight response still completes and is billed.
- **Nous Portal `usage=None` unconfirmed live:** if Portal doesn't honor `stream_options.include_usage`, tokens are estimated and flagged `estimated=1`. The dashboard indicates this with `~$`. See "Provider probe" in README.

### Possible Next Steps (not committed)

**Observability:**
- `pre_llm_call` receives `sender_id` (confirmed in source): already captured. Enabling `per_sender` scope requires user to configure `per_sender.default.daily_usd` in `budget.yaml`.
- Export metrics to Prometheus/InfluxDB (new module `export.py`, `post_llm_call` hook).
- Local web dashboard (sqlite3 → HTML with Chart.js, no server deps).

**Budget:**
- Support custom windows (e.g. `weekly_usd`).
- Channel notification (Telegram/Discord) when soft/hard triggers.
- `dry_run: true` mode to test limits without blocking.

**Pricing:**
- Scrape prices from Anthropic API / public page (module `pricing_sync.py`).
- Alert when a new model appears with `estimated=1` so user adds it to `pricing.yaml`.

**Robustness:**
- `on_session_reset` hook (exists in VALID_HOOKS): clear session state without restart.
- Periodic vacuum / configurable retention (purge runs > N days).

---

Source: `git clone --depth=1 https://github.com/NousResearch/hermes-agent`
Inspected files: `hermes_cli/plugins.py`, `agent/conversation_loop.py`,
`model_tools.py`, `cron/scheduler.py`, `tools/delegate_tool.py`, `agent/usage_pricing.py`

---

## PluginContext API (real signatures)

```python
ctx.register_hook(hook_name: str, callback: Callable) -> None
ctx.register_command(name: str, handler: Callable, description: str = "", args_hint: str = "") -> None
ctx.register_cli_command(name: str, help: str, setup_fn: Callable, handler_fn: Callable | None = None) -> None
ctx.register_tool(name, toolset, schema, handler, ...) -> None
```

Handler signature for slash commands: `fn(raw_args: str) -> str | None`
(sync or async — both supported)

---

## Valid Hooks (`VALID_HOOKS` in `hermes_cli/plugins.py`)

```
pre_tool_call, post_tool_call, transform_terminal_output, transform_tool_result,
transform_llm_output, pre_llm_call, post_llm_call, pre_api_request, post_api_request,
on_session_start, on_session_end, on_session_finalize, on_session_reset,
subagent_stop, pre_gateway_dispatch, pre_approval_request, post_approval_response
```

---

## Hook kwargs (confirmed from source)

### `on_session_start`
Source: `agent/conversation_loop.py:295-300`
```python
session_id: str      # unique ID; cron format: "cron_{job_id}_{YYYYMMDD_HHMMSS}"
model: str           # active model name
platform: str        # "cli" | "cron" | "telegram" | "discord" | ...
```
Fired **once** at the start of a new session (not on each turn of an interactive session).

### `pre_llm_call`
Source: `agent/conversation_loop.py:702-711`
```python
session_id: str
user_message: str
conversation_history: list
is_first_turn: bool
model: str
platform: str
sender_id: str
```
Fired once per **turn** (before the API call loop). Return value used for context injection only.
**NO token data** — wrong hook for cost capture.

### `pre_api_request`
Source: `agent/conversation_loop.py:1235-1253`
```python
task_id: str
session_id: str
user_message: str
conversation_history: list
platform: str
model: str
provider: str
base_url: str
api_mode: str
api_call_count: int       # 0-indexed within this turn
request_messages: list
message_count: int
tool_count: int
approx_input_tokens: int  # APPROXIMATE token count (character-based estimate)
request_char_count: int
max_tokens: int
```
Fired before each individual API call. `approx_input_tokens` is an estimate only.

### `post_api_request`  ← **primary hook for tokens/cost/latency**
Source: `agent/conversation_loop.py:3463-3482`
```python
task_id: str
session_id: str
platform: str
model: str
provider: str
base_url: str
api_mode: str
api_call_count: int
api_duration: float       # seconds (float) — convert to ms by * 1000
finish_reason: str
message_count: int
response_model: str       # model name as reported by the API response
usage: dict | None        # see CanonicalUsage below
assistant_message: object
assistant_content_chars: int
assistant_tool_call_count: int
```

`usage` dict (from `agent/usage_pricing.py::CanonicalUsage`):
```python
{
  "input_tokens": int,       # non-cached input tokens
  "output_tokens": int,
  "cache_read_tokens": int,
  "cache_write_tokens": int,
  "reasoning_tokens": int,
  "request_count": int,
  "prompt_tokens": int,      # input + cache_read + cache_write
  "total_tokens": int,       # prompt + output
}
```
`usage` is **None** when the provider returns no usage information (some streaming providers, ACP mode). In that case we record `tokens_in=0, tokens_out=0` and log a debug warning.

### `post_llm_call`
Source: `agent/conversation_loop.py:4573-4581`
```python
session_id: str
user_message: str
assistant_response: str
conversation_history: list
model: str
platform: str
```
Fired once per turn after the tool loop completes. **NO token data.** We register
this hook only for the `end_of_turn` marker (updating per-session sums from accumulated
`post_api_request` data).

### `post_tool_call`
Source: `model_tools.py:994-1005`
```python
tool_name: str
args: dict
result: str        # JSON string returned by the tool
task_id: str
session_id: str
tool_call_id: str
duration_ms: int   # wall-clock ms for the dispatch + execution
```
Success/failure: we attempt `json.loads(result)` and check for an `"error"` key.
`result` is not always JSON (some tools return plain text), so we catch parse errors
and fall back to checking `result.startswith('{"error"')`.

### `on_session_end`
Source: `agent/conversation_loop.py:4692-4700`
```python
session_id: str
completed: bool
interrupted: bool
model: str
platform: str
```
Fired at the end of **every `run_conversation()` call** — once per turn in interactive
CLI sessions, once per cron job execution. We use this to snapshot `ended_at` and
derive session `status`.

### `on_session_finalize`
Source: `cli.py:955`, `gateway/run.py:9646`
```python
session_id: str | None
platform: str
```
Fired when the session is truly torn down (CLI atexit, gateway session expiry,
`/reset`). We update `status` to `"ok"` if not already set to `"error"`.

### `subagent_stop`
Source: `tools/delegate_tool.py:2269-2277`
```python
parent_session_id: str
child_role: str         # role/description of the subagent
child_summary: str
child_status: str       # "ok" | "error" | other
duration_ms: int
```
**NO token or cost data** in this hook. The parent agent's `session_estimated_cost_usd`
is updated after the hook fires (internal to `delegate_tool.py`), but this is not
exposed here.
Strategy: We record a `tool_call` row with `tool_name="delegate_task/subagent"` and
`ok = (child_status == "ok")`. This gives a proxy count of subagent invocations per
session.

---

## Cron job identification

There is **no `cron_job_id` kwarg** passed to any hook.

Extraction strategy: when `platform == "cron"`, the `session_id` follows the format
`cron_{job_id}_{YYYYMMDD_HHMMSS}` (confirmed in `cron/scheduler.py:1392`).

Implemented with an anchored regex (R4) rather than `split("_")`, because job_ids
can themselves contain underscores (`cron_my_job_2_20260101_120000` → `my_job_2`):

```python
CRON_SESSION_RE = re.compile(r"^cron_(?P<job_id>.+)_\d{8}_\d{6}$")

def _extract_cron_job_id(session_id: str, platform: str) -> str | None:
    if platform != "cron":
        return None
    m = CRON_SESSION_RE.match(session_id)
    if m:
        return m.group("job_id")
    logger.warning(...)   # format changed → cron_job_id NULL, surfaced loudly
    return None
```

---

## Concurrency model

Cron jobs run in a `ThreadPoolExecutor` (see `cron/scheduler.py`), so multiple jobs
can write to the DB concurrently from different threads.

Decision: **per-thread SQLite connections** via `threading.local()`. Each thread
opens its own connection to the same WAL-mode DB file. SQLite WAL allows concurrent
readers + one writer; the `busy_timeout=5000` ensures write collisions retry for 5s
before raising. This is the standard SQLite concurrency pattern and requires no
application-level locking.

Alternative (rejected): a single shared connection protected by a `threading.Lock`.
This is simpler but serializes all writes and is a bottleneck if many cron jobs run
in parallel. Per-thread connections are marginally more memory-intensive but scale
better.

---

## Metrics: what is measurable vs. estimated

| Metric | Status | Source |
|--------|--------|--------|
| Tokens in (non-cached) | ✅ Real | `post_api_request.usage.input_tokens` |
| Tokens out | ✅ Real | `post_api_request.usage.output_tokens` |
| Cache read tokens | ✅ Real | `post_api_request.usage.cache_read_tokens` |
| Cache write tokens | ✅ Real | `post_api_request.usage.cache_write_tokens` |
| Reasoning tokens | ✅ Real | `post_api_request.usage.reasoning_tokens` |
| API call latency | ✅ Real | `post_api_request.api_duration` (seconds → ms) |
| Tool call latency | ✅ Real | `post_tool_call.duration_ms` |
| Model name | ✅ Real | `post_api_request.model` (or `response_model`) |
| Provider name | ✅ Real | `post_api_request.provider` |
| Platform | ✅ Real | `on_session_start.platform` |
| Cron job ID | ✅ Real (parsed) | `session_id` prefix parsing |
| Session duration | ✅ Real (wall time) | `started_at` → `ended_at` (last turn) |
| Tool success/failure | ✅ Real | Parse `result` JSON for `"error"` key |
| Subagent count | ✅ Real (proxy) | `subagent_stop` hook count |
| Cost (USD) | ⚠️ Estimated | Local pricing table × token counts (cache/reasoning split, R1) |
| Tokens when `usage=None` | ⚠️ Estimated, flagged | Fallback estimate (`approx_input_tokens` + `assistant_content_chars/4`), row marked `estimated=1` (R2) — **never silently 0** |
| Per-turn token breakdown | ⚠️ Aggregated | Turn-level aggregation of `post_api_request` calls |
| Subagent token cost (global total) | ✅ Real | Child agents fire their own `post_api_request` → recorded as independent runs; `/stats` and the global budget already include them |
| Subagent cost attributed to parent cron job | ❌ Not available | `delegate_task` result carries no child `session_id` (verified, Phase 3 A3) → per-cron-job spend undercounts delegated work |

**Cost** is always an *estimate* computed from a locally-maintained pricing table.
We do not call any provider pricing API. Users can override pricing via
`~/.hermes/telemetry/pricing.yaml`.

---

## Design Decisions & Refinements

**R1 — Cache/reasoning cost split:**
Cost is computed per-component (input, output, cache_read, cache_write, reasoning) rather than from `prompt_tokens` or `total_tokens`. This avoids double-counting and correctly models providers that charge differently for cache hits vs fresh input. `prompt_tokens` and `total_tokens` from the usage dict are stored for reference but never used in cost calculation.

**R2 — Nous Portal usage availability:**
Hermes sends `stream_options: {"include_usage": True}` (in `agent/chat_completion_helpers.py:1707`) for ALL OpenAI-compatible streaming providers. Nous Portal uses `chat_completions` mode against `nousresearch.com`. Whether Nous Portal honors `include_usage` in streaming responses is **unconfirmed without live testing** — it depends on the Portal API implementation. If it does, usage will be real. If it doesn't, usage will be None and the fallback estimation kicks in (marked `estimated=1`). The `estimated` column makes this distinguishable in the dashboard.

**R3 — Subagent session architecture (confirmed):**
- Child `AIAgent` instances are created WITHOUT an explicit `session_id` → they auto-generate one (`{YYYYMMDD_HHMMSS}_{uuid6}` format, from `agent/agent_init.py:972-974`)
- Child agents call `run_conversation()` which fires full hook lifecycle including `on_session_start`, `post_api_request`, `on_session_end`
- **Conclusion**: child tokens ARE captured independently. `/stats` totals already include subagent costs (as separate runs)
- **Limitation**: `subagent_stop` receives `parent_session_id` but NOT `child_session_id` (see `tools/delegate_tool.py:2269-2277`). `on_session_start` receives `session_id` but NOT `parent_session_id`. Therefore parent-child attribution CANNOT be established from hooks alone. `runs.parent_session_id` column added for future use but never populated in v0.1.

**R4 — Cron job ID regex:**
Anchored regex `^cron_(?P<job_id>.+)_\d{8}_\d{6}$` handles job IDs containing underscores. A naive `split("_")` would break on IDs like `my_job_2`. On mismatch, a WARNING is logged and `cron_job_id` is set to NULL — surfaced loudly rather than silently wrong.

---

## Phase 3: Budget Guardrails — Source Audit (A1–A4)

Before building the budget engine, four things were verified against the real
source. Findings drove the design.

**A1 — cache/reasoning cost test (R1):** Implementation was already correct
(per-token split, `prompt_tokens` ignored to avoid double-counting). The test
suite covered `cache_read`, reasoning-as-output, no-double-count and the
multiplier fallback, but lacked a case exercising `cache_write_tokens != 0`.
Added `test_cache_read_and_write_split_exact` (exact per-component sum + cheaper
than all-fresh-input despite the cache_write premium).

**A2 — cron regex canary (R4):** Already solid — anchored regex, underscore
job_ids, WARNING + NULL on mismatch, canary comment. No change.

**A3 — subagent → parent link:** NOT recoverable. `delegate_task`
(`tools/delegate_tool.py:2303-2309`) returns `{results:[{tokens, model,
api_calls, status, ...}], total_duration_seconds}` — **no child `session_id`**;
`post_tool_call` gets that JSON verbatim (`model_tools.py:999`); `subagent_stop`
passes no child id either. The result *does* carry per-child `tokens`+`model`,
but attributing it would double-count against the child's own independent runs,
so we don't. **Decision:** budget enforces reliably at **global/session** scope
(total is correct); `per_cron_job` scope explicitly warns that it excludes
subagent spend.

**A4 — what can actually stop spend:**
| Primitive | Can abort/deny? | Mechanism |
|-----------|-----------------|-----------|
| `pre_llm_call` return | ❌ No | Used only for context injection (`conversation_loop.py:687-722`) |
| `pre_api_request` return | ❌ No | Return value discarded (`conversation_loop.py:1235-1255`) |
| `pre_approval_request` / `post_approval_response` | ❌ No | Documented "observers only" (`plugins.py:160-167`) |
| `pre_tool_call` return | ✅ **Yes** | `{"action":"block","message":...}` aborts the tool (`plugins.py:1666-1707`) |
| `cron.jobs.pause_job(job_id, reason)` | ✅ Yes | Pauses future runs of a cron job |
| `agent.interrupt()` | ✅ Yes, but | Needs the agent object, not exposed via hook kwargs |

**Enforcement level achieved:** there is **no true mid-call hard-stop** of the
model API call. The realistic maximum is:
- **soft** (≥ `soft_pct`): one-time-per-window notice injected via `pre_llm_call`
  context (anti-spam ledger = `budget_alerts` table);
- **hard** (≥ `hard_pct`): a **tool-gate** via `pre_tool_call` — blocking every
  subsequent tool ends the agentic loop at the next boundary (the in-flight model
  response still completes and is billed), plus **`pause_job`** for cron futures.
- Budgets resting on `estimated=1` rows degrade hard→soft when
  `on_estimated.mode == "warn_only"` (a budget built on estimates shouldn't hard-cut).

The verdict cache (5 s TTL) keeps the per-tool-call gate from re-querying SQLite
within a turn; spend only changes when a new `post_api_request` is recorded.
