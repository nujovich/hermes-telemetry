"""Budget guardrails for hermes-telemetry.

Pure aggregation over the SQLite telemetry that db.py already records — zero
network calls. Decides whether a scope (global / per-cron-job / per-sender) is
within budget, in a soft-warning band, or over the hard limit, for daily and
monthly windows computed in the user's LOCAL timezone.

Enforcement reality (verified against Hermes Agent source, see NOTES.md):
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

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DEFAULT_THRESHOLDS = {"soft_pct": 0.80, "hard_pct": 1.00}
_DEFAULT_ON_ESTIMATED = {"mode": "warn_only"}  # warn_only | enforce

_WINDOWS = (("daily", "daily_usd"), ("monthly", "monthly_usd"))

_config_cache: Optional[dict] = None
_config_lock = threading.Lock()

# Short-TTL verdict cache so the pre_tool_call gate (which fires on EVERY tool
# call) does not re-query SQLite within a single assistant turn. Spend only
# changes when a new llm call is recorded, so a few seconds is safe.
_VERDICT_TTL_S = 5.0
_verdict_cache: dict[tuple, tuple] = {}
_verdict_lock = threading.Lock()


def _budget_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "telemetry" / "budget.yaml"


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
    scope: str            # global | cron_job | sender
    scope_id: str         # "" for global
    window: str           # daily | monthly
    status: str           # ok | soft | hard
    spent: float
    limit: float
    pct: float
    based_on_estimates: bool
    degraded: bool        # True if a hard verdict was softened by on_estimated
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

    out: dict[str, float] = {}
    for _, key in _WINDOWS:
        val = node.get(key) if isinstance(node, dict) else None
        if val is not None:
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                pass
    return out


def check(scope: str, scope_id: str = "") -> Optional[BudgetVerdict]:
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

    best: Optional[BudgetVerdict] = None
    for window, key in _WINDOWS:
        limit = limits.get(key)
        if not limit or limit <= 0:
            continue
        s = db.spend_by_scope(scope, scope_id, _window_start_utc(window))
        spent = s["spent_usd"]
        pct = spent / limit
        based_on_est = s["estimated_pct"] > 0.0

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
            scope=scope, scope_id=scope_id, window=window, status=status,
            spent=spent, limit=limit, pct=pct,
            based_on_estimates=based_on_est, degraded=degraded,
            period_key=_period_key(window),
        )
        if best is None or v.severity > best.severity or (
            v.severity == best.severity and v.pct > best.pct
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
    return verdicts


# ---------------------------------------------------------------------------
# Enforcement helpers
# ---------------------------------------------------------------------------

def block_message_for(verdicts: list[BudgetVerdict]) -> Optional[str]:
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


def alert_context(verdicts: list[BudgetVerdict]) -> Optional[str]:
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
            reason = (
                f"hermes-telemetry budget: spent ${v.spent:.4f} of "
                f"${v.limit:.2f} ({v.window})"
            )
            _pause_cron_job(v.scope_id, reason)


# ---------------------------------------------------------------------------
# /budget slash command
# ---------------------------------------------------------------------------

def _fmt_verdict_line(label: str, v: Optional[BudgetVerdict]) -> str:
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
    lines.append("  Note: per-cron-job spend EXCLUDES subagent (delegate_task) cost —")
    lines.append("  child runs are tracked separately and are not attributable to a")
    lines.append("  parent job (Hermes hooks expose no parent→child link). Use the")
    lines.append("  global budget for a tope that captures delegated spend.")
    return "\n".join(lines)


def _set_budget(scope: str, window: str, usd: float) -> str:
    """Persist a limit to budget.yaml and hot-reload."""
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

    with open(path, "w") as f:
        yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False)
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
