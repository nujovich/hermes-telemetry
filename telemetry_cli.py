from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from . import db, stats

_STATS_WINDOW_HOURS: dict[str, int] = {
    "today": 24,
    "week": 168,
    "month": 720,
    "last-7-days": 168,
    "last-30-days": 720,
    "cron": 168,
    "cron-week": 168,
    "cron-month": 720,
    "providers": 24,
    "providers-week": 168,
    "providers-month": 720,
    "models": 24,
    "models-week": 168,
    "models-month": 720,
}

_STATS_CHOICES = list(_STATS_WINDOW_HOURS)


def _parse_date(date_str: str) -> str:
    """Parse a date string and return ISO format (YYYY-MM-DD or ISO 8601)."""
    # Support formats: YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, YYYY-MM-DDTHH:MM:SSZ, now
    date_str = date_str.strip().lower()
    if date_str == "now":
        return datetime.now(timezone.utc).isoformat()

    # Try parsing as date only (YYYY-MM-DD)
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue

    raise argparse.ArgumentTypeError(
        f"Invalid date format: {date_str}. Use YYYY-MM-DD or ISO 8601."
    )


def _preset_to_date_range(preset: str) -> tuple[str | None, str | None]:
    """Convert preset strings like 'today', 'week', 'month', 'last-7-days' to (from, to)."""
    preset = preset.strip().lower()
    now = datetime.now(timezone.utc)

    if preset == "today":
        from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (from_dt.isoformat(), None)
    if preset == "week":
        from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        return (from_dt.isoformat(), None)
    if preset == "month":
        from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=30)
        return (from_dt.isoformat(), None)
    if preset.startswith("last-") and preset.endswith("-days"):
        try:
            days = int(preset[5:-5])
            from_dt = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
            return (from_dt.isoformat(), None)
        except ValueError:
            pass

    return (None, None)


def _subcommand_to_date_range(subcommand: str) -> tuple[str | None, str | None]:
    """Convert any stats subcommand to a date range using its default window hours."""
    # Check for explicit presets first
    preset_result = _preset_to_date_range(subcommand)
    if preset_result != (None, None):
        return preset_result

    # For other subcommands (cron, providers, models, cron-week, etc.),
    # use their default window hours
    window_hours = _STATS_WINDOW_HOURS.get(subcommand)
    if window_hours is not None:
        now = datetime.now(timezone.utc)
        from_dt = now - timedelta(hours=window_hours)
        return (from_dt.isoformat(), None)

    # Fallback to 24h
    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(hours=24)
    return (from_dt.isoformat(), None)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-telemetry",
        description="Query hermes-telemetry data outside an active Hermes session.",
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    _build_parser_into(sub)
    return p


def _build_parser_into(sub) -> None:
    """Attach telemetry subcommands to an existing argparse subparsers action."""
    sp = sub.add_parser("stats", help="Show session statistics")
    sp.add_argument(
        "subcommand",
        nargs="?",
        default="today",
        choices=_STATS_CHOICES,
        metavar="SUBCOMMAND",
        help=f"One of: {', '.join(_STATS_CHOICES)} (default: today)",
    )
    sp.add_argument(
        "--from",
        dest="date_from",
        type=_parse_date,
        help="Start date (ISO 8601, e.g. 2025-01-15 or 2025-01-15T00:00:00Z)",
    )
    sp.add_argument(
        "--to",
        dest="date_to",
        type=_parse_date,
        help="End date (ISO 8601, exclusive). Defaults to now.",
    )
    sp.add_argument("--json", action="store_true", help="Output as JSON")

    bp = sub.add_parser("budget", help="Show budget status or set limits")
    bp.add_argument("--json", action="store_true", help="Output as JSON")
    bsub = bp.add_subparsers(dest="budget_command", metavar="BUDGET_COMMAND")

    bc = bsub.add_parser("cron", help="Per-cron-job budget status")
    bc.add_argument("--json", action="store_true", help="Output as JSON")

    bs = bsub.add_parser("set", help="Set a budget limit")
    bs.add_argument("scope", choices=["global", "cron_job", "sender"])
    bs.add_argument("window", choices=["daily", "monthly"])
    bs.add_argument("usd", type=float)

    bf = bsub.add_parser("forecast", help="Project burn rate toward the budget limit")
    bf.add_argument("window", nargs="?", choices=["daily", "monthly"], default="monthly")
    bf.add_argument("scope", nargs="?", default="global", choices=["global", "cron_job", "sender"])
    bf.add_argument("scope_id", nargs="?", default="")
    bf.add_argument("--json", action="store_true", help="Output as JSON")


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _dispatch(args, parser)


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser | None = None) -> None:
    if args.command == "stats":
        _handle_stats(args)
    elif args.command == "budget":
        _handle_budget(args)
    else:
        if parser:
            parser.print_help()
        sys.exit(0)


def _resolve_date_range(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Resolve the date range from --from/--to flags or from preset subcommand."""
    # Explicit --from/--to flags take precedence
    if args.date_from is not None:
        date_from = args.date_from
        date_to = args.date_to or datetime.now(timezone.utc).isoformat()
        return (date_from, date_to)

    # Otherwise use subcommand to determine date range
    return _subcommand_to_date_range(args.subcommand)


def _handle_stats(args: argparse.Namespace) -> None:
    date_from, date_to = _resolve_date_range(args)

    if args.json:
        _stats_json(args.subcommand, date_from, date_to)
    else:
        _stats_text(args.subcommand, date_from, date_to)


def _stats_text(subcommand: str, date_from: str | None, date_to: str | None) -> None:
    if subcommand.startswith("cron"):
        print(stats._cron_block(date_from=date_from, date_to=date_to))
    elif subcommand.startswith("providers"):
        print(stats._providers_block(date_from=date_from, date_to=date_to))
    elif subcommand.startswith("models"):
        print(stats._models_block(date_from=date_from, date_to=date_to))
    else:
        # today, week, month, last-N-days
        print(stats._summary_block(date_from=date_from, date_to=date_to))


def _stats_json(subcommand: str, date_from: str | None, date_to: str | None) -> None:
    if subcommand.startswith("cron"):
        data = db.cost_by_job(date_from=date_from, date_to=date_to)
    elif subcommand.startswith("providers"):
        data = db.stats_by_provider(date_from=date_from, date_to=date_to)
    elif subcommand.startswith("models"):
        data = db.stats_by_model(date_from=date_from, date_to=date_to)
    else:
        # today, week, month, last-N-days
        data = db.stats_summary(date_from=date_from, date_to=date_to)
    print(json.dumps(data, default=str))


def _handle_budget(args: argparse.Namespace) -> None:
    from . import budget as _budget

    if args.budget_command == "set":
        print(_budget._set_budget(args.scope, args.window, args.usd))
        return

    if args.budget_command == "forecast":
        if getattr(args, "json", False):
            _budget_forecast_json(args)
        else:
            _budget_forecast_text(args)
        return

    use_json = getattr(args, "json", False)
    if use_json:
        _budget_json(args.budget_command or "status")
    else:
        _budget_text(args.budget_command or "status")


def _budget_forecast_text(args: argparse.Namespace) -> None:
    from . import budget as _budget

    print(_budget._forecast_block(args.scope, args.scope_id or "", args.window))


def _budget_forecast_json(args: argparse.Namespace) -> None:
    from . import budget as _budget

    proj = _budget.burn_rate_projection(args.scope, args.scope_id or "", window=args.window)
    print(json.dumps(proj, default=str))


def _budget_text(subcommand: str) -> None:
    from . import budget as _budget

    if subcommand == "cron":
        print(_budget._cron_block())
    else:
        print(_budget._status_block())


def _budget_json(subcommand: str) -> None:
    import dataclasses as _dc

    from . import budget as _budget

    since30 = _budget._days_ago_utc(30)

    if subcommand == "cron":
        cron_ids = db.list_cron_job_ids(since30)
        rows = []
        for jid in cron_ids:
            v = _budget.check("cron_job", jid)
            if v is not None:
                rows.append(_dc.asdict(v))
        print(json.dumps(rows, default=str))
        return

    # Full status: global + cron_jobs + senders
    result: dict = {}

    g = _budget.check("global", "")
    result["global"] = _dc.asdict(g) if g is not None else None

    cron_ids = db.list_cron_job_ids(since30)
    result["cron_jobs"] = {}
    for jid in cron_ids:
        v = _budget.check("cron_job", jid)
        result["cron_jobs"][jid] = _dc.asdict(v) if v is not None else None

    sender_ids = db.list_sender_ids(since30)
    result["senders"] = {}
    for sid in sender_ids:
        v = _budget.check("sender", sid)
        result["senders"][sid] = _dc.asdict(v) if v is not None else None

    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
