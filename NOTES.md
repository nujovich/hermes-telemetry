# hermes-telemetry — API Research Notes

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

```python
def _extract_cron_job_id(session_id: str, platform: str) -> str | None:
    if platform != "cron":
        return None
    # "cron_abc123_20260101_120000" → "abc123"
    parts = session_id.split("_", 2)  # ["cron", job_id, timestamp_part]
    if len(parts) >= 2:
        return parts[1]
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
| Cost (USD) | ⚠️ Estimated | Local pricing table × token counts |
| Tokens when `usage=None` | ❌ Not available | Provider didn't return usage; recorded as 0 |
| Per-turn token breakdown | ⚠️ Aggregated | Turn-level aggregation of `post_api_request` calls |
| Subagent token cost | ❌ Not available | No token data in `subagent_stop`; logged as proxy row |

**Cost** is always an *estimate* computed from a locally-maintained pricing table.
We do not call any provider pricing API. Users can override pricing via
`~/.hermes/telemetry/pricing.yaml`.
