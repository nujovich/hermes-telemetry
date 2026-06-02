"""Interactive setup wizard for hermes-telemetry.

Runs automatically on first plugin load when pricing.yaml and/or budget.yaml
are missing. Also available as the /setup slash command for re-configuration.

Usage (programmatic):
    from hermes_telemetry import setup
    setup.run(interactive=True)   # full wizard
    setup.run(interactive=False)  # auto-generate defaults, no prompts
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths (resolved lazily so tests can monkeypatch HERMES_HOME)
# ---------------------------------------------------------------------------

def _pricing_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "telemetry" / "pricing.yaml"


def _budget_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "telemetry" / "budget.yaml"


def _tele_dir() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    d = hermes_home / "telemetry"
    return d


# ---------------------------------------------------------------------------
# Default pricing seed — same models as _DEFAULT_PRICING in pricing.py
# Values: USD per 1M tokens.  Sources: official provider pages, May 2026.
# ---------------------------------------------------------------------------
_DEFAULT_SEED: dict[str, dict] = {
    # Anthropic / Nous Portal
    "claude-opus-4-8":    dict(input=5.00,  output=25.00, cache_read=0.50,  cache_write=6.25),
    "claude-opus-4-7":    dict(input=5.00,  output=25.00, cache_read=0.50,  cache_write=6.25),
    "claude-sonnet-4-6":  dict(input=3.00,  output=15.00, cache_read=0.30,  cache_write=3.75),
    "claude-sonnet-4-5":  dict(input=3.00,  output=15.00, cache_read=0.30,  cache_write=3.75),
    "claude-haiku-4-5":   dict(input=0.80,  output=4.00,  cache_read=0.08,  cache_write=1.00),
    "claude-opus-4":      dict(input=15.00, output=75.00, cache_read=1.50,  cache_write=18.75),
    "claude-sonnet-4":    dict(input=3.00,  output=15.00, cache_read=0.30,  cache_write=3.75),
    "claude-3-5-sonnet-20241022": dict(input=3.00, output=15.00, cache_read=0.30, cache_write=3.75),
    "claude-3-5-haiku-20241022":  dict(input=0.80, output=4.00,  cache_read=0.08, cache_write=1.00),
    "claude-3-opus-20240229":     dict(input=15.00, output=75.00, cache_read=1.50, cache_write=18.75),
    "claude-3-haiku-20240307":    dict(input=0.25, output=1.25,  cache_read=0.03, cache_write=0.30),
    # OpenAI
    "gpt-4o":        dict(input=2.50,  output=10.00),
    "gpt-4o-mini":   dict(input=0.15,  output=0.60),
    "gpt-4-turbo":   dict(input=10.00, output=30.00),
    "gpt-4":         dict(input=30.00, output=60.00),
    "gpt-3.5-turbo": dict(input=0.50,  output=1.50),
    "o1":            dict(input=15.00, output=60.00),
    "o1-mini":       dict(input=3.00,  output=12.00),
    "o3":            dict(input=10.00, output=40.00),
    "o3-mini":       dict(input=1.10,  output=4.40),
    "o4-mini":       dict(input=1.10,  output=4.40),
    # DeepSeek
    "deepseek-chat": dict(input=0.27, output=1.10),
    "deepseek-v3":   dict(input=0.27, output=1.10),
    "deepseek-r1":   dict(input=0.55, output=2.19),
    # Nous Research (Portal)
    "owl-alpha":                   dict(input=0.00, output=0.00),
    "hermes-3-llama-3.1-405b":     dict(input=3.00,  output=15.00),
    "hermes-3-llama-3.1-70b":      dict(input=0.70,  output=0.90),
    # Meta (via OpenRouter)
    "meta-llama/llama-3.1-405b-instruct": dict(input=2.70, output=2.70),
    "meta-llama/llama-3.1-70b-instruct":  dict(input=0.52, output=0.75),
    "meta-llama/llama-3.3-70b-instruct":  dict(input=0.59, output=0.79),
    # Google
    "gemini-1.5-pro":   dict(input=3.50,  output=10.50),
    "gemini-1.5-flash": dict(input=0.075, output=0.30),
    "gemini-2.0-flash": dict(input=0.10,  output=0.40),
    "gemini-2.5-pro":   dict(input=1.25,  output=10.00),
}


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------
def _dump_yaml(data: dict) -> str:
    """Serialize a dict to YAML string using stdlib-only fallback or PyYAML."""
    try:
        import yaml
        from io import StringIO
        buf = StringIO()
        yaml.dump(data, buf, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return buf.getvalue()
    except ImportError:
        # Minimal YAML serializer for our flat structure
        lines = []
        _serialize_yaml_node(data, lines, indent=0)
        return "\n".join(lines) + "\n"


def _serialize_yaml_node(node, lines: list, indent: int):
    prefix = "  " * indent
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, dict):
                lines.append(f"{prefix}{k}:")
                _serialize_yaml_node(v, lines, indent + 1)
            elif isinstance(v, list):
                lines.append(f"{prefix}{k}:")
                for item in v:
                    lines.append(f"{prefix}- {item}")
            elif isinstance(v, float):
                lines.append(f"{prefix}{k}: {v:.4f}")
            elif isinstance(v, bool):
                lines.append(f"{prefix}{k}: {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'{prefix}{k}: "{v}"')
            else:
                lines.append(f"{prefix}{k}: {v}")
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                _serialize_yaml_node(item, lines, indent + 1)
            else:
                lines.append(f"{prefix}- {item}")


# ---------------------------------------------------------------------------
# Pricing setup
# ---------------------------------------------------------------------------
def _fetch_openrouter_models() -> dict[str, dict]:
    """Fetch all models with fixed pricing from OpenRouter API.
    Returns {model_id: {input, output}} in USD per 1M tokens.
    Models with negative prices (no fixed pricing) are excluded.
    """
    import urllib.request
    import urllib.error
    import json as _json

    url = "https://openrouter.ai/api/v1/models"
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-telemetry/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
    except Exception as exc:
        logger.warning("setup: OpenRouter fetch failed: %s", exc)
        return {}

    result = {}
    for m in data.get("data", []):
        mid = m.get("id", "")
        pricing = m.get("pricing", {})
        if not mid or not pricing:
            continue
        try:
            inp = float(pricing.get("prompt", "0"))
            out = float(pricing.get("completion", "0"))
        except (ValueError, TypeError):
            continue
        if inp <= 0 and out <= 0:
            continue  # skip models without fixed pricing
        result[mid] = {
            "input": round(inp * 1_000_000, 4),
            "output": round(out * 1_000_000, 4),
        }
    return result


def _build_pricing_yaml(models: dict[str, dict]) -> str:
    """Build a pricing.yaml string from a model dict."""
    defaults = {"cache_read_multiplier": 0.10, "cache_write_multiplier": 1.25}
    data = {
        "models": models,
        "defaults": defaults,
    }
    header = (
        "# ~/.hermes/telemetry/pricing.yaml\n"
        "# Auto-generated by hermes-telemetry setup.\n"
        "# Prices in USD per 1 million tokens.\n"
        "#\n"
        "# Models with 'openrouter/' prefix = manual (auto-refresh won't overwrite).\n"
        "# Models without prefix = auto-refreshed from OpenRouter API.\n"
        "#\n"
        "# Add your own overrides:\n"
        "#   \"my-custom-model\":\n"
        "#     input: 1.00\n"
        "#     output: 3.00\n"
        "#\n"
    )
    return header + _dump_yaml(data)


def _write_pricing(models: dict[str, dict]) -> Path:
    _tele_dir().mkdir(parents=True, exist_ok=True)
    content = _build_pricing_yaml(models)
    p = _pricing_path()
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Budget setup
# ---------------------------------------------------------------------------
DEFAULT_BUDGET = {
    "budgets": {
        "global": {
            "daily_usd": 5.00,
            "monthly_usd": 100.00,
        },
    },
    "thresholds": {
        "soft_pct": 0.80,
        "hard_pct": 1.00,
    },
    "on_estimated": {
        "mode": "warn_only",
    },
}


def _build_budget_yaml(data: dict) -> str:
    header = (
        "# ~/.hermes/telemetry/budget.yaml\n"
        "# Auto-generated by hermes-telemetry setup.\n"
        "# All amounts in USD. Windows are evaluated in your LOCAL timezone.\n"
        "#\n"
        "# Enforcement:\n"
        "#   soft (≥80%)  → one-time-per-window notice injected into chat\n"
        "#   hard (≥100%) → tool calls blocked, cron jobs paused\n"
        "#\n"
        "# Scopes:\n"
        "#   global  — all spend combined (recommended)\n"
        "#\n"
        "# Note: per_cron_job and per_sender scopes are NOT recommended.\n"
        "# Subagent (delegate_task) cost cannot be attributed to a parent cron job.\n"
        "# Use the global budget to cap total spend including delegated work.\n"
        "#\n"
    )
    return header + _dump_yaml(data)


def _write_budget(data: dict) -> Path:
    _tele_dir().mkdir(parents=True, exist_ok=True)
    content = _build_budget_yaml(data)
    p = _budget_path()
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Public API — called from __init__.py or /setup command
# ---------------------------------------------------------------------------
def run(interactive: bool = True, daily_usd: float = 5.00, monthly_usd: float = 100.00) -> str:
    """Run the setup wizard.

    Args:
        interactive: If True, prints prompts and reads stdin.
                     If False, auto-generates defaults (used by first-load auto-setup).
        daily_usd:   Default daily budget when non-interactive.
        monthly_usd: Default monthly budget when non-interactive.

    Returns:
        A human-readable summary of what was configured.
    """
    lines: list[str] = []
    lines.append("hermes-telemetry — first-time setup")
    lines.append("=" * 50)

    # ---- Pricing ----
    pricing_done = False
    if _pricing_path().exists():
        lines.append(f"\n[Pricing] Already configured: {_pricing_path()}")
        lines.append("  Skipping (delete the file to re-run pricing setup).")
        pricing_done = True

    if not pricing_done:
        lines.append("\n[Pricing] No pricing.yaml found.")
        if interactive:
            lines.append("  How do you want to configure model prices?")
            lines.append("  1) Auto-generate: built-in defaults + fetch from OpenRouter API")
            lines.append("  2) Minimal: built-in defaults only (~30 models, no network)")
            lines.append("  3) Manual: I'll add models myself later")
            lines.append("")
            # We can't actually read stdin from a plugin hook, so we return
            # instructions instead. The /setup command handler deals with I/O.
            lines.append("  Run /setup again and pass an option:")
            lines.append("    /setup pricing auto     → option 1")
            lines.append("    /setup pricing minimal  → option 2")
            lines.append("    /setup pricing skip     → option 3")
            return "\n".join(lines)
        else:
            # Non-interactive: auto-generate with defaults + OpenRouter fetch
            models = dict(_DEFAULT_SEED)
            try:
                or_models = _fetch_openrouter_models()
                # Merge: OpenRouter models get the openrouter/ prefix
                for mid, prices in or_models.items():
                    if mid not in models:
                        models[mid] = prices
                lines.append(f"  Fetched {len(or_models)} models from OpenRouter API.")
            except Exception as exc:
                lines.append(f"  OpenRouter fetch failed ({exc}), using built-in defaults only.")
            path = _write_pricing(models)
            lines.append(f"  Wrote {len(models)} models to {path}")
            pricing_done = True

    # ---- Budget ----
    budget_done = False
    if _budget_path().exists():
        lines.append(f"\n[Budget] Already configured: {_budget_path()}")
        lines.append("  Skipping (delete the file to re-run budget setup).")
        budget_done = True

    if not budget_done:
        lines.append("\n[Budget] No budget.yaml found.")
        if interactive:
            lines.append("  How do you want to configure budgets?")
            lines.append("  1) Recommended: global budget ($5/day, $100/month)")
            lines.append("  2) Custom: I'll set my own limits")
            lines.append("  3) Skip: no budgets (costs still tracked, no enforcement)")
            lines.append("")
            lines.append("  Run /setup again and pass an option:")
            lines.append("    /setup budget default   → option 1")
            lines.append("    /setup budget custom    → option 2")
            lines.append("    /setup budget skip      → option 3")
            return "\n".join(lines)
        else:
            # Non-interactive: write recommended defaults
            bdata = {
                "budgets": {
                    "global": {
                        "daily_usd": daily_usd,
                        "monthly_usd": monthly_usd,
                    },
                },
                "thresholds": {"soft_pct": 0.80, "hard_pct": 1.00},
                "on_estimated": {"mode": "warn_only"},
            }
            path = _write_budget(bdata)
            lines.append(f"  Wrote default budget to {path}")
            lines.append(f"  Global: ${daily_usd:.2f}/day, ${monthly_usd:.2f}/month")
            budget_done = True

    # ---- Summary ----
    lines.append("\n" + "=" * 50)
    lines.append("Setup complete!")
    lines.append("")
    lines.append("Next steps:")
    lines.append("  1. Restart the Hermes gateway: hermes gateway restart")
    lines.append("  2. Run any session, then type /stats to see captured data")
    lines.append("  3. Adjust budgets anytime with /budget set global daily <amount>")
    lines.append("")
    lines.append("Files:")
    if _pricing_path().exists():
        lines.append(f"  Pricing : {_pricing_path()}")
    if _budget_path().exists():
        lines.append(f"  Budget  : {_budget_path()}")
    lines.append(f"  DB      : {_tele_dir() / 'telemetry.db'}")
    lines.append(f"  Log     : {_tele_dir() / 'telemetry.log'}")

    return "\n".join(lines)


def handle_command(raw_args: str) -> str:
    """Handler for the /setup slash command.

    Subcommands:
        /setup                    → show current status + instructions
        /setup pricing auto       → auto-generate pricing (defaults + OpenRouter)
        /setup pricing minimal    → built-in defaults only
        /setup pricing skip       → don't configure pricing
        /setup budget default     → recommended global budget ($5/d, $100/mo)
        /setup budget custom      → set your own limits
        /setup budget skip        → no budgets
    """
    args = (raw_args or "").strip().lower()
    parts = args.split()

    if not parts:
        # Status
        lines = ["hermes-telemetry — setup status", "=" * 40]
        lines.append(f"  pricing.yaml: {'found' if _pricing_path().exists() else 'NOT FOUND'}")
        lines.append(f"  budget.yaml : {'found' if _budget_path().exists() else 'NOT FOUND'}")
        lines.append("")
        if not _pricing_path().exists() or not _budget_path().exists():
            lines.append("  Run setup:")
            if not _pricing_path().exists():
                lines.append("    /setup pricing auto     → defaults + OpenRouter fetch")
                lines.append("    /setup pricing minimal  → built-in defaults only")
                lines.append("    /setup pricing skip     → skip")
            if not _budget_path().exists():
                lines.append("    /setup budget default   → $5/day, $100/month")
                lines.append("    /setup budget custom    → set your own")
                lines.append("    /setup budget skip      → no budgets")
        return "\n".join(lines)

    sub = parts[0]

    if sub == "pricing":
        if len(parts) < 2:
            return "Usage: /setup pricing <auto|minimal|skip>"
        choice = parts[1]
        if choice == "auto":
            models = dict(_DEFAULT_SEED)
            or_models = _fetch_openrouter_models()
            for mid, prices in or_models.items():
                if mid not in models:
                    models[mid] = prices
            path = _write_pricing(models)
            return (
                f"Pricing configured: {len(models)} models written to {path}\n"
                f"  ({len(_DEFAULT_SEED)} built-in + {len(or_models)} from OpenRouter)\n"
                f"  Restart the gateway to pick up changes."
            )
        elif choice == "minimal":
            path = _write_pricing(dict(_DEFAULT_SEED))
            return (
                f"Pricing configured: {len(_DEFAULT_SEED)} built-in models written to {path}\n"
                f"  Restart the gateway to pick up changes."
            )
        elif choice == "skip":
            return "Pricing setup skipped. Models not in the pricing table will record $0.00 cost."
        else:
            return f"Unknown option {choice!r}. Use: auto | minimal | skip"

    if sub == "budget":
        if len(parts) < 2:
            return "Usage: /setup budget <default|custom|skip>"
        choice = parts[1]
        if choice == "default":
            bdata = {
                "budgets": {"global": {"daily_usd": 5.00, "monthly_usd": 100.00}},
                "thresholds": {"soft_pct": 0.80, "hard_pct": 1.00},
                "on_estimated": {"mode": "warn_only"},
            }
            path = _write_budget(bdata)
            return (
                f"Budget configured: global $5.00/day, $100.00/month\n"
                f"  Written to {path}\n"
                f"  Adjust anytime with: /budget set global daily <amount>\n"
                f"  Restart the gateway to pick up changes."
            )
        elif choice == "custom":
            return (
                "Custom budget setup:\n"
                "  /budget set global daily <amount>\n"
                "  /budget set global monthly <amount>\n"
                "  Example: /budget set global daily 10.00"
            )
        elif choice == "skip":
            return "Budget setup skipped. Costs will be tracked but not enforced. Add budget.yaml later to enable."
        else:
            return f"Unknown option {choice!r}. Use: default | custom | skip"

    return (
        "Usage: /setup [pricing|budget] [option]\n"
        "  /setup pricing <auto|minimal|skip>\n"
        "  /setup budget  <default|custom|skip>"
    )
