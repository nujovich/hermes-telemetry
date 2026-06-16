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
  // Defensive destructure: older Hermes SDKs may not expose Tabs* — the
  // tab page renders its own button-based switcher so it never depends
  // on shadcn Tabs being present in the host shell.
  const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  const { useState, useEffect, useRef } = SDK.hooks;

  const PLUGIN = "hermes-telemetry";
  const api = (path) => SDK.fetchJSON(`/api/plugins/${PLUGIN}${path}`);

  // ---- formatting helpers ------------------------------------------------
  const fmtUsd = (v) => `$${Number(v || 0).toFixed(4)}`;
  const fmtInt = (v) => Number(v || 0).toLocaleString();
  const fmtMs = (v) => (v ? `${Math.round(v)} ms` : "—");
  const pct = (v) => `${Number(v || 0).toFixed(1)}%`;

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

  // ---- Tab page: internal sub-tabs --------------------------------------
  function StatCard({ label, value, hint }) {
    return h("div", { className: "flex flex-col gap-1 border border-border p-3" },
      h("span", { className: "text-xs text-muted-foreground uppercase" }, label),
      h("span", { className: "text-lg font-courier" }, value),
      hint ? h("span", { className: "text-xs text-muted-foreground" }, hint) : null,
    );
  }

  function SummaryPanel() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(() => {
      api("/summary?window_hours=24").then(setData).catch((e) => setErr(String(e)));
    }, []);
    if (err) return h(Card, null, h(CardContent, { className: "py-4 text-sm" },
      "Backend not reachable: ", err));
    if (!data) return h(Card, null, h(CardContent, { className: "py-4 text-sm" }, "Loading…"));
    const r = data.runs || {}, l = data.llm || {};
    return h("div", { className: "grid grid-cols-2 md:grid-cols-3 gap-3" },
      h(StatCard, { label: "Cost (24h)", value: fmtUsd(r.cost_usd) }),
      h(StatCard, { label: "Runs",       value: fmtInt(r.total_runs), hint: `${r.failed_runs || 0} failed` }),
      h(StatCard, { label: "API calls",  value: fmtInt(l.api_calls) }),
      h(StatCard, { label: "Tokens in",  value: fmtInt(r.tokens_in) }),
      h(StatCard, { label: "Tokens out", value: fmtInt(r.tokens_out) }),
      h(StatCard, { label: "Avg latency", value: fmtMs(l.avg_latency_ms) }),
    );
  }

  function RowsTable({ columns, rows, empty }) {
    if (!rows || !rows.length) return h("p", { className: "text-sm text-muted-foreground" }, empty || "No data.");
    return h("div", { className: "overflow-x-auto" },
      h("table", { className: "w-full text-xs font-courier border-collapse" },
        h("thead", null,
          h("tr", null, columns.map((c) =>
            h("th", { key: c.key, className: "text-left border-b border-border py-1 px-2" }, c.label))),
        ),
        h("tbody", null, rows.map((row, i) =>
          h("tr", { key: i, className: "border-b border-border/40" },
            columns.map((c) =>
              h("td", { key: c.key, className: "py-1 px-2" },
                c.render ? c.render(row) : (row[c.key] ?? "—")))))),
      ),
    );
  }

  function RunsPanel() {
    const [data, setData] = useState(null);
    useEffect(() => { api("/runs?limit=50&window_hours=168").then(setData).catch(() => setData({ rows: [] })); }, []);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    return h(RowsTable, {
      rows: data.rows,
      empty: "No runs in the last 7 days.",
      columns: [
        { key: "started_at", label: "Started" },
        { key: "session_id", label: "Session", render: (r) => h("span", { title: r.session_id }, (r.session_id || "").slice(0, 18) + "…") },
        { key: "platform",   label: "Platform" },
        { key: "model",      label: "Model" },
        { key: "provider",   label: "Provider" },
        { key: "status",     label: "Status" },
        { key: "cost_usd",   label: "Cost",   render: (r) => fmtUsd(r.cost_usd) },
        { key: "tokens_in",  label: "Tok in", render: (r) => fmtInt(r.tokens_in) },
        { key: "tokens_out", label: "Tok out", render: (r) => fmtInt(r.tokens_out) },
      ],
    });
  }

  function RequestsPanel() {
    const [data, setData] = useState(null);
    useEffect(() => { api("/requests?limit=100&window_hours=168").then(setData).catch(() => setData({ rows: [] })); }, []);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    return h(RowsTable, {
      rows: data.rows,
      empty: "No LLM calls in the last 7 days.",
      columns: [
        { key: "ts",         label: "Time" },
        { key: "model",      label: "Model" },
        { key: "provider",   label: "Provider" },
        { key: "tokens_in",  label: "Tok in",  render: (r) => fmtInt(r.tokens_in) },
        { key: "tokens_out", label: "Tok out", render: (r) => fmtInt(r.tokens_out) },
        { key: "cost_usd",   label: "Cost",    render: (r) => fmtUsd(r.cost_usd) },
        { key: "latency_ms", label: "Latency", render: (r) => fmtMs(r.latency_ms) },
        { key: "estimated",  label: "Est?",    render: (r) => (r.estimated ? "yes" : "no") },
      ],
    });
  }

  function ProvidersPanel() {
    const [data, setData] = useState(null);
    useEffect(() => { api("/providers?window_hours=168").then(setData).catch(() => setData({ rows: [] })); }, []);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    return h(RowsTable, {
      rows: data.rows,
      empty: "No provider activity.",
      columns: [
        { key: "provider",       label: "Provider" },
        { key: "total_calls",    label: "Calls",     render: (r) => fmtInt(r.total_calls) },
        { key: "estimated_calls", label: "Estimated", render: (r) => fmtInt(r.estimated_calls) },
        { key: "cost_usd",       label: "Cost",      render: (r) => fmtUsd(r.cost_usd) },
        { key: "tokens_in",      label: "Tok in",    render: (r) => fmtInt(r.tokens_in) },
        { key: "tokens_out",     label: "Tok out",   render: (r) => fmtInt(r.tokens_out) },
      ],
    });
  }

  function CronPanel() {
    const [data, setData] = useState(null);
    useEffect(() => { api("/cron?window_hours=720").then(setData).catch(() => setData({ rows: [] })); }, []);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    return h(RowsTable, {
      rows: data.rows,
      empty: "No cron runs recorded.",
      columns: [
        { key: "cron_job_id", label: "Job" },
        { key: "runs",        label: "Runs",       render: (r) => fmtInt(r.runs) },
        { key: "ok_runs",     label: "Ok",         render: (r) => fmtInt(r.ok_runs) },
        { key: "failed_runs", label: "Failed",     render: (r) => fmtInt(r.failed_runs) },
        { key: "cost_usd",    label: "Cost",       render: (r) => fmtUsd(r.cost_usd) },
        { key: "last_run",    label: "Last run" },
      ],
    });
  }

  function BudgetsPanel() {
    const [data, setData] = useState(null);
    useEffect(() => { api("/budget").then(setData).catch(() => setData({ enabled: false })); }, []);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    if (!data.enabled) {
      return h(Card, null, h(CardContent, { className: "py-4 text-sm text-muted-foreground" },
        "No budget.yaml configured. Run /setup or /budget set."));
    }
    if (!data.scopes || !data.scopes.length) {
      return h(Card, null, h(CardContent, { className: "py-4 text-sm text-muted-foreground" },
        "Budget enabled but no global daily/monthly limit set."));
    }
    return h("div", { className: "grid gap-3" }, data.scopes.map((s) =>
      h(Card, { key: s.scope },
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-2" },
            h(CardTitle, { className: "text-sm" }, s.scope),
            h(Badge, { variant: s.level === "ok" ? "outline" : "destructive" }, s.level),
          ),
        ),
        h(CardContent, { className: "text-sm font-courier" },
          `${fmtUsd(s.spent_usd)} / ${fmtUsd(s.limit_usd)}  (${pct(s.pct)})`),
      )));
  }

  const TABS = [
    { id: "summary",   label: "Summary",   render: SummaryPanel },
    { id: "runs",      label: "Runs",      render: RunsPanel },
    { id: "requests",  label: "Requests",  render: RequestsPanel },
    { id: "providers", label: "Providers", render: ProvidersPanel },
    { id: "cron",      label: "Cron",      render: CronPanel },
    { id: "budgets",   label: "Budgets",   render: BudgetsPanel },
  ];

  function TabBar({ tabs, active, onChange }) {
    return h("div", { className: "flex items-center gap-1 border-b border-border" },
      tabs.map((t) => {
        const isActive = t.id === active;
        const cls = isActive
          ? "px-3 py-1 text-sm font-medium border-b-2 border-foreground cursor-pointer"
          : "px-3 py-1 text-sm text-muted-foreground hover:text-foreground cursor-pointer";
        return h("button", {
          key: t.id,
          type: "button",
          onClick: () => onChange(t.id),
          className: cls,
        }, t.label);
      }),
    );
  }

  function TelemetryPage() {
    const [active, setActive] = useState("summary");
    const Panel = (TABS.find((t) => t.id === active) || TABS[0]).render;
    return h("div", { className: "flex flex-col gap-4" },
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-lg" }, "Telemetry"),
            h(Badge, { variant: "outline" }, "hermes-telemetry"),
          ),
        ),
        h(CardContent, { className: "flex flex-col gap-4" },
          h(TabBar, { tabs: TABS, active, onChange: setActive }),
          h(Panel, null),
        ),
      ),
    );
  }

  // ---- Slot widgets ------------------------------------------------------
  function SessionsTopWidget() {
    // The shell renders sessions:top on /sessions; the active session id is
    // not yet exposed by the SDK, so we surface the most-recent run as a
    // pinned "last session" card. When the SDK adds useActiveSession we
    // swap this for a per-row card.
    const [run, setRun] = useState(null);
    useEffect(() => {
      api("/runs?limit=1&window_hours=0")
        .then((d) => setRun((d.rows && d.rows[0]) || null))
        .catch(() => setRun(null));
    }, []);
    if (!run) return null;
    return h(Card, { className: "border-dashed" },
      h(CardContent, { className: "py-2 flex items-center gap-3 text-xs font-courier" },
        h(Badge, { variant: "outline" }, "Telemetry"),
        h("span", null, "Last run: ", h("strong", null, fmtUsd(run.cost_usd))),
        h("span", { className: "text-muted-foreground" },
          `${fmtInt(run.tokens_in)} in / ${fmtInt(run.tokens_out)} out · ${run.model || "—"}`),
      ),
    );
  }

  function CronTopWidget() {
    const [data, setData] = useState(null);
    useEffect(() => {
      api("/cron?window_hours=168").then(setData).catch(() => setData({ rows: [] }));
    }, []);
    if (!data) return null;
    const total = (data.rows || []).reduce((acc, r) => acc + (r.cost_usd || 0), 0);
    const failed = (data.rows || []).reduce((acc, r) => acc + (r.failed_runs || 0), 0);
    return h(Card, { className: "border-dashed" },
      h(CardContent, { className: "py-2 flex items-center gap-3 text-xs font-courier" },
        h(Badge, { variant: "outline" }, "Telemetry"),
        h("span", null, "Cron 7d: ", h("strong", null, fmtUsd(total))),
        failed ? h(Badge, { variant: "destructive" }, `${failed} failed`) : null,
      ),
    );
  }

  function HeaderRightWidget() {
    const [state, setState] = useState({ cost: null, level: "ok", pct: null });
    useEffect(() => {
      Promise.all([api("/summary?window_hours=24"), api("/budget")])
        .then(([s, b]) => {
          const cost = (s.runs && s.runs.cost_usd) || 0;
          const daily = (b.scopes || []).find((x) => x.window === "daily");
          setState({ cost, level: daily ? daily.level : "ok", pct: daily ? daily.pct : null });
        })
        .catch(() => setState({ cost: null, level: "ok", pct: null }));
    }, []);
    if (state.cost === null) return null;
    const variant = state.level === "hard" ? "destructive" : "outline";
    const suffix = state.pct !== null ? ` · ${pct(state.pct)}` : "";
    return h(Badge, { variant, className: "font-courier" },
      `24h: $${Number(state.cost).toFixed(2)}${suffix}`);
  }

  function AnalyticsBottomChart() {
    const ref = useRef(null);
    const [degraded, setDegraded] = useState(false);
    useEffect(() => {
      let chart = null;
      Promise.all([ensureChart(), api("/summary?window_hours=720")])
        .then(([Chart, data]) => {
          if (!ref.current) return;
          const points = data.daily_cost || [];
          chart = new Chart(ref.current, {
            type: "line",
            data: {
              labels: points.map((p) => p.day),
              datasets: [{
                label: "Daily cost (USD)",
                data: points.map((p) => p.cost),
                tension: 0.25,
              }],
            },
            options: { responsive: true, maintainAspectRatio: false },
          });
        })
        .catch(() => setDegraded(true));
      return () => { if (chart) chart.destroy(); };
    }, []);
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, { className: "text-base" }, "Telemetry — daily cost (30d)")),
      h(CardContent, null,
        degraded
          ? h("p", { className: "text-xs text-muted-foreground" },
              "Chart.js unavailable (CDN blocked). The /telemetry tab has the same data in tabular form.")
          : h("div", { style: { height: "240px" } }, h("canvas", { ref })),
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
