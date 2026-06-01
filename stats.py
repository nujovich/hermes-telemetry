"""Handler for the /stats slash command.

Subcommands:
  /stats              → summary for last 24h
  /stats today        → last 24h (alias)
  /stats week         → last 168h
  /stats month        → last 720h
  /stats cron         → cost/failure breakdown by cron_job_id
  /stats raw [N]      → last N runs (default 20)
  /stats providers    → per-provider real vs estimated breakdown (last 24h)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db


def _fmt_cost(v: Any) -> str:
    if v is None:
        return "$0.000000"
    return f"${float(v):.6f}"


def _fmt_ms(v: Any) -> str:
    if v is None:
        return "—"
    ms = float(v)
    if ms >= 60_000:
        return f"{ms / 60_000:.1f}m"
    if ms >= 1_000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms)}ms"


def _fmt_int(v: Any) -> str:
    if v is None:
        return "0"
    return f"{int(v):,}"


def _window_label(hours: int) -> str:
    if hours <= 24:
        return "last 24 h"
    if hours <= 168:
        return "last 7 days"
    return f"last {hours // 24} days"


def _summary_block(window_hours: int) -> str:
    s = db.stats_summary(window_hours)
    total = s.get("total_runs") or 0
    ok = s.get("ok_runs") or 0
    failed = s.get("failed_runs") or 0
    success_rate = f"{ok / total * 100:.1f}%" if total else "—"

    api_calls = int(s.get("api_calls") or 0)
    estimated_calls = int(s.get("estimated_llm_calls") or 0)
    has_estimated = estimated_calls > 0

    # Use ~ prefix on cost if any calls were estimated
    cost_label = "Cost (est.)   "
    cost_val = s.get("cost_usd")
    if has_estimated:
        cost_str = f"~{_fmt_cost(cost_val)}"
    else:
        cost_str = _fmt_cost(cost_val)

    lines = [
        f"hermes-telemetry — {_window_label(window_hours)}",
        "=" * 44,
        f"  Sessions      : {_fmt_int(total)}",
        f"  Success rate  : {success_rate}  (ok={_fmt_int(ok)}, failed={_fmt_int(failed)})",
        f"  API calls     : {_fmt_int(api_calls)}",
        f"  Tool calls    : {_fmt_int(s.get('tool_calls'))}",
        f"  Tokens in     : {_fmt_int(s.get('tokens_in'))}",
        f"  Tokens out    : {_fmt_int(s.get('tokens_out'))}",
        f"  {cost_label}: {cost_str}",
        f"  Avg latency   : {_fmt_ms(s.get('avg_latency_ms'))}",
        f"  Avg duration  : {_fmt_ms(s.get('avg_duration_ms'))}",
    ]

    if has_estimated:
        est_pct = f"{estimated_calls / api_calls * 100:.1f}%" if api_calls else "100%"
        lines.append(f"  Estimated data: {est_pct} of API calls")

    if not s.get("parent_links_available", True):
        lines.append(
            "  Note: Subagent tokens included in total "
            "(individual sessions, no parent-child attribution)"
        )

    top = s.get("top_tools") or []
    if top:
        lines.append("")
        lines.append("  Top tools:")
        lines.append(f"  {'Tool':<30} {'Calls':>6} {'Failures':>8} {'Avg ms':>8}")
        lines.append("  " + "-" * 56)
        for t in top:
            name = (t.get("tool_name") or "")[:30]
            calls = _fmt_int(t.get("calls"))
            fails = _fmt_int(t.get("failures"))
            avg_ms = _fmt_ms(t.get("avg_ms"))
            lines.append(f"  {name:<30} {calls:>6} {fails:>8} {avg_ms:>8}")

    return "\n".join(lines)


def _cron_block(window_hours: int = 168) -> str:
    rows = db.cost_by_job(window_hours)
    if not rows:
        return f"No cron runs in the last {window_hours // 24} days."

    lines = [
        f"hermes-telemetry — cron jobs ({_window_label(window_hours)})",
        "=" * 72,
        f"  {'Job ID':<20} {'Runs':>5} {'OK':>5} {'Fail':>5} {'Tok-in':>9} {'Tok-out':>9} {'Cost':>12} {'Avg dur':>9}",
        "  " + "-" * 70,
    ]
    for r in rows:
        job_id = (r.get("cron_job_id") or "?")[:20]
        runs = _fmt_int(r.get("runs"))
        ok = _fmt_int(r.get("ok_runs"))
        fail = _fmt_int(r.get("failed_runs"))
        tin = _fmt_int(r.get("tokens_in"))
        tout = _fmt_int(r.get("tokens_out"))
        cost = _fmt_cost(r.get("cost_usd"))
        dur = _fmt_ms(r.get("avg_duration_ms"))
        lines.append(f"  {job_id:<20} {runs:>5} {ok:>5} {fail:>5} {tin:>9} {tout:>9} {cost:>12} {dur:>9}")
    return "\n".join(lines)


def _providers_block(window_hours: int = 24) -> str:
    rows = db.stats_by_provider(window_hours)
    if not rows:
        return f"No API calls recorded in the {_window_label(window_hours)}."

    lines = [
        f"hermes-telemetry — providers ({_window_label(window_hours)})",
        "=" * 72,
        f"  {'Provider':<28} {'Calls':>6} {'Real':>6} {'Est':>5} {'Est%':>6} {'Cost':>12}",
        "  " + "-" * 67,
    ]
    for r in rows:
        prov    = (r.get("provider") or "(unknown)")[:28]
        total   = _fmt_int(r.get("total_calls"))
        real    = _fmt_int(r.get("real_calls"))
        est     = _fmt_int(r.get("estimated_calls"))
        est_pct = f"{r.get('estimated_pct', 0.0) * 100:.0f}%"
        cost    = _fmt_cost(r.get("cost_usd"))
        lines.append(
            f"  {prov:<28} {total:>6} {real:>6} {est:>5} {est_pct:>6} {cost:>12}"
        )

    lines.append("")
    lines.append("  Provider key:")
    lines.append("    openrouter  = model requested with 'openrouter/' prefix (routed through OpenRouter)")
    lines.append("    nous        = model requested without prefix (direct to Nous Research)")
    lines.append("    anthropic   = model requested with 'anthropic/' prefix (direct to Anthropic)")
    lines.append("")
    lines.append(
        "  Est% = share of calls where the provider returned no usage data "
        "(tokens estimated locally)."
    )
    lines.append(
        "  If Est% > 0 for your main provider, budget hard-verdicts may be "
        "degraded to soft under on_estimated.mode: warn_only."
    )

    # Check for estimated-price models
    try:
        import yaml
        pricing_file = Path.home() / ".hermes" / "telemetry" / "pricing.yaml"
        if pricing_file.exists():
            cfg = yaml.safe_load(pricing_file.read_text()) or {}
            est_models = cfg.get("_meta", {}).get("estimated_price_models", [])
            if est_models:
                lines.append("")
                lines.append(
                    f"  \u26a0\ufe0f  {len(est_models)} model(s) have estimated pricing "
                    "(no fixed price from provider)."
                )
                lines.append(
                    "  Budget hard-verdicts are degraded to soft under "
                    "on_estimated.mode: warn_only when these models are used."
                )
                # Show first few examples
                for m in est_models[:5]:
                    lines.append(f"    - {m}")
                if len(est_models) > 5:
                    lines.append(f"    ... and {len(est_models) - 5} more")
    except Exception:
        pass

    return "\n".join(lines)


def _raw_block(limit: int = 20) -> str:
    rows = db.recent_runs(limit)
    if not rows:
        return "No runs recorded yet."

    lines = [
        f"hermes-telemetry — last {limit} runs",
        "=" * 80,
    ]
    for r in rows:
        sid = (r.get("session_id") or "")[:32]
        plat = (r.get("platform") or "")[:8]
        model = (r.get("model") or "")[:24]
        status = r.get("status") or "?"
        cost = _fmt_cost(r.get("cost_usd"))
        dur = _fmt_ms(r.get("duration_ms"))
        started = (r.get("started_at") or "")[:16]
        cron = r.get("cron_job_id") or ""
        tag = f" [{cron}]" if cron else ""
        lines.append(
            f"  {started}  {plat:<8}  {model:<24}  {status:<10}  {cost}  {dur}{tag}"
        )
    return "\n".join(lines)


def handle(raw_args: str) -> str:
    """Entry point for /stats command handler."""
    args = (raw_args or "").strip().lower()

    if not args or args in ("today",):
        return _summary_block(24)
    if args == "week":
        return _summary_block(168)
    if args == "month":
        return _summary_block(720)
    if args in ("cron", "cron week"):
        return _cron_block(168)
    if args == "cron month":
        return _cron_block(720)
    if args == "cron today":
        return _cron_block(24)
    if args.startswith("raw"):
        parts = args.split()
        try:
            limit = int(parts[1]) if len(parts) > 1 else 20
        except ValueError:
            limit = 20
        return _raw_block(min(limit, 200))

    if args.startswith("providers"):
        sub = args[len("providers"):].strip()
        if sub in ("", "today"):
            return _providers_block(24)
        if sub == "week":
            return _providers_block(168)
        if sub == "month":
            return _providers_block(720)

    return (
        "Usage: /stats [today|week|month|cron|cron week|cron month|providers|raw [N]]\n"
        "  /stats               — last 24h summary\n"
        "  /stats week          — last 7 days summary\n"
        "  /stats month         — last 30 days summary\n"
        "  /stats cron          — breakdown by cron job (last 7 days)\n"
        "  /stats providers     — per-provider real vs estimated breakdown (24h)\n"
        "  /stats providers week — provider breakdown, last 7 days\n"
        "  /stats raw [N]       — last N raw run records (default 20)"
    )
