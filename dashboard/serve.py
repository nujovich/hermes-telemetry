#!/usr/bin/env python3
"""hermes-telemetry dashboard server -- zero dependencies, stdlib only.

Usage:
    python serve.py                            # http://localhost:8765 (loopback only)
    python serve.py --port 9090                # custom port, still loopback
    python serve.py 9090                       # positional port (back-compat)
    python serve.py --host 0.0.0.0             # bind all interfaces (no auth!)

The dashboard has no authentication. By default it binds to 127.0.0.1 so it is
unreachable from other hosts. To view it from another machine, either:

  - Open an SSH tunnel from your client:
        ssh -L 8765:localhost:8765 <user>@<server>
    then browse http://localhost:8765 on the client.

  - Or, on a trusted LAN only, pass --host 0.0.0.0 to bind all interfaces.
    Anyone who can reach the chosen port will see every captured token, cost,
    and tool-call detail with no login. Do not expose this to the public
    internet or to networks that include untrusted hosts.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

logger = logging.getLogger("hermes_telemetry.dashboard")

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


def _since_clause(window_hours, col="started_at"):
    """Return SQL WHERE clause for time window. 0 = all time (no filter).
    Args:
        window_hours: 0 = all time (no filter), otherwise hours back
        col: column name (default 'started_at', use 'ts' for llm_calls)
    """
    wh = int(window_hours)
    if wh == 0:
        return "1=1"
    return f"{col} >= datetime('now', '-{wh} hours')"


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------
def api_summary(window_hours=24):
    since_clause = _since_clause(window_hours, "started_at")
    since_clause_ts = _since_clause(window_hours, "ts")

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
        FROM runs WHERE {since_clause}
    """)

    llm = _one(f"""
        SELECT COUNT(*) AS api_calls, AVG(latency_ms) AS avg_latency_ms
        FROM llm_calls WHERE {since_clause_ts}
    """)

    top_tools = _rows(f"""
        SELECT tool_name, COUNT(*) AS calls,
               SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures,
               AVG(latency_ms) AS avg_ms
        FROM tool_calls tc
        JOIN runs r ON tc.session_id = r.session_id
        WHERE {_since_clause(window_hours, "r.started_at")}
        GROUP BY tool_name ORDER BY calls DESC LIMIT 10
    """)

    # daily cost chart data (last 7 days for 24h/7d windows, last 30 days for 30d, last 90 days for 90d, unbounded for all-time)
    daily_window = int(window_hours)
    if daily_window == 0:
        daily_cost = _rows("""
            SELECT DATE(started_at) AS day,
                   ROUND(SUM(cost_usd), 4) AS cost,
                   COUNT(*) AS runs
            FROM runs
            GROUP BY DATE(started_at)
            ORDER BY day
        """)
    else:
        daily_cost = _rows(f"""
            SELECT DATE(started_at) AS day,
                   ROUND(SUM(cost_usd), 4) AS cost,
                   COUNT(*) AS runs
            FROM runs
            WHERE started_at >= datetime('now', '-{daily_window // 24} days')
            GROUP BY DATE(started_at)
            ORDER BY day
        """)

    return {
        "window_hours": int(window_hours),
        "runs": runs,
        "llm": llm,
        "top_tools": top_tools,
        "daily_cost": daily_cost,
    }


def api_token_breakdown(window_hours=24):
    """Get detailed token breakdown: input, output, cache_read, cache_write, reasoning."""
    since_clause_ts = _since_clause(window_hours, "ts")
    return _one(f"""
        SELECT
            SUM(tokens_in) AS tokens_in,
            SUM(tokens_out) AS tokens_out,
            SUM(cache_read_tokens) AS cache_read_tokens,
            SUM(cache_write_tokens) AS cache_write_tokens,
            SUM(reasoning_tokens) AS reasoning_tokens,
            COALESCE(SUM(tokens_in) + SUM(tokens_out) + SUM(cache_read_tokens) + SUM(cache_write_tokens) + SUM(reasoning_tokens), 0) AS total_tokens
        FROM llm_calls WHERE {since_clause_ts}
    """)


def api_cron(window_hours=168):
    since_clause = _since_clause(window_hours)
    return _rows(f"""
        SELECT cron_job_id,
               COUNT(*) AS runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed_runs,
               SUM(tokens_in) AS tokens_in,
               SUM(tokens_out) AS tokens_out,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               ROUND(SUM(cost_usd), 6) AS cost_usd,
               AVG(duration_ms) AS avg_duration_ms,
               MAX(started_at) AS last_run
        FROM runs
        WHERE cron_job_id IS NOT NULL
          AND {since_clause}
        GROUP BY cron_job_id
        ORDER BY cost_usd DESC
    """)


def api_providers(window_hours=24):
    since_clause_ts = _since_clause(window_hours, "ts")
    return _rows(f"""
        SELECT provider,
               COUNT(*) AS total_calls,
               SUM(CASE WHEN estimated=0 THEN 1 ELSE 0 END) AS real_calls,
               SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END) AS estimated_calls,
               ROUND(SUM(cost_usd), 6) AS cost_usd
        FROM llm_calls
        WHERE {since_clause_ts}
        GROUP BY provider
        ORDER BY total_calls DESC
    """)


def api_runs(limit=50):
    return _rows(
        """
        SELECT session_id, platform, cron_job_id, model, provider,
               started_at, ended_at, status,
               tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
               cost_usd, duration_ms,
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


def api_budget_update(payload):
    """Update budget.yaml from POST payload. Returns updated budget status or error."""
    budget_path = DB_PATH.parent / "budget.yaml"
    if not budget_path.exists():
        return {"enabled": False, "error": "budget.yaml not found"}

    try:
        import yaml
    except ImportError:
        return {"enabled": False, "error": "PyYAML not installed"}

    try:
        cfg = yaml.safe_load(budget_path.read_text()) or {}
    except Exception as e:
        return {"enabled": False, "error": f"Failed to parse budget.yaml: {e}"}

    # Expected payload: {"scope": "global", "window": "daily", "limit_usd": 5.0}
    scope = payload.get("scope", "global")
    window = payload.get("window", "daily")
    limit_usd = payload.get("limit_usd")

    if limit_usd is None:
        return {"enabled": False, "error": "limit_usd is required"}

    try:
        limit_usd = float(limit_usd)
    except (ValueError, TypeError):
        return {"enabled": False, "error": "limit_usd must be a number"}

    if limit_usd < 0:
        return {"enabled": False, "error": "limit_usd must be >= 0"}

    # Initialize budgets structure if missing
    if "budgets" not in cfg:
        cfg["budgets"] = {}
    if scope not in cfg["budgets"]:
        cfg["budgets"][scope] = {}

    # Update the limit
    key = f"{window}_usd"
    cfg["budgets"][scope][key] = limit_usd

    # Optional: update thresholds
    if "soft_pct" in payload:
        try:
            soft = float(payload["soft_pct"])
            if 0 <= soft <= 1:
                cfg.setdefault("thresholds", {})["soft_pct"] = soft
        except (ValueError, TypeError):
            pass
    if "hard_pct" in payload:
        try:
            hard = float(payload["hard_pct"])
            if 0 <= hard <= 1:
                cfg.setdefault("thresholds", {})["hard_pct"] = hard
        except (ValueError, TypeError):
            pass
    if "on_estimated_mode" in payload:
        mode = payload["on_estimated_mode"]
        if mode in ("warn_only", "enforce"):
            cfg.setdefault("on_estimated", {})["mode"] = mode

    # Write back
    try:
        budget_path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False))
    except Exception as e:
        return {"enabled": False, "error": f"Failed to write budget.yaml: {e}"}

    # Return updated status by calling api_budget()
    return api_budget()


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
            qs = parse_qs(parsed.query)
            return self._json(api_providers(int(qs.get("hours", [24])[0])))

        if path == "/api/runs":
            qs = parse_qs(parsed.query)
            return self._json(api_runs(qs.get("limit", [50])[0]))

        if path == "/api/budget":
            return self._json(api_budget())

        if path == "/api/token-breakdown":
            qs = parse_qs(parsed.query)
            return self._json(api_token_breakdown(int(qs.get("hours", [24])[0])))

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

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/budget":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid JSON")
                return
            result = api_budget_update(payload)
            return self._json(result)

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
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv=None):
    """Parse command-line arguments.

    Back-compat: the original signature was `serve.py [port]` — a single
    positional integer. Preserved so existing scripts and docs keep working.
    """
    parser = argparse.ArgumentParser(
        prog="serve.py",
        description="hermes-telemetry dashboard server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  serve.py                       # bind 127.0.0.1:8765\n"
            "  serve.py --port 9090           # custom port, still loopback\n"
            "  serve.py 9090                  # positional port (back-compat)\n"
            "  serve.py --host 0.0.0.0        # all interfaces (NO AUTH!)\n"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"Interface to bind to (default: {DEFAULT_HOST}). Use 0.0.0.0 to "
            "expose on every interface — the dashboard has NO authentication, "
            "so only do this on a trusted LAN."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to bind to (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "port_positional",
        nargs="?",
        type=int,
        default=None,
        metavar="PORT",
        help="Back-compat: positional port (use --port instead).",
    )
    args = parser.parse_args(argv)

    port = args.port if args.port is not None else args.port_positional
    if port is None:
        port = DEFAULT_PORT
    return args.host, port


def _warn_if_exposed(host: str) -> None:
    """Print a clear warning when binding to anything except loopback."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    msg = (
        f"WARNING: binding dashboard on {host} exposes it to every host "
        "that can reach this interface, and the dashboard has NO "
        "authentication. Anyone who reaches the port will see every "
        "captured token, cost, and tool-call detail. Do not expose to "
        "the public internet or to untrusted networks."
    )
    print(msg, file=sys.stderr)
    logger.warning("Dashboard bound on %s with no authentication.", host)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    host, port = _parse_args(argv)

    if not DB_PATH.exists():
        print(f"ERROR: telemetry DB not found at {DB_PATH}")
        print("Make sure hermes-telemetry plugin has captured data.")
        sys.exit(1)

    _warn_if_exposed(host)

    server = HTTPServer((host, port), Handler)
    display_host = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"hermes-telemetry dashboard at http://{display_host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
