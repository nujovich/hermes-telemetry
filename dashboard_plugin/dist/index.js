/**
 * hermes-telemetry — Hermes dashboard plugin (IIFE, no build step).
 *
 * Verified against NousResearch/hermes-agent@main:
 *   - SDK shape:       website/docs/user-guide/features/extending-the-dashboard.md
 *   - Loader:          hermes_cli/web_server.py::_discover_dashboard_plugins
 *   - Slot catalogue:  same doc, "Slot catalogue" table
 *
 * The matching standalone dashboard lives at hermes_telemetry/dashboard/.
 * The two surfaces are intentionally independent.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) {
    console.warn("[hermes-telemetry] SDK not found; aborting plugin load.");
    return;
  }

  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  const { useState, useEffect } = SDK.hooks;

  const PLUGIN = "hermes-telemetry";
  const api = (path) => SDK.fetchJSON(`/api/plugins/${PLUGIN}${path}`);

  // ---- Chart.js, loaded once from CDN ------------------------------------
  let _chartReady = null;
  function ensureChart() {
    if (_chartReady) return _chartReady;
    _chartReady = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/chart.js@4";
      s.onload = () => resolve(window.Chart);
      s.onerror = () => reject(new Error("Chart.js CDN unreachable"));
      document.head.appendChild(s);
    });
    return _chartReady;
  }

  // ---- Main tab page -----------------------------------------------------
  function TelemetryPage() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);

    useEffect(() => {
      api("/summary?window_hours=24")
        .then(setData)
        .catch((e) => setErr(String(e)));
    }, []);

    if (err) {
      return h(Card, null, h(CardContent, { className: "py-4 text-sm" },
        "Telemetry backend not reachable: ", err));
    }
    if (!data) {
      return h(Card, null, h(CardContent, { className: "py-4 text-sm" }, "Loading…"));
    }

    const runs = data.runs || {};
    const llm = data.llm || {};
    return h("div", { className: "flex flex-col gap-4" },
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-lg" }, "Telemetry — last 24h"),
            h(Badge, { variant: "outline" }, `${runs.total_runs || 0} runs`),
          ),
        ),
        h(CardContent, { className: "grid grid-cols-2 gap-3 text-sm" },
          h("div", null, "Cost (USD): ", h("strong", null, (runs.cost_usd || 0).toFixed(4))),
          h("div", null, "API calls: ", h("strong", null, llm.api_calls || 0)),
          h("div", null, "Tokens in: ", h("strong", null, runs.tokens_in || 0)),
          h("div", null, "Tokens out: ", h("strong", null, runs.tokens_out || 0)),
          h("div", null, "Avg latency (ms): ",
            h("strong", null, llm.avg_latency_ms ? Math.round(llm.avg_latency_ms) : "—")),
          h("div", null, "Failed runs: ", h("strong", null, runs.failed_runs || 0)),
        ),
      ),
    );
  }

  // ---- Slot widgets (placeholders for now; wired in follow-up steps) -----
  function SessionsTopWidget() {
    return h(Card, { className: "border-dashed" },
      h(CardContent, { className: "py-2 flex items-center gap-2 text-xs" },
        h(Badge, { variant: "outline" }, "Telemetry"),
        h("span", { className: "text-muted-foreground" },
          "Per-session tokens & cost appear here once the session is recorded."),
      ),
    );
  }

  function CronTopWidget() {
    return h(Card, { className: "border-dashed" },
      h(CardContent, { className: "py-2 flex items-center gap-2 text-xs" },
        h(Badge, { variant: "outline" }, "Telemetry"),
        h("span", { className: "text-muted-foreground" },
          "Per-cron-job budget status and pause state appear here."),
      ),
    );
  }

  function HeaderRightWidget() {
    const [cost, setCost] = useState(null);
    useEffect(() => {
      api("/summary?window_hours=24")
        .then((d) => setCost((d.runs && d.runs.cost_usd) || 0))
        .catch(() => setCost(null));
    }, []);
    if (cost === null) return null;
    return h(Badge, { variant: "outline", className: "font-courier" },
      `24h: $${Number(cost).toFixed(2)}`);
  }

  function AnalyticsBottomChart() {
    const ref = React.useRef(null);
    useEffect(() => {
      let chart = null;
      Promise.all([ensureChart(), api("/summary?window_hours=720")])
        .then(([Chart, data]) => {
          if (!ref.current) return;
          const points = (data.daily_cost || []);
          chart = new Chart(ref.current, {
            type: "line",
            data: {
              labels: points.map((p) => p.day),
              datasets: [{ label: "Daily cost (USD)", data: points.map((p) => p.cost) }],
            },
            options: { responsive: true, maintainAspectRatio: false },
          });
        })
        .catch(() => {/* CDN blocked or backend down — degrade silently */});
      return () => { if (chart) chart.destroy(); };
    }, []);
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Telemetry — daily cost")),
      h(CardContent, null,
        h("div", { style: { height: "240px" } }, h("canvas", { ref })),
      ),
    );
  }

  const P = window.__HERMES_PLUGINS__;
  P.register(PLUGIN, TelemetryPage);
  P.registerSlot(PLUGIN, "sessions:top", SessionsTopWidget);
  P.registerSlot(PLUGIN, "cron:top", CronTopWidget);
  P.registerSlot(PLUGIN, "header-right", HeaderRightWidget);
  P.registerSlot(PLUGIN, "analytics:bottom", AnalyticsBottomChart);
})();
