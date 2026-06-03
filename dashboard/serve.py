#!/usr/bin/env python3
"""hermes-telemetry dashboard server -- zero dependencies, stdlib only.

Usage:
    python serve.py           # serves on http://localhost:8765
    python serve.py 9090      # custom port
"""

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# DB path
# ---------------------------------------------------------------------------
DB_PATH = (
    Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "telemetry" / "telemetry.db"
)

_local = threading.local()


def _conn():
    if not getattr(_local, "c", None):
        _local.c = sqlite3.connect(str(DB_PATH), isolation_level=None)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA busy_timeout=5000")
    return _local.c


def _rows(sql, params=()):
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _one(sql, params=()):
    r = _conn().execute(sql, params).fetchone()
    return dict(r) if r else {}


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------
def api_summary(window_hours=24):
    wh = int(window_hours)
    since = f"datetime('now', '-{wh} hours')"

    runs = _one(f"""
        SELECT
            COUNT(*) AS total_runs,
            SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
            SUM(CASE WHEN status NOT IN ('ok','running') THEN 1 ELSE 0 END) AS failed_runs,
            SUM(tokens_in) AS tokens_in,
            SUM(tokens_out) AS tokens_out,
            ROUND(SUM(cost_usd), 6) AS cost_usd,
            AVG(duration_ms) AS avg_duration_ms,
            SUM(tool_calls) AS tool_calls,
            SUM(estimated_llm_calls) AS estimated_llm_calls
        FROM runs WHERE started_at >= {since}
    """)

    llm = _one(f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE ts >= {since}
    """)

    top_tools = _rows(f"""
        SELECT tool_name, COUNT(*) AS calls,
               SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures,
               AVG(latency_ms) AS avg_ms
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE r.started_at >= {since}
        GROUP BY tool_name ORDER BY calls DESC LIMIT 10
    """)

    # daily cost chart data (last 7 days)
    daily_cost = _rows("""
        SELECT DATE(started_at) AS day,
               ROUND(SUM(cost_usd), 4) AS cost,
               COUNT(*) AS runs
        FROM runs
        WHERE started_at >= datetime('now', '-7 days')
        GROUP BY DATE(started_at)
        ORDER BY day
    """)

    return {
        "window_hours": wh,
        "runs": runs,
        "llm": llm,
        "top_tools": top_tools,
        "daily_cost": daily_cost,
    }


def api_cron(window_hours=168):
    return _rows(f"""
        SELECT cron_job_id,
               COUNT(*) AS runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed_runs,
               SUM(tokens_in) AS tokens_in,
               SUM(tokens_out) AS tokens_out,
               ROUND(SUM(cost_usd), 6) AS cost_usd,
               AVG(duration_ms) AS avg_duration_ms,
               MAX(started_at) AS last_run
        FROM runs
        WHERE cron_job_id IS NOT NULL
          AND started_at >= datetime('now', '-{int(window_hours)} hours')
        GROUP BY cron_job_id
        ORDER BY cost_usd DESC
    """)


def api_providers(window_hours=24):
    return _rows(f"""
        SELECT provider,
               COUNT(*) AS total_calls,
               SUM(CASE WHEN estimated=0 THEN 1 ELSE 0 END) AS real_calls,
               SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END) AS estimated_calls,
               ROUND(SUM(cost_usd), 6) AS cost_usd
        FROM llm_calls
        WHERE ts >= datetime('now', '-{int(window_hours)} hours')
        GROUP BY provider
        ORDER BY total_calls DESC
    """)


def api_runs(limit=50):
    return _rows(
        """
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cost_usd, duration_ms,
               api_calls, tool_calls, estimated_llm_calls
        FROM runs
        ORDER BY started_at DESC
        LIMIT ?
    """,
        (int(limit),),
    )


def api_budget():
    budget_path = DB_PATH.parent / "budget.yaml"
    if not budget_path.exists():
        return {"enabled": False}

    try:
        import yaml

        cfg = yaml.safe_load(budget_path.read_text())
    except ImportError:
        return {"enabled": True, "raw": budget_path.read_text()}

    budgets = cfg.get("budgets", {})
    thresholds = cfg.get("thresholds", {})
    on_est = cfg.get("on_estimated", {})

    # evaluate each scope
    now = datetime.now(timezone.utc)
    now.strftime("%Y-%m-%d")
    now.strftime("%Y-%m")
    local_now = now.astimezone()
    local_now.strftime("%Y-%m-%d")
    local_now.strftime("%Y-%m")

    scopes = []

    # global daily
    g = budgets.get("global", {})
    for win_key, win_label, since in [
        ("daily", "global/daily", now - timedelta(hours=24)),
        ("monthly", "monthly", now - timedelta(days=30)),
    ]:
        limit = g.get(f"{win_key}_usd")
        if limit is None:
            continue
        spend = _one(
            "SELECT COALESCE(SUM(cost_usd),0.0) AS spent, COALESCE(SUM(estimated_llm_calls),0) AS est, COALESCE(SUM(api_calls),0) AS total FROM runs WHERE started_at >= ?",
            (since.isoformat(),),
        )
        spent = float(spend.get("spent", 0))
        pct = spent / limit if limit > 0 else 0
        soft_pct = thresholds.get("soft_pct", 0.8)
        hard_pct = thresholds.get("hard_pct", 1.0)
        level = "ok"
        if pct >= hard_pct:
            level = "hard"
        elif pct >= soft_pct:
            level = "soft"
        scopes.append(
            {
                "scope": win_label,
                "spent": round(spent, 6),
                "limit": limit,
                "pct": round(pct * 100, 1),
                "level": level,
                "estimated_calls": spend.get("est", 0),
                "total_calls": spend.get("total", 0),
            }
        )

    return {"enabled": True, "budgets": scopes, "on_estimated": on_est.get("mode", "warn_only")}


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # API routes
        if path == "/api/summary":
            qs = parse_qs(parsed.query)
            return self._json(api_summary(qs.get("hours", [24])[0]))

        if path == "/api/cron":
            qs = parse_qs(parsed.query)
            return self._json(api_cron(qs.get("hours", [168])[0]))

        if path == "/api/providers":
            return self._json(api_providers())

        if path == "/api/runs":
            qs = parse_qs(parsed.query)
            return self._json(api_runs(qs.get("limit", [50])[0]))

        if path == "/api/budget":
            return self._json(api_budget())

        # Static: serve index.html for /
        if path == "/" or path == "/index.html":
            html = (SCRIPT_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
            return

        # Try to serve other static files from the same directory
        fpath = SCRIPT_DIR / path.lstrip("/")
        if fpath.is_file():
            super().do_GET()
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if not DB_PATH.exists():
        print(f"ERROR: telemetry DB not found at {DB_PATH}")
        print("Make sure hermes-telemetry plugin has captured data.")
        sys.exit(1)

    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"hermes-telemetry dashboard at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
