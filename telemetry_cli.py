from __future__ import annotations

import argparse
import json
import sys

from . import db, stats

_STATS_WINDOW_HOURS: dict[str, int] = {
    "today": 24,
    "week": 168,
    "month": 720,
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


def _handle_stats(args: argparse.Namespace) -> None:
    if args.json:
        _stats_json(args.subcommand)
    else:
        _stats_text(args.subcommand)


def _stats_text(subcommand: str) -> None:
    hours = _STATS_WINDOW_HOURS.get(subcommand, 24)
    if subcommand in ("today", "week", "month"):
        print(stats._summary_block(hours))
    elif subcommand.startswith("cron"):
        print(stats._cron_block(hours))
    elif subcommand.startswith("providers"):
        print(stats._providers_block(hours))
    elif subcommand.startswith("models"):
        print(stats._models_block(hours))
    else:
        print(stats._summary_block(24))


def _stats_json(subcommand: str) -> None:
    hours = _STATS_WINDOW_HOURS.get(subcommand, 24)
    if subcommand in ("today", "week", "month"):
        data = db.stats_summary(hours)
    elif subcommand.startswith("cron"):
        data = db.cost_by_job(hours)
    elif subcommand.startswith("providers"):
        data = db.stats_by_provider(hours)
    elif subcommand.startswith("models"):
        data = db.stats_by_model(hours)
    else:
        data = db.stats_summary(24)
    print(json.dumps(data, default=str))


def _handle_budget(args: argparse.Namespace) -> None:
    from . import budget as _budget

    if args.budget_command == "set":
        print(_budget._set_budget(args.scope, args.window, args.usd))
        return

    use_json = getattr(args, "json", False)
    if use_json:
        _budget_json(args.budget_command or "status")
    else:
        _budget_text(args.budget_command or "status")


def _budget_text(subcommand: str) -> None:
    from . import budget as _budget

    if subcommand == "cron":
        print(_budget._cron_block())
    else:
        print(_budget._status_block())


def _budget_json(subcommand: str) -> None:
    pass  # implemented in Task 6


if __name__ == "__main__":
    main()
