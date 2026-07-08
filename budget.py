"""Budget guardrails for hermes-telemetry.

Pure aggregation over the SQLite telemetry that db.py already records — zero
network calls. Decides whether a scope (global / per-cron-job / per-sender) is
within budget, in a soft-warning band, or over the hard limit, for daily and
monthly windows computed in the user's LOCAL timezone.

Enforcement reality (verified against Hermes Agent source, see ONBOARDING.md):
  * pre_llm_call / pre_api_request CANNOT abort a model call — their return is
    used only for context injection (pre_llm_call) or ignored (pre_api_request).
  * pre_tool_call CAN block a tool by returning {"action":"block","message":..}.
    Blocking every subsequent tool ends the agentic loop at the next tool
    boundary — a "tool-gate", not a true mid-call abort. The in-flight model
    response still completes and is billed.
  * cron jobs can additionally be paused for FUTURE runs via cron.jobs.pause_job.

So the achievable enforcement is: soft alert (context injection, once per
window) + hard tool-gate (pre_tool_call block) + cron pause. Budgets resting on
estimated (usage=None) rows are deliberately laxer: a hard verdict degrades to
soft when on_estimated.mode == "warn_only".

Config: ~/.hermes/telemetry/budget.yaml  (absent → budgets disabled, all ok).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, paths

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File watcher for budget.yaml (hot-reload on config changes)
# ---------------------------------------------------------------------------
_budget_observer = None
_budget_watcher_started = False


def start_budget_watcher() -> None:
    """Start a background file watcher on budget.yaml to auto-reload config on changes."""
    global _budget_observer, _budget_watcher_started
    if _budget_watcher_started:
        return
    try:
        from watchdog.events import FileModifiedEvent, FileSystemEventHandler
        from watchdog.observers import Observer
    except Exception as exc:
        logger.debug("budget watcher unavailable (watchdog not installed): %s", exc)
        return

    class BudgetConfigHandler(FileSystemEventHandler):
        def _reload_if_budget(self, path: str) -> None:
            if os.path.basename(path) == "budget.yaml":
                logger.info("budget.yaml changed — hot-reloading budget config")
                reload_config()

        def on_modified(self, event: FileModifiedEvent) -> None:
            if not event.is_directory:
                self._reload_if_budget(event.src_path)

        def on_created(self, event) -> None:
            if not event.is_directory:
                self._reload_if_budget(event.src_path)

        def on_moved(self, event) -> None:
            self._reload_if_budget(getattr(event, "dest_path", ""))

    path = _budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _budget_observer = Observer()
    _budget_observer.schedule(BudgetConfigHandler(), str(path.parent), recursive=False)
    _budget_observer.daemon = True
    _budget_observer.start()
    _budget_watcher_started = True
    logger.info("Budget file watcher started on %s", path)


def stop_budget_watcher() -> None:
    """Stop the budget file watcher (for cleanup/tests)."""
    global _budget_observer, _budget_watcher_started
    if _budget_observer:
        _budget_observer.stop()
        _budget_observer.join(timeout=2)
        _budget_observer = None
    _budget_watcher_started = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DEFAULT_THRESHOLDS = {"soft_pct": 0.80, "hard_pct": 1.00}
_DEFAULT_ON_ESTIMATED = {"mode": "warn_only"}  # warn_only | enforce

_WINDOWS = (("daily", "daily_usd"), ("monthly", "monthly_usd"))

_config_cache: dict | None = None
_config_lock = threading.Lock()

# Short-TTL verdict cache so the pre_tool_call gate (which fires on EVERY tool
# call) does not re-query SQLite within a single assistant turn. Spend only
# changes when a new llm call is recorded, so a few seconds is safe.
_VERDICT_TTL_S = 5.0
_verdict_cache: dict[tuple, tuple] = {}
_verdict_lock = threading.Lock()


def _budget_path() -> Path:
    return paths.get_budget_path()


def _pricing_path() -> Path:
    return paths.get_pricing_path()


def load_config() -> dict:
    """Load and cache budget.yaml. Returns a normalized config dict. A missing
    or malformed file yields an empty (disabled) budget — never raises."""
    global _config_cache
    with _config_lock:
        if _config_cache is not None:
            return _config_cache
        cfg = {
            "budgets": {},
            "thresholds": dict(_DEFAULT_THRESHOLDS),
            "on_estimated": dict(_DEFAULT_ON_ESTIMATED),
        }
        path = _budget_path()
        if path.exists():
            try:
                import yaml

                with open(path) as f:
                    raw = yaml.safe_load(f) or {}
                cfg["budgets"] = raw.get("budgets") or {}
                if isinstance(raw.get("thresholds"), dict):
                    cfg["thresholds"].update(raw["thresholds"])
                if isinstance(raw.get("on_estimated"), dict):
                    cfg["on_estimated"].update(raw["on_estimated"])
            except Exception as exc:
                logger.warning("Failed to load budget config %s: %s", path, exc)
        _config_cache = cfg
        return cfg


def reload_config() -> None:
    """Drop the cached config (after an edit) so the next read re-parses."""
    global _config_cache
    with _config_lock:
        _config_cache = None
    with _verdict_lock:
        _verdict_cache.clear()


# ---------------------------------------------------------------------------
# Window math (local timezone)
# ---------------------------------------------------------------------------


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _window_start_utc(window: str) -> str:
    """ISO-8601 UTC start of the current local daily/monthly window. Returned as
    a UTC string so it compares lexicographically against runs.started_at
    (also stored as UTC ISO with a +00:00 offset)."""
    now = _now_local()
    if window == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # daily
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def _period_key(window: str) -> str:
    now = _now_local()
    return now.strftime("%Y-%m") if window == "monthly" else now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class BudgetVerdict:
    scope: str  # global | cron_job | sender | profile
    scope_id: str  # "" for global
    window: str  # daily | monthly
    status: str  # ok | soft | hard
    spent: float
    limit: float
    pct: float
    based_on_estimates: bool
    degraded: bool  # True if a hard verdict was softened by on_estimated
    period_key: str

    _SEVERITY = {"ok": 0, "soft": 1, "hard": 2}

    @property
    def severity(self) -> int:
        return self._SEVERITY[self.status]

    def scope_label(self) -> str:
        if self.scope == "global":
            return "Global"
        if self.scope == "cron_job":
            return f"Cron job '{self.scope_id}'"
        if self.scope == "sender":
            return f"Sender '{self.scope_id}'"
        if self.scope == "profile":
            return f"Profile '{self.scope_id}'"
        return self.scope


def _resolve_limits(scope: str, scope_id: str) -> dict[str, float]:
    """Return {'daily_usd': x, 'monthly_usd': y} for a scope, honoring per-id
    overrides. Missing windows are omitted (no limit = not enforced)."""
    budgets = load_config().get("budgets", {})
    node: Any = {}
    if scope == "global":
        node = budgets.get("global") or {}
    elif scope == "cron_job":
        per = budgets.get("per_cron_job") or {}
        overrides = per.get("overrides") or {}
        node = overrides.get(scope_id) or per.get("default") or {}
    elif scope == "sender":
        per = budgets.get("per_sender") or {}
        overrides = per.get("overrides") or {}
        node = overrides.get(scope_id) or per.get("default") or {}
    elif scope == "profile":
        per = budgets.get("per_profile") or {}
        overrides = per.get("overrides") or {}
        node = overrides.get(scope_id) or per.get("default") or {}

    out: dict[str, float] = {}
    for _, key in _WINDOWS:
        val = node.get(key) if isinstance(node, dict) else None
        if val is not None:
            with contextlib.suppress(TypeError, ValueError):
                out[key] = float(val)
    return out


def check(scope: str, scope_id: str = "") -> BudgetVerdict | None:
    """Evaluate every configured window for a scope and return the single most
    severe verdict, or None if the scope has no configured limit."""
    cache_key = (scope, scope_id)
    now = datetime.now().timestamp()
    with _verdict_lock:
        cached = _verdict_cache.get(cache_key)
        if cached and (now - cached[0]) < _VERDICT_TTL_S:
            return cached[1]

    limits = _resolve_limits(scope, scope_id)
    thresholds = load_config().get("thresholds", _DEFAULT_THRESHOLDS)
    soft_pct = float(thresholds.get("soft_pct", 0.80))
    hard_pct = float(thresholds.get("hard_pct", 1.00))
    mode = load_config().get("on_estimated", _DEFAULT_ON_ESTIMATED).get("mode", "warn_only")

    best: BudgetVerdict | None = None
    for window, key in _WINDOWS:
        limit = limits.get(key)
        if not limit or limit <= 0:
            continue
        s = db.spend_by_scope(scope, scope_id, _window_start_utc(window))
        spent = s["spent_usd"]
        pct = spent / limit
        based_on_est = s["estimated_pct"] > 0.0

        # Also check if any calls used models with estimated pricing
        # (e.g. OpenRouter auto-routing models with no fixed price)
        if not based_on_est:
            based_on_est = (
                db.estimated_price_share(scope, scope_id, _window_start_utc(window)) > 0.0
            )

        if pct >= hard_pct:
            status = "hard"
        elif pct >= soft_pct:
            status = "soft"
        else:
            status = "ok"

        degraded = False
        if status == "hard" and based_on_est and mode == "warn_only":
            status = "soft"
            degraded = True

        v = BudgetVerdict(
            scope=scope,
            scope_id=scope_id,
            window=window,
            status=status,
            spent=spent,
            limit=limit,
            pct=pct,
            based_on_estimates=based_on_est,
            degraded=degraded,
            period_key=_period_key(window),
        )
        if (
            best is None
            or v.severity > best.severity
            or (v.severity == best.severity and v.pct > best.pct)
        ):
            best = v

    with _verdict_lock:
        _verdict_cache[cache_key] = (now, best)
    return best


def evaluate_run(run_row: dict) -> list[BudgetVerdict]:
    """Return verdicts for every budget scope applicable to a run: always
    global, plus cron_job / sender when the run carries those ids."""
    verdicts: list[BudgetVerdict] = []
    g = check("global", "")
    if g:
        verdicts.append(g)
    cron_job_id = run_row.get("cron_job_id")
    if cron_job_id:
        v = check("cron_job", cron_job_id)
        if v:
            verdicts.append(v)
    sender_id = run_row.get("sender_id")
    if sender_id:
        v = check("sender", sender_id)
        if v:
            verdicts.append(v)
    profile = run_row.get("profile")
    if profile:
        v = check("profile", profile)
        if v:
            verdicts.append(v)
    return verdicts


# ---------------------------------------------------------------------------
# Enforcement helpers
# ---------------------------------------------------------------------------


def block_message_for(verdicts: list[BudgetVerdict]) -> str | None:
    """If any verdict is a (non-degraded) hard breach, return an actionable
    block message for the pre_tool_call gate. Degraded-by-estimate verdicts are
    already 'soft' and never reach here."""
    hard = [v for v in verdicts if v.status == "hard"]
    if not hard:
        return None
    v = max(hard, key=lambda x: x.pct)
    return (
        f"[BUDGET] {v.scope_label()} is over its {v.window} budget: "
        f"spent ${v.spent:.4f} of ${v.limit:.2f} ({v.pct * 100:.0f}%). "
        f"Tool calls are blocked until the {v.window} window resets. "
        f"Raise the limit with `/budget set {v.scope} {v.window} <usd>` if intended."
    )


def alert_context(verdicts: list[BudgetVerdict]) -> str | None:
    """Build a one-time-per-window notice (soft or hard) for context injection
    via pre_llm_call. Anti-spam is enforced through db.try_budget_alert."""
    parts: list[str] = []
    for v in verdicts:
        if v.status == "ok":
            continue
        if not db.try_budget_alert(
            v.scope, v.scope_id, v.window, v.period_key, v.status, v.spent, v.limit
        ):
            continue  # already alerted this window
        est = " (based on estimated usage)" if v.based_on_estimates else ""
        if v.status == "hard":
            parts.append(
                f"[BUDGET] {v.scope_label()} hit its {v.window} limit: "
                f"${v.spent:.4f} / ${v.limit:.2f}{est}. Further tool use is blocked."
            )
        else:
            band = "estimate-softened" if v.degraded else f"{v.pct * 100:.0f}%"
            parts.append(
                f"[BUDGET] {v.scope_label()} at {band} of its {v.window} budget "
                f"(${v.spent:.4f} / ${v.limit:.2f}){est}."
            )
    return "\n".join(parts) if parts else None


def _pause_cron_job(job_id: str, reason: str) -> bool:
    """Pause a cron job for future runs. Isolated so tests can monkeypatch it."""
    try:
        from cron.jobs import pause_job  # type: ignore

        pause_job(job_id, reason=reason)
        return True
    except Exception as exc:  # cron module unavailable or call failed
        logger.warning("budget: could not pause cron job %r: %s", job_id, exc)
        return False


def enforce_cron_pause(verdicts: list[BudgetVerdict]) -> None:
    """For each hard cron_job breach, pause the job once per window (future runs
    only — the in-progress run is stopped by the pre_tool_call gate)."""
    for v in verdicts:
        if v.scope != "cron_job" or v.status != "hard":
            continue
        if db.try_budget_alert(
            v.scope, v.scope_id, v.window, v.period_key, "paused", v.spent, v.limit
        ):
            reason = f"hermes-telemetry budget: spent ${v.spent:.4f} of ${v.limit:.2f} ({v.window})"
            _pause_cron_job(v.scope_id, reason)


def burn_rate_projection(
    scope: str,
    scope_id: str = "",
    *,
    window: str = "daily",
    lookback_days: int = 14,
    now=None,
) -> dict:
    """Project whether a scope is on track to breach its configured limit.

    Learns a recent daily spend rate from the recorded telemetry (moving-window
    average over the last ``lookback_days``), then projects the spend for the
    remainder of the current ``window`` ("daily" or "monthly") at that rate.
    Returns a serializable dict describing the projection.

    A scope with no configured limit for the requested window returns
    ``{"enabled": False}``. This is a forecast only — it makes no network calls
    and does not mutate budget state.
    """
    if now is None:
        now = _now_local()

    limits = _resolve_limits(scope, scope_id)
    limit_key = "monthly_usd" if window == "monthly" else "daily_usd"
    limit = limits.get(limit_key)
    if not limit or limit <= 0:
        return {"enabled": False, "scope": scope, "scope_id": scope_id, "window": window}

    series = db.daily_spend_series(scope, scope_id, lookback_days, now=now)
    recent = [d["cost_usd"] for d in series]
    avg_daily = sum(recent) / len(recent) if recent else 0.0

    if window == "monthly":
        window_start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        window_start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = window_start_dt.astimezone(timezone.utc).isoformat()

    spend_now = db.spend_by_scope(scope, scope_id, window_start)
    spent_so_far = float(spend_now["spent_usd"])

    if window == "monthly":
        days_in_window = _days_in_month(now)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        days_in_window = 1
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)

    elapsed_seconds = (now - start_dt).total_seconds()
    window_seconds = days_in_window * 86400.0

    remaining_seconds = max(0.0, window_seconds - elapsed_seconds)
    remaining_days = remaining_seconds / 86400.0

    projected_remaining = avg_daily * remaining_days
    projected_total = spent_so_far + projected_remaining

    pct = projected_total / limit if limit > 0 else 0.0
    status = "hard" if pct >= 1.00 else ("soft" if pct >= 0.80 else "ok")

    if window == "monthly":
        days_left_in_window = max(0, days_in_window - now.day)
    else:
        days_left_in_window = 1 if elapsed_seconds < window_seconds else 0
    if avg_daily > 0:
        usd_left = limit - spent_so_far
        est_days_to_breach = usd_left / avg_daily if usd_left > 0 else 0.0
    else:
        est_days_to_breach = None

    return {
        "enabled": True,
        "scope": scope,
        "scope_id": scope_id,
        "window": window,
        "limit_usd": float(limit),
        "spent_so_far_usd": round(spent_so_far, 6),
        "avg_daily_usd": round(avg_daily, 6),
        "remaining_days_in_window": days_left_in_window,
        "projected_remaining_usd": round(projected_remaining, 6),
        "projected_total_usd": round(projected_total, 6),
        "projected_pct": round(pct, 4),
        "status": status,
        "lookback_days": lookback_days,
        "est_days_to_breach": (
            round(est_days_to_breach, 2) if est_days_to_breach is not None else None
        ),
    }


def _days_in_month(now: datetime) -> int:
    """Number of days in the month that ``now`` falls in."""
    import calendar

    return calendar.monthrange(now.year, now.month)[1]


# ---------------------------------------------------------------------------
# /budget slash command
# ---------------------------------------------------------------------------


def _fmt_verdict_line(label: str, v: BudgetVerdict | None) -> str:
    if v is None:
        return f"  {label:<28} (no limit set)"
    flag = {"ok": " ", "soft": "!", "hard": "█"}[v.status]
    est = " ~est" if v.based_on_estimates else ""
    return (
        f"{flag} {label:<28} ${v.spent:>9.4f} / ${v.limit:>8.2f}  "
        f"{v.pct * 100:>5.0f}%  [{v.window}]{est}"
    )


def _status_block() -> str:
    lines = ["hermes-telemetry — budget status", "=" * 60]
    g = check("global", "")
    lines.append(_fmt_verdict_line("global", g))

    since30 = _days_ago_utc(30)
    cron_ids = db.list_cron_job_ids(since30)
    if cron_ids:
        lines.append("")
        lines.append("  Cron jobs:")
        for jid in cron_ids:
            lines.append("  " + _fmt_verdict_line(jid, check("cron_job", jid)))
    senders = db.list_sender_ids(since30)
    if senders:
        lines.append("")
        lines.append("  Senders:")
        for sid in senders:
            lines.append("  " + _fmt_verdict_line(sid, check("sender", sid)))

    if g is None and not cron_ids and not senders:
        lines.append("")
        lines.append("  No budgets configured. Add ~/.hermes/telemetry/budget.yaml")
        lines.append("  or run: /budget set global daily 5.00")
    lines.append("")
    lines.append("  Legend:  (blank)=ok  !=soft (≥80%)  █=hard (≥100%)  ~est=estimated data")

    # Estimated-price models warning
    try:
        import yaml

        pricing_file = _pricing_path()
        if pricing_file.exists():
            cfg = yaml.safe_load(pricing_file.read_text()) or {}
            est_models = cfg.get("_meta", {}).get("estimated_price_models", [])
            if est_models:
                lines.append("")
                lines.append(
                    f"  ⚠️  {len(est_models)} model(s) with estimated pricing "
                    "(no fixed price → cost shown as $0.00)."
                )
                lines.append(
                    "  If >0% of spend uses these models, budget hard-verdicts "
                    "are degraded to soft (on_estimated.mode: warn_only)."
                )
    except Exception:
        pass

    return "\n".join(lines)


def _forecast_block(scope: str = "global", scope_id: str = "", window: str = "daily") -> str:
    proj = burn_rate_projection(scope, scope_id, window=window)
    if not proj.get("enabled"):
        return (
            "hermes-telemetry — burn-rate forecast\n"
            + "=" * 60
            + "\n"
            + f"  No {window} budget configured for scope "
            + (scope_id or scope)
            + ".\n"
            + "  Set one with: /budget set "
            + scope
            + " "
            + window
            + " <usd>"
        )
    flag = {"ok": " ", "soft": "!", "hard": "X"}[proj["status"]]
    lines = ["hermes-telemetry — burn-rate forecast", "=" * 60]
    lines.append(f"  Scope:    {scope_id or scope} ({proj['window']})")
    lines.append(f"  Limit:    ${proj['limit_usd']:.2f}")
    lines.append(f"  Spent:    ${proj['spent_so_far_usd']:.4f}")
    lines.append(f"  Avg/day:  ${proj['avg_daily_usd']:.4f} (last {proj['lookback_days']}d)")
    lines.append(
        f"  Projected ${proj['projected_total_usd']:.4f} "
        f"({proj['projected_pct'] * 100:.0f}%) by window end"
    )
    if proj["est_days_to_breach"] is not None:
        lines.append(f"  At this rate: breach in ~{proj['est_days_to_breach']:.1f} days")
    lines.append(f"  {flag} Projected status: {proj['status'].upper()}")
    return "\n".join(lines)


def _cron_block() -> str:
    since30 = _days_ago_utc(30)
    cron_ids = db.list_cron_job_ids(since30)
    if not cron_ids:
        return "No cron runs recorded in the last 30 days."
    lines = ["hermes-telemetry — cron budgets", "=" * 60]
    for jid in cron_ids:
        lines.append(_fmt_verdict_line(jid, check("cron_job", jid)))
    lines.append("")
    lines.append("  Note: per-cron-job spend now INCLUDES linked subagent (delegate_task)")
    lines.append("  cost — child runs are attributed to their root cron job via the")
    lines.append("  subagent_edges tree (async + nested). Spend from unlinked children")
    lines.append("  (edge not recorded) is not attributable; the global budget remains")
    lines.append("  the catch-all cap.")
    return "\n".join(lines)


def _set_budget(scope: str, window: str, usd: float) -> str:
    """Persist a limit to budget.yaml and hot-reload."""
    import os

    import yaml

    if scope not in ("global", "cron_job", "sender"):
        return f"Unknown scope {scope!r}. Use: global | cron_job | sender"
    if window not in ("daily", "monthly"):
        return f"Unknown window {window!r}. Use: daily | monthly"
    key = f"{window}_usd"

    path = _budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if path.exists():
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
        except Exception:
            raw = {}
    budgets = raw.setdefault("budgets", {})
    if scope == "global":
        budgets.setdefault("global", {})[key] = usd
    elif scope == "cron_job":
        budgets.setdefault("per_cron_job", {}).setdefault("default", {})[key] = usd
    else:  # sender
        budgets.setdefault("per_sender", {}).setdefault("default", {})[key] = usd

    # Atomic write: temp file + os.replace (POSIX atomic)
    # Prevents watchdog from reading partial/empty YAML on IN_MODIFY
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, path)
    reload_config()
    return f"Set {scope} {window} budget to ${usd:.2f}. Saved to {path}."


def _days_ago_utc(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def handle(raw_args: str) -> str:
    """Entry point for the /budget command."""
    args = (raw_args or "").strip()
    if not args:
        return _status_block()
    parts = args.split()
    sub = parts[0].lower()

    if sub == "cron":
        return _cron_block()

    if sub == "forecast":
        window = "monthly"
        if len(parts) >= 2 and parts[1].lower() in ("daily", "monthly"):
            window = parts[1].lower()
        scope = "global"
        scope_id = ""
        if len(parts) >= 3:
            scope = parts[2].lower()
            if scope not in ("global", "cron_job", "sender"):
                return f"Unknown scope {scope!r}. Use: global | cron_job | sender"
            if len(parts) >= 4:
                scope_id = parts[3]
        return _forecast_block(scope, scope_id, window)

    if sub == "set":
        if len(parts) != 4:
            return "Usage: /budget set <global|cron_job|sender> <daily|monthly> <usd>"
        _, scope, window, usd_s = parts
        try:
            usd = float(usd_s)
        except ValueError:
            return f"Invalid amount {usd_s!r} — must be a number, e.g. 5.00"
        if usd < 0:
            return "Amount must be non-negative."
        return _set_budget(scope.lower(), window.lower(), usd)

    return (
        "Usage: /budget [cron | set <scope> <window> <usd>]\n"
        "  /budget                       — status of all scopes\n"
        "  /budget cron                  — per-cron-job budgets\n"
        "  /budget set global daily 5.00 — set/raise a limit (hot-reloads)"
    )
