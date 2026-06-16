"""Handler for the /stats slash command.

Subcommands:
  /stats              → summary for last 24h
  /stats today        → last 24h (alias)
  /stats week         → last 168h
  /stats month        → last 720h
  /stats cron         → cost/failure breakdown by cron_job_id
  /stats raw [N]      → last N runs (default 20)
  /stats providers    → per-provider real vs estimated breakdown (last 24h)
  /stats models       → per-model breakdown within each provider (last 24h)

Date range support:
  --from YYYY-MM-DD   → start date (inclusive)
  --to YYYY-MM-DD     → end date (exclusive, defaults to now)
"""

from __future__ import annotations

from datetime import datetime, timezone
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


def _date_range_label(date_from: str | None, date_to: str | None) -> str:
    if date_from and date_to:
        return f"{date_from[:10]} to {date_to[:10]}"
    if date_from:
        return f"since {date_from[:10]}"
    if date_to:
        return f"until {date_to[:10]}"
    return "all time"


def _date_range_to_hours(date_from: str | None, date_to: str | None) -> int | None:
    """Convert date range to approximate hours for backward compat label."""
    if not date_from:
        return None
    try:
        from_dt = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        to_dt = (
            datetime.fromisoformat(date_to.replace("Z", "+00:00"))
            if date_to
            else datetime.now(timezone.utc)
        )
        hours = int((to_dt - from_dt).total_seconds() / 3600)
        return hours
    except Exception:
        return None


def _summary_block(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> str:
    s = db.stats_summary(window_hours=window_hours, date_from=date_from, date_to=date_to)
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
    cost_str = f"~{_fmt_cost(cost_val)}" if has_estimated else _fmt_cost(cost_val)

    label = _window_label(window_hours) if window_hours else _date_range_label(date_from, date_to)

    lines = [
        f"hermes-telemetry — {label}",
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


def _cron_block(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> str:
    rows = db.cost_by_job(window_hours=window_hours, date_from=date_from, date_to=date_to)
    label = _window_label(window_hours) if window_hours else _date_range_label(date_from, date_to)
    if not rows:
        return f"No cron runs in {label}."

    lines = [
        f"hermes-telemetry — cron jobs ({label})",
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
        lines.append(
            f"  {job_id:<20} {runs:>5} {ok:>5} {fail:>5} {tin:>9} {tout:>9} {cost:>12} {dur:>9}"
        )
    return "\n".join(lines)


def _providers_block(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> str:
    rows = db.stats_by_provider(window_hours=window_hours, date_from=date_from, date_to=date_to)
    label = _window_label(window_hours) if window_hours else _date_range_label(date_from, date_to)
    if not rows:
        return f"No API calls recorded in {label}."

    lines = [
        f"hermes-telemetry — providers ({label})",
        "=" * 72,
        f"  {'Provider':<28} {'Calls':>6} {'Real':>6} {'Est':>5} {'Est%':>6} {'Cost':>12}",
        "  " + "-" * 67,
    ]
    for r in rows:
        prov = (r.get("provider") or "(unknown)")[:28]
        total = _fmt_int(r.get("total_calls"))
        real = _fmt_int(r.get("real_calls"))
        est = _fmt_int(r.get("estimated_calls"))
        est_pct = f"{r.get('estimated_pct', 0.0) * 100:.0f}%"
        cost = _fmt_cost(r.get("cost_usd"))
        lines.append(f"  {prov:<28} {total:>6} {real:>6} {est:>5} {est_pct:>6} {cost:>12}")

    lines.append("")
    lines.append("  Provider key:")
    lines.append("    The provider label is whatever the Hermes gateway reports for each API")
    lines.append("    call (post_api_request hook); it is stored verbatim and rows are grouped")
    lines.append("    by it. It is NOT derived from the model name — so everything the gateway")
    lines.append("    routes through OpenRouter shows up under its 'openrouter' label regardless")
    lines.append("    of the model's own 'google/', 'openai/', 'anthropic/', … prefix.")
    lines.append("    '(unknown)' = the gateway reported no provider for that call.")
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


def _models_block(
    window_hours: int | None = None, *, date_from: str | None = None, date_to: str | None = None
) -> str:
    rows = db.stats_by_model(window_hours=window_hours, date_from=date_from, date_to=date_to)
    label = _window_label(window_hours) if window_hours else _date_range_label(date_from, date_to)
    if not rows:
        return f"No API calls recorded in {label}."

    # Subscription models (`_subscription: true` in pricing.yaml) are an
    # explicitly declared $0 — surface them so the $0.00 footer can stop
    # claiming every zero-cost row is a missing-pricing problem.
    from . import pricing as _pricing

    subscription_models = _pricing._load_custom_pricing().get("subscription_models", set())

    lines = [
        f"hermes-telemetry — models ({label})",
        "=" * 108,
        f"  {'Provider':<20} {'Model':<46} {'Calls':>6} {'Real':>6} {'Est':>5} {'Cost':>12}  Notes",
        "  " + "-" * 106,
    ]
    sub_zero_count = 0
    noentry_zero_count = 0
    for r in rows:
        prov = (r.get("provider") or "(unknown)")[:20]
        # Model names (esp. OpenRouter's dated keys) are kept wide so the date
        # suffix stays visible — that's the whole point of this view.
        model = (r.get("model") or "(unknown)")[:46]
        total = _fmt_int(r.get("total_calls"))
        real = _fmt_int(r.get("real_calls"))
        est = _fmt_int(r.get("estimated_calls"))
        cost_v = float(r.get("cost_usd") or 0.0)
        cost = _fmt_cost(cost_v)
        note = ""
        if cost_v == 0.0:
            if (r.get("model") or "").lower() in subscription_models:
                note = "subscription/free-tier"
                sub_zero_count += 1
            else:
                note = "no price entry"
                noentry_zero_count += 1
        lines.append(f"  {prov:<20} {model:<46} {total:>6} {real:>6} {est:>5} {cost:>12}  {note}")

    lines.append("")
    lines.append("  Rows are grouped by provider, then by calls (desc).")
    if sub_zero_count:
        lines.append(
            f"  {sub_zero_count} model(s) at $0.00 are subscription/free tier "
            "(declared in pricing.yaml via `_subscription: true`)."
        )
    if noentry_zero_count:
        lines.append(
            f"  {noentry_zero_count} model(s) at $0.00 have no price entry in "
            "pricing.yaml — run /setup pricing auto"
        )
        lines.append("  to refresh, or add them manually.")
    return "\n".join(lines)


def _raw_block(limit: int = 20, *, date_from: str | None = None, date_to: str | None = None) -> str:
    rows = db.recent_runs(limit, date_from=date_from, date_to=date_to)
    if not rows:
        return "No runs recorded yet."

    lines = [
        f"hermes-telemetry — last {limit} runs",
        "=" * 80,
    ]
    for r in rows:
        (r.get("session_id") or "")[:32]
        plat = (r.get("platform") or "")[:8]
        model = (r.get("model") or "")[:24]
        status = r.get("status") or "?"
        cost = _fmt_cost(r.get("cost_usd"))
        dur = _fmt_ms(r.get("duration_ms"))
        started = (r.get("started_at") or "")[:16]
        cron = r.get("cron_job_id") or ""
        tag = f" [{cron}]" if cron else ""
        lines.append(f"  {started}  {plat:<8}  {model:<24}  {status:<10}  {cost}  {dur}{tag}")
    return "\n".join(lines)


_ISO_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%fZ",
)


def _parse_iso_date(raw: str) -> str | None:
    """Parse a date/timestamp into an ISO-8601 string with a UTC offset.

    Returns None when the input isn't a recognized format — slash command args
    are user-typed, so we surface a parse failure as "ignore this flag" rather
    than raising, and the caller renders an error message inline.
    """
    s = raw.strip()
    if not s:
        return None
    # ``fromisoformat`` handles offsets like ``+00:00`` natively and accepts
    # ``Z`` on Python 3.11+; it covers the timestamps produced by ``_utcnow``.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        pass
    # Legacy strptime formats for inputs that don't carry timezone info.
    for fmt in _ISO_DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _extract_date_flags(raw_args: str) -> tuple[str, str | None, str | None, str | None]:
    """Pull `--from <val>` and `--to <val>` out of a slash-command arg string.

    Returns ``(remaining, date_from, date_to, error)`` where:
      - ``remaining`` is the arg string with the flags removed (preset tokens
        survive untouched and keep their original casing — the caller still
        lowercases them).
      - ``date_from`` / ``date_to`` are ISO-normalized or ``None``.
      - ``error`` is a human-readable message if a flag value failed to parse,
        otherwise ``None``. The caller short-circuits with that message.
    """
    if not raw_args:
        return ("", None, None, None)
    tokens = raw_args.split()
    out: list[str] = []
    date_from: str | None = None
    date_to: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--from", "--to"):
            if i + 1 >= len(tokens):
                return ("", None, None, f"Missing value for {tok}.")
            parsed = _parse_iso_date(tokens[i + 1])
            if parsed is None:
                return (
                    "",
                    None,
                    None,
                    f"Invalid date for {tok}: {tokens[i + 1]!r} "
                    "(use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ).",
                )
            if tok == "--from":
                date_from = parsed
            else:
                date_to = parsed
            i += 2
        else:
            out.append(tok)
            i += 1
    return (" ".join(out), date_from, date_to, None)


def handle(raw_args: str) -> str:
    """Entry point for /stats command handler (Slack slash command format).

    Supports ``--from <iso>`` / ``--to <iso>`` flags alongside the preset
    subcommands so the in-chat surface has parity with the standalone CLI.
    When a date flag is supplied, the matching block ignores the preset window
    and queries the (possibly bounded) date range instead.
    """
    remaining, date_from, date_to, err = _extract_date_flags(raw_args or "")
    if err:
        return err
    has_range = date_from is not None or date_to is not None

    args = remaining.strip().lower()

    if not args or args in ("today",):
        if has_range:
            return _summary_block(date_from=date_from, date_to=date_to)
        return _summary_block(24)
    if args == "week":
        return (
            _summary_block(168)
            if not has_range
            else _summary_block(date_from=date_from, date_to=date_to)
        )
    if args == "month":
        return (
            _summary_block(720)
            if not has_range
            else _summary_block(date_from=date_from, date_to=date_to)
        )
    if args in ("cron", "cron week"):
        return (
            _cron_block(168) if not has_range else _cron_block(date_from=date_from, date_to=date_to)
        )
    if args == "cron month":
        return (
            _cron_block(720) if not has_range else _cron_block(date_from=date_from, date_to=date_to)
        )
    if args == "cron today":
        return (
            _cron_block(24) if not has_range else _cron_block(date_from=date_from, date_to=date_to)
        )
    if args.startswith("raw"):
        parts = args.split()
        try:
            limit = int(parts[1]) if len(parts) > 1 else 20
        except ValueError:
            limit = 20
        if has_range:
            return _raw_block(min(limit, 200), date_from=date_from, date_to=date_to)
        return _raw_block(min(limit, 200))

    if args.startswith("providers"):
        sub = args[len("providers") :].strip()
        if has_range and sub in ("", "today", "week", "month"):
            return _providers_block(date_from=date_from, date_to=date_to)
        if sub in ("", "today"):
            return _providers_block(24)
        if sub == "week":
            return _providers_block(168)
        if sub == "month":
            return _providers_block(720)

    if args.startswith("models"):
        sub = args[len("models") :].strip()
        if has_range and sub in ("", "today", "week", "month"):
            return _models_block(date_from=date_from, date_to=date_to)
        if sub in ("", "today"):
            return _models_block(24)
        if sub == "week":
            return _models_block(168)
        if sub == "month":
            return _models_block(720)

    return (
        "Usage: /stats [today|week|month|cron|cron week|cron month|providers|models|raw [N]]\n"
        "             [--from YYYY-MM-DD[THH:MM:SS[Z]]] [--to YYYY-MM-DD[THH:MM:SS[Z]]]\n"
        "  /stats               — last 24h summary\n"
        "  /stats week          — last 7 days summary\n"
        "  /stats month         — last 30 days summary\n"
        "  /stats cron          — breakdown by cron job (last 7 days)\n"
        "  /stats providers     — per-provider real vs estimated breakdown (24h)\n"
        "  /stats providers week — provider breakdown, last 7 days\n"
        "  /stats models        — per-model breakdown within each provider (24h)\n"
        "  /stats models week   — per-model breakdown, last 7 days\n"
        "  /stats raw [N]       — last N raw run records (default 20)\n"
        "\n"
        "Date range examples (work the same way under the CLI):\n"
        "  /stats models --from 2026-06-16T12:00:00Z\n"
        "  /stats providers --from 2026-06-10 --to 2026-06-15"
    )
