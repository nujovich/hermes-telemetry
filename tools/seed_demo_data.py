#!/usr/bin/env python3
"""Seed the telemetry DB with realistic-looking demo data for screenshots.

Usage:
    HERMES_HOME=~/.hermes-test python tools/seed_demo_data.py

Inserts a mix of CLI and cron sessions across the last ~14 days, with
different models, providers, token counts, costs, latencies and a few
failed runs / failed tool calls so the dashboard panels show varied data.

This script writes directly to telemetry.db through the runtime API
(``db.start_run`` / ``db.record_llm_call`` / ``db.record_tool_call``), so
the resulting rows are schema-correct. It's safe to run multiple times —
each invocation appends a new batch.

Do NOT point this at your real ~/.hermes — by default it refuses unless
HERMES_HOME is set to something other than ~/.hermes.
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure we import the runtime db module (sibling of this tools/ dir).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

# ---------------------------------------------------------------------------
# Safety check
# ---------------------------------------------------------------------------
home = Path(os.environ.get("HERMES_HOME", "")).expanduser().resolve()
real_home = (Path.home() / ".hermes").resolve()
if not home or home == real_home:
    sys.exit(
        "Refusing to seed: HERMES_HOME must be set to a test directory "
        "(e.g. ~/.hermes-test). Got: " + str(home or "<unset>")
    )
print(f"Seeding into HERMES_HOME={home}")

random.seed(42)

MODELS = [
    ("anthropic/claude-sonnet-4", "anthropic", 3.0, 15.0),
    ("anthropic/claude-opus-4-7", "anthropic", 15.0, 75.0),
    ("openai/gpt-4o", "openai", 2.5, 10.0),
    ("openai/gpt-4o-mini", "openai", 0.15, 0.60),
    ("google/gemini-2.5-pro", "google", 1.25, 5.0),
    ("nvidia/nemotron-3-ultra", "nvidia", 0.10, 0.50),
    ("openrouter/qwen3.7-plus", "openrouter", 0.40, 1.20),
]
PLATFORMS = ["cli", "cli", "cli", "cron", "cron", "telegram"]
TOOLS = [
    ("read_file", 0.98),
    ("write_file", 0.96),
    ("patch", 0.93),
    ("terminal", 0.91),
    ("search_files", 0.99),
    ("delegate_task", 0.85),
    ("memory", 0.97),
    ("browser_navigate", 0.80),
]
CRON_JOBS = ["daily-digest", "sync-inbox", "kanban-report", "weekly-summary"]


def _ts(offset_hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=offset_hours)).isoformat()


def seed_session(offset_hours: float, *, force_cron: str | None = None):
    model, provider, price_in, price_out = random.choice(MODELS)
    platform = "cron" if force_cron else random.choice(PLATFORMS)
    cron_job_id = (
        force_cron if force_cron else (random.choice(CRON_JOBS) if platform == "cron" else None)
    )

    if platform == "cron":
        session_id = (
            "cron_"
            + cron_job_id
            + "_"
            + (datetime.now(timezone.utc) - timedelta(hours=offset_hours)).strftime("%Y%m%d_%H%M%S")
        )
    else:
        session_id = (
            (datetime.now(timezone.utc) - timedelta(hours=offset_hours)).strftime("%Y%m%d_%H%M%S")
            + "_"
            + uuid.uuid4().hex[:6]
        )

    db.start_run(session_id=session_id, model=model, platform=platform, cron_job_id=cron_job_id)

    # 1–6 API calls per session
    n_calls = random.randint(1, 6)
    for i in range(n_calls):
        tokens_in = random.randint(500, 12000)
        tokens_out = random.randint(80, 2500)
        cache_read = random.choice([0, 0, 0, random.randint(100, 5000)])
        reasoning = random.choice([0, 0, random.randint(50, 800)])
        cost = (
            tokens_in * price_in
            + tokens_out * price_out
            + cache_read * (price_in * 0.1)
            + reasoning * price_out
        ) / 1_000_000
        latency = random.randint(450, 6200)
        # Roughly 8% estimated rows (no provider usage info)
        estimated = random.random() < 0.08
        db.record_llm_call(
            session_id=session_id,
            ts=_ts(offset_hours - i * 0.01),
            model=model,
            provider=provider,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 6),
            latency_ms=latency,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            estimated=estimated,
        )

    # 0–8 tool calls per session
    n_tools = random.randint(0, 8)
    for _ in range(n_tools):
        tool_name, ok_rate = random.choice(TOOLS)
        ok = random.random() < ok_rate
        db.record_tool_call(
            session_id=session_id,
            tool_name=tool_name,
            ok=ok,
            latency_ms=random.randint(40, 4500),
            ts=_ts(offset_hours),
        )

    # ~6% of runs end in failure
    status = "error" if random.random() < 0.06 else "ok"
    db.end_run(session_id, status=status)


def main():
    # Spread ~40 sessions across the last 14 days, with a denser tail in
    # the last 24h so the "24h" panels look populated.
    for _ in range(25):
        seed_session(offset_hours=random.uniform(0, 24))
    for _ in range(15):
        seed_session(offset_hours=random.uniform(24, 14 * 24))
    # A few dedicated cron runs per job so the Cron tab has variety.
    for job in CRON_JOBS:
        for h in (3, 27, 51, 99):
            seed_session(offset_hours=h, force_cron=job)
    print("✓ Seeded ~56 sessions. Reload /telemetry in the browser.")


if __name__ == "__main__":
    main()
