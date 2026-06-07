"""hermes-telemetry plugin — observability for Hermes Agent.

Captures tokens, cost, latency, and tool calls per session and cron job.
Persists to local SQLite. Exposes /stats slash command.

All telemetry capture goes through hooks; zero agent-visible surface except /stats.
Errors are caught silently — telemetry never takes down a session.
"""

from __future__ import annotations

__version__ = "0.3.1"

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Approximate token store for pre_api_request → post_api_request correlation
# ---------------------------------------------------------------------------
_approx_store: dict[tuple, int] = {}
_approx_lock = threading.Lock()

# ---------------------------------------------------------------------------
# One-time warning set: tracks providers already warned about estimated usage.
# Used in post_api_request to avoid repeating the Nous Portal probe warning.
# ---------------------------------------------------------------------------
_nous_estimated_warned: set = set()

# Characters per token estimate (used when usage=None)
_CHARS_PER_TOKEN = 4

# ---------------------------------------------------------------------------
# Cron session ID regex
# ---------------------------------------------------------------------------
CRON_SESSION_RE = re.compile(r"^cron_(?P<job_id>.+)_\d{8}_\d{6}$")
# If this test breaks, Hermes changed the cron session_id format — check cron/scheduler.py


def _setup_log_file() -> None:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    log_dir = hermes_home / "telemetry"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "telemetry.log"

    fh = logging.FileHandler(str(log_file))
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    plugin_logger = logging.getLogger("hermes_telemetry")
    plugin_logger.addHandler(fh)
    plugin_logger.setLevel(logging.DEBUG)
    plugin_logger.propagate = False


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_pricing_refresh(log: logging.Logger) -> None:
    """Refresh pricing from remote sources once per 24h.

    Uses a sentinel file (~/.hermes/telemetry/.pricing_refresh) to track
    last refresh time. Safe to call on every plugin load.
    """
    import time as _time

    sentinel = (
        Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        / "telemetry"
        / ".pricing_refresh"
    )
    ttl = 86400  # 24h
    try:
        if sentinel.exists():
            elapsed = _time.time() - sentinel.stat().st_mtime
            if elapsed < ttl:
                return
        from . import pricing, pricing_refresh

        # refresh_pricing returns (changes, manual_overrides).
        changes, _overrides = pricing_refresh.refresh_pricing()
        if changes:
            log.info("pricing auto-refreshed: %d model(s) updated", len(changes))
            # Drop the in-process pricing cache so the new prices are picked up
            # immediately — no gateway restart needed.
            pricing.reload_custom_pricing()
        else:
            log.debug("pricing auto-refresh: no changes")
        sentinel.touch()
    except Exception as exc:
        log.warning("pricing auto-refresh failed (non-fatal): %s", exc)


def _extract_cron_job_id(session_id: str, platform: str) -> str | None:
    """Extract the job ID from a cron session ID.

    Cron session IDs have the format: cron_{job_id}_{YYYYMMDD_HHMMSS}
    (see cron/scheduler.py:1392 in hermes-agent source).
    Uses CRON_SESSION_RE for robust parsing — logs a warning if the format
    doesn't match so that format changes are immediately visible.
    """
    if platform != "cron":
        return None
    m = CRON_SESSION_RE.match(session_id)
    if m:
        return m.group("job_id")
    logger.warning(
        "hermes-telemetry: platform='cron' but session_id %r doesn't match expected "
        "format cron_{job_id}_{YYYYMMDD_HHMMSS} — cron_job_id will be NULL. "
        "If this recurs, check cron/scheduler.py in hermes-agent.",
        session_id,
    )
    return None


def _is_tool_ok(result: Any) -> bool:
    """Determine success/failure from a tool result string."""
    if not isinstance(result, str):
        return True
    if result.startswith('{"error"'):
        return False
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and "error" in parsed:
            return False
    except (json.JSONDecodeError, ValueError):
        pass
    return True


def register(ctx) -> None:  # noqa: ANN001
    _setup_log_file()
    tele_log = logging.getLogger("hermes_telemetry")

    from . import budget, db, pricing, setup, stats

    # ------------------------------------------------------------------
    # Auto-refresh pricing from remote sources (once per 24h)
    # ------------------------------------------------------------------
    _try_pricing_refresh(tele_log)

    # ------------------------------------------------------------------
    # on_session_start
    # Fired once per new session.
    # kwargs: session_id, model, platform
    # ------------------------------------------------------------------
    def on_session_start(session_id: str = "", model: str = "", platform: str = "", **_kw) -> None:
        try:
            cron_job_id = _extract_cron_job_id(session_id, platform)
            db.start_run(
                session_id=session_id, model=model, platform=platform, cron_job_id=cron_job_id
            )
            tele_log.debug(
                "session_start session=%s platform=%s cron_job=%s",
                session_id,
                platform,
                cron_job_id,
            )
        except Exception as exc:
            tele_log.error("on_session_start failed: %s", exc)

    ctx.register_hook("on_session_start", on_session_start)

    # ------------------------------------------------------------------
    # pre_api_request
    # Fired before each individual API call.
    # We stash approx_input_tokens so post_api_request can use it as a
    # fallback estimate when usage=None.
    # kwargs: session_id, api_call_count, approx_input_tokens, ...
    # ------------------------------------------------------------------
    def pre_api_request(
        session_id: str = "",
        api_call_count: int = 0,
        approx_input_tokens: int = 0,
        **_kw,
    ) -> None:
        with _approx_lock:
            _approx_store[(session_id, api_call_count)] = approx_input_tokens

    ctx.register_hook("pre_api_request", pre_api_request)

    # ------------------------------------------------------------------
    # post_api_request  ← PRIMARY source for tokens/cost/latency
    # Fired after each individual API call within a turn.
    # kwargs: session_id, model, provider, api_duration (seconds),
    #         usage (dict with input_tokens/output_tokens or None),
    #         api_call_count, assistant_content_chars
    # ------------------------------------------------------------------
    def post_api_request(
        session_id: str = "",
        model: str = "",
        provider: str = "",
        api_duration: float = 0.0,
        usage: dict | None = None,
        response_model: str | None = None,
        api_call_count: int = 0,
        assistant_content_chars: int = 0,
        **_kw,
    ) -> None:
        try:
            effective_model = response_model or model or ""

            # Always clean up the approx store for this call
            with _approx_lock:
                approx_in = _approx_store.pop((session_id, api_call_count), 0)

            if isinstance(usage, dict):
                tokens_in = int(usage.get("input_tokens") or 0)
                tokens_out = int(usage.get("output_tokens") or 0)
                cache_read_tok = int(usage.get("cache_read_tokens") or 0)
                cache_write_tok = int(usage.get("cache_write_tokens") or 0)
                reasoning_tok = int(usage.get("reasoning_tokens") or 0)
                estimated = False
            else:
                # usage=None: estimate from pre_api_request stash + response chars
                tele_log.debug(
                    "post_api_request: usage=None for session=%s — estimating from "
                    "approx_input_tokens=%d + assistant_content_chars=%d",
                    session_id,
                    approx_in,
                    assistant_content_chars,
                )
                tokens_in = approx_in
                tokens_out = int(assistant_content_chars / _CHARS_PER_TOKEN)
                cache_read_tok = 0
                cache_write_tok = 0
                reasoning_tok = 0
                estimated = True
                # One-time warning: fires the first time a Nous Portal call returns
                # usage=None so you don't have to grep logs to know your provider
                # isn't returning real usage data.
                prov_lower = (provider or "").lower()
                if "nous" in prov_lower and provider not in _nous_estimated_warned:
                    _nous_estimated_warned.add(provider)
                    tele_log.warning(
                        "hermes-telemetry: provider=%r returned usage=None — cost for "
                        "this session is ESTIMATED (not real). Run /stats providers to "
                        "see the real vs estimated breakdown. If this persists, budget "
                        "hard-verdicts will be degraded to soft (on_estimated.mode: "
                        "warn_only). To silence this warning, set mode: enforce.",
                        provider,
                    )

            full_usage = {
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cache_read_tokens": cache_read_tok,
                "cache_write_tokens": cache_write_tok,
                "reasoning_tokens": reasoning_tok,
            }
            cost = pricing.estimate_cost(full_usage, effective_model)
            latency_ms = int(api_duration * 1000)

            db.record_llm_call(
                session_id=session_id,
                ts=_utcnow(),
                model=effective_model,
                provider=provider,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                latency_ms=latency_ms,
                cache_read_tokens=cache_read_tok,
                cache_write_tokens=cache_write_tok,
                reasoning_tokens=reasoning_tok,
                estimated=estimated,
            )
        except Exception as exc:
            tele_log.error("post_api_request hook failed: %s", exc)

    ctx.register_hook("post_api_request", post_api_request)

    # ------------------------------------------------------------------
    # post_tool_call
    # Fired after each tool execution.
    # kwargs: tool_name, result, duration_ms, session_id, task_id, args
    # ------------------------------------------------------------------
    def post_tool_call(
        tool_name: str = "",
        result: Any = None,
        duration_ms: int = 0,
        session_id: str = "",
        **_kw,
    ) -> None:
        try:
            ok = _is_tool_ok(result)
            db.record_tool_call(
                session_id=session_id,
                ts=_utcnow(),
                tool_name=tool_name,
                ok=ok,
                latency_ms=duration_ms,
            )
        except Exception as exc:
            tele_log.error("post_tool_call hook failed: %s", exc)

    ctx.register_hook("post_tool_call", post_tool_call)

    # ------------------------------------------------------------------
    # subagent_stop
    # Fired when a delegated subagent finishes. No token data available.
    # We log a proxy tool_call row so subagent invocations are visible.
    # kwargs: parent_session_id, child_role, child_status, duration_ms
    # ------------------------------------------------------------------
    def subagent_stop(
        parent_session_id: str = "",
        child_role: str = "",
        child_status: str = "",
        duration_ms: int = 0,
        **_kw,
    ) -> None:
        try:
            ok = child_status in ("ok", "success", "complete", "")
            db.record_tool_call(
                session_id=parent_session_id,
                ts=_utcnow(),
                tool_name="delegate_task/subagent",
                ok=ok,
                latency_ms=duration_ms,
            )
        except Exception as exc:
            tele_log.error("subagent_stop hook failed: %s", exc)

    ctx.register_hook("subagent_stop", subagent_stop)

    # ------------------------------------------------------------------
    # post_llm_call
    # Fired once per turn after the tool loop completes.
    # No token data here — we use it only to refresh session end time.
    # kwargs: session_id, model, platform
    # ------------------------------------------------------------------
    def post_llm_call(session_id: str = "", **_kw) -> None:
        try:
            db.end_run(session_id=session_id, status="running")
        except Exception as exc:
            tele_log.error("post_llm_call hook failed: %s", exc)

    ctx.register_hook("post_llm_call", post_llm_call)

    # ------------------------------------------------------------------
    # on_session_end
    # Fired at the end of every run_conversation() call.
    # kwargs: session_id, completed, interrupted, model, platform
    # ------------------------------------------------------------------
    def on_session_end(
        session_id: str = "",
        completed: bool = False,
        interrupted: bool = False,
        **_kw,
    ) -> None:
        try:
            if interrupted:
                status = "interrupted"
            elif completed:
                status = "ok"
            else:
                status = "error"
            db.end_run(session_id=session_id, status=status)
            tele_log.debug("session_end session=%s status=%s", session_id, status)
        except Exception as exc:
            tele_log.error("on_session_end hook failed: %s", exc)

    ctx.register_hook("on_session_end", on_session_end)

    # ------------------------------------------------------------------
    # on_session_finalize
    # Fired when the session is truly torn down (CLI atexit, gateway expiry).
    # kwargs: session_id, platform
    # ------------------------------------------------------------------
    def on_session_finalize(session_id: str | None = None, **_kw) -> None:
        if not session_id:
            return
        try:
            db.end_run(session_id=session_id, status="ok")
            tele_log.debug("session_finalize session=%s", session_id)
        except Exception as exc:
            tele_log.error("on_session_finalize hook failed: %s", exc)

    ctx.register_hook("on_session_finalize", on_session_finalize)

    # ------------------------------------------------------------------
    # pre_llm_call  — budget SOFT alerting + sender capture
    # This hook CANNOT abort a call (Hermes uses its return only for context
    # injection — see budget.py / NOTES.md). We use it to (a) attach sender_id
    # to the run for per-sender budgets, and (b) inject a one-time-per-window
    # budget notice into the conversation. The hard tool-gate lives in
    # pre_tool_call below.
    # kwargs: session_id, sender_id, model, platform, user_message, is_first_turn
    # ------------------------------------------------------------------
    def pre_llm_call(session_id: str = "", sender_id: str = "", **_kw):
        try:
            if sender_id:
                db.set_sender(session_id, sender_id)
            run = db.get_run(session_id)
            if not run:
                return None
            verdicts = budget.evaluate_run(run)
            budget.enforce_cron_pause(verdicts)
            ctx_text = budget.alert_context(verdicts)
            if ctx_text:
                tele_log.info("budget alert injected for session=%s", session_id)
                return {"context": ctx_text}
        except Exception as exc:
            tele_log.error("pre_llm_call (budget) hook failed: %s", exc)
        return None

    ctx.register_hook("pre_llm_call", pre_llm_call)

    # ------------------------------------------------------------------
    # pre_tool_call  — budget HARD enforcement (the real gate)
    # Returning {"action":"block","message":...} aborts the tool call and
    # returns an error to the model instead (see plugins.py:1666). Blocking
    # every subsequent tool ends the agentic loop at the next boundary —
    # bounding spend without a true mid-call abort (which Hermes doesn't
    # expose). Cron jobs are additionally paused for future runs.
    # kwargs: tool_name, args, task_id, session_id, tool_call_id
    # ------------------------------------------------------------------
    def pre_tool_call(session_id: str = "", **_kw):
        try:
            run = db.get_run(session_id)
            if not run:
                return None
            verdicts = budget.evaluate_run(run)
            budget.enforce_cron_pause(verdicts)
            msg = budget.block_message_for(verdicts)
            if msg:
                tele_log.warning("budget hard-block for session=%s: %s", session_id, msg)
                return {"action": "block", "message": msg}
        except Exception as exc:
            tele_log.error("pre_tool_call (budget) hook failed: %s", exc)
        return None

    ctx.register_hook("pre_tool_call", pre_tool_call)

    # ------------------------------------------------------------------
    # /stats slash command
    # ------------------------------------------------------------------
    ctx.register_command(
        "stats",
        stats.handle,
        description="Show telemetry: tokens, cost, latency, tool usage per session/cron job",
        args_hint="[today|week|month|cron|providers|models|raw]",
    )

    # ------------------------------------------------------------------
    # /budget slash command
    # ------------------------------------------------------------------
    ctx.register_command(
        "budget",
        budget.handle,
        description="Show/set spend budgets (global, per cron job, per sender)",
        args_hint="[cron | set <scope> <window> <usd>]",
    )

    # ------------------------------------------------------------------
    # /setup slash command
    # ------------------------------------------------------------------
    ctx.register_command(
        "setup",
        setup.handle_command,
        description="First-time setup wizard for pricing and budgets",
        args_hint="[pricing|budget] [auto|minimal|skip|default|custom]",
    )

    # ------------------------------------------------------------------
    # Auto-setup: if pricing.yaml and/or budget.yaml are missing, run
    # non-interactive setup on first load (unless HERMES_TELEMETRY_NO_SETUP=1)
    # ------------------------------------------------------------------
    if os.environ.get("HERMES_TELEMETRY_NO_SETUP") != "1":
        try:
            auto_msg = setup.run(interactive=False)
            # Log so the user sees it in telemetry.log on first load
            for line in auto_msg.splitlines():
                tele_log.info(line)
        except Exception as exc:
            tele_log.warning("auto-setup skipped: %s", exc)

    tele_log.info("hermes-telemetry loaded — SQLite at %s", _db_path_info())


def _db_path_info() -> str:
    try:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        return str(hermes_home / "telemetry" / "telemetry.db")
    except Exception:
        return "~/.hermes/telemetry/telemetry.db"
