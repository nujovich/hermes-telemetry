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

  const profileQS = (p) => (p ? `&profile=${encodeURIComponent(p)}` : "");

  function SummaryPanel({ profile }) {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(() => {
      api(`/summary?window_hours=24${profileQS(profile)}`).then(setData).catch((e) => setErr(String(e)));
    }, [profile]);
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
      // Only shown when MoA presets were used. References are untracked, so the
      // aggregator cost is a lower bound — the hint says so.
      (r.moa_calls ? h(StatCard, {
        label: "MoA calls",
        value: fmtInt(r.moa_calls),
        hint: "aggregator only — refs untracked",
      }) : null),
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

  function RunsPanel({ profile }) {
    const [data, setData] = useState(null);
    useEffect(() => { api(`/runs?limit=50&window_hours=168${profileQS(profile)}`).then(setData).catch(() => setData({ rows: [] })); }, [profile]);
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

  function RequestsPanel({ profile }) {
    const [data, setData] = useState(null);
    useEffect(() => { api(`/requests?limit=100&window_hours=168${profileQS(profile)}`).then(setData).catch(() => setData({ rows: [] })); }, [profile]);
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
        { key: "provider_assumed", label: "Asm?", render: (r) => (r.provider_assumed ? "yes" : "no") },
        // MoA aggregator calls carry the preset name; reference-model tokens are
        // untracked (they run through Hermes' auxiliary path, which fires no
        // hooks), so a MoA row's cost is a lower bound.
        { key: "moa_preset", label: "MoA", render: (r) => (r.moa_preset ? "▲ " + r.moa_preset : "—") },
      ],
    });
  }

  function ProvidersPanel({ profile }) {
    const [data, setData] = useState(null);
    useEffect(() => { api(`/providers?window_hours=168${profileQS(profile)}`).then(setData).catch(() => setData({ rows: [] })); }, [profile]);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    return h(RowsTable, {
      rows: data.rows,
      empty: "No provider activity.",
      columns: [
        { key: "provider",       label: "Provider" },
        { key: "total_calls",    label: "Calls",     render: (r) => fmtInt(r.total_calls) },
        { key: "estimated_calls", label: "Estimated", render: (r) => fmtInt(r.estimated_calls) },
        { key: "provider_assumed_calls", label: "Assumed", render: (r) => fmtInt(r.provider_assumed_calls) },
        { key: "cost_usd",       label: "Cost",      render: (r) => fmtUsd(r.cost_usd) },
        { key: "tokens_in",      label: "Tok in",    render: (r) => fmtInt(r.tokens_in) },
        { key: "tokens_out",     label: "Tok out",   render: (r) => fmtInt(r.tokens_out) },
      ],
    });
  }

  function CronPanel({ profile }) {
    const [data, setData] = useState(null);
    useEffect(() => { api(`/cron?window_hours=720${profileQS(profile)}`).then(setData).catch(() => setData({ rows: [] })); }, [profile]);
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
    const [forecast, setForecast] = useState(null);
    useEffect(() => { api("/budget").then(setData).catch(() => setData({ enabled: false })); }, []);
    useEffect(() => {
      api("/forecast?window=monthly").then(setForecast).catch(() => setForecast({ enabled: false }));
    }, []);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    if (!data.enabled) {
      return h(Card, null, h(CardContent, { className: "py-4 text-sm text-muted-foreground" },
        "No budget.yaml configured. Run /setup or /budget set."));
    }
    if (!data.scopes || !data.scopes.length) {
      return h(Card, null, h(CardContent, { className: "py-4 text-sm text-muted-foreground" },
        "Budget enabled but no global daily/monthly limit set."));
    }
    const scopeCards = data.scopes.map((s) =>
      h(Card, { key: s.scope },
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-2" },
            h(CardTitle, { className: "text-sm" }, s.scope),
            h(Badge, { variant: s.level === "ok" ? "outline" : "destructive" }, s.level),
          ),
        ),
        h(CardContent, { className: "text-sm font-courier" },
          `${fmtUsd(s.spent_usd)} / ${fmtUsd(s.limit_usd)}  (${pct(s.pct)})`),
      ));
    // Burn-rate forecast (global scope) — only shown when a limit is configured.
    const forecastCard = forecast && forecast.enabled
      ? h(Card, { key: "__forecast" },
          h(CardHeader, null,
            h("div", { className: "flex items-center gap-2" },
              h(CardTitle, { className: "text-sm" }, `Burn-rate forecast (${forecast.window})`),
              h(Badge, { variant: forecast.status === "ok" ? "outline" : "destructive" }, forecast.status),
            ),
          ),
          h(CardContent, { className: "text-sm font-courier" },
            `Projected ${fmtUsd(forecast.projected_total_usd)} / ${fmtUsd(forecast.limit_usd)} ` +
            `(${pct(Number(forecast.projected_pct || 0) * 100)}) · avg ${fmtUsd(forecast.avg_daily_usd)}/day` +
            (forecast.est_days_to_breach != null
              ? ` · breach in ~${Number(forecast.est_days_to_breach).toFixed(1)}d`
              : "")),
        )
      : null;
    return h("div", { className: "grid gap-3" }, [...scopeCards, forecastCard]);
  }

  function EfficiencyPanel({ profile }) {
    const [data, setData] = useState(null);
    useEffect(() => {
      api(`/efficiency?window_hours=24${profileQS(profile)}`).then(setData).catch(() => setData({ sessions: [] }));
    }, [profile]);
    if (!data) return h("p", { className: "text-sm" }, "Loading…");
    const rows = data.sessions || [];
    return h("div", { className: "flex flex-col gap-3" },
      h("p", { className: "text-xs text-muted-foreground" },
        `Average ${Number(data.average_score || 0).toFixed(1)}/100 across ` +
        `${fmtInt(data.sessions_scored || rows.length)} session(s). ` +
        "90+ Excellent · 70-89 Good · 50-69 Fair · <50 Needs attention."),
      h(RowsTable, {
        rows,
        empty: "No completed sessions in the last 24h.",
        columns: [
          { key: "efficiency_score", label: "Score", render: (r) => Number(r.efficiency_score || 0).toFixed(1) },
          { key: "status",     label: "Status" },
          { key: "api_calls",  label: "API",     render: (r) => fmtInt(r.api_calls) },
          { key: "tokens_in",  label: "Tok in",  render: (r) => fmtInt(r.tokens_in) },
          { key: "tokens_out", label: "Tok out", render: (r) => fmtInt(r.tokens_out) },
          { key: "cost_usd",   label: "Cost",    render: (r) => fmtUsd(r.cost_usd) },
          { key: "session_id", label: "Session", render: (r) => h("span", { title: r.session_id }, (r.session_id || "").slice(0, 18) + "…") },
        ],
      }),
    );
  }

  const TABS = [
    { id: "summary",   label: "Summary",   render: SummaryPanel },
    { id: "runs",      label: "Runs",      render: RunsPanel },
    { id: "requests",  label: "Requests",  render: RequestsPanel },
    { id: "providers", label: "Providers", render: ProvidersPanel },
    { id: "cron",      label: "Cron",      render: CronPanel },
    { id: "budgets",   label: "Budgets",   render: BudgetsPanel },
    { id: "efficiency", label: "Efficiency", render: EfficiencyPanel },
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
    const [profile, setProfile] = useState("");        // "" = all profiles
    const [profiles, setProfiles] = useState([]);
    useEffect(() => {
      api("/profiles").then((d) => setProfiles((d && d.profiles) || [])).catch(() => setProfiles([]));
    }, []);
    const Panel = (TABS.find((t) => t.id === active) || TABS[0]).render;
    return h("div", { className: "flex flex-col gap-4" },
      h(SmellsWidget, { profile }),
      h(ModelUnavailableWidget, null),
      h(TierTransitionsWidget, null),
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center gap-3" },
            h(CardTitle, { className: "text-lg" }, "Telemetry"),
            h(Badge, { variant: "outline" }, "hermes-telemetry"),
            (profiles.length ? h("select", {
              className: "ml-auto text-sm border border-border bg-transparent px-2 py-1",
              value: profile,
              onChange: (e) => setProfile(e.target.value),
              title: "Filter by Hermes profile",
              "aria-label": "Filter by Hermes profile",
            },
              h("option", { value: "" }, "All profiles"),
              profiles.map((p) => h("option", { key: p, value: p }, p)),
            ) : null),
          ),
        ),
        h(CardContent, { className: "flex flex-col gap-4" },
          h(TabBar, { tabs: TABS, active, onChange: setActive }),
          h(Panel, { profile }),
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
      // Pull a handful of recent runs and pick the first one with real data.
      // The literal last run in the table can be a session that died at
      // init (no API key, agent never made a call), which shows as a noisy
      // "Last run: $0.0000 · 0 in / 0 out" badge.
      api("/runs?limit=10&window_hours=0")
        .then((d) => {
          const rows = (d && d.rows) || [];
          const real = rows.find((r) =>
            (r.cost_usd || 0) > 0
            || (r.tokens_in || 0) > 0
            || (r.tokens_out || 0) > 0
            || r.model,
          );
          setRun(real || null);
        })
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

  function TierTransitionsWidget() {
    // Surfaces models that flipped from $0 to a paid charge in the last 72h.
    // Renders nothing if there are no transitions in the window — the slot
    // stays invisible during the happy path.
    const [rows, setRows] = useState([]);
    useEffect(() => {
      api("/tier-transitions?window_hours=72")
        .then((d) => setRows((d && d.rows) || []))
        .catch(() => setRows([]));
    }, []);
    if (!rows.length) return null;
    const now = Date.now();
    const within24h = rows.some((r) => {
      const t = Date.parse(r.detected_at);
      return Number.isFinite(t) && (now - t) < 24 * 3600 * 1000;
    });
    const daysFree = (r) => {
      if (!r.first_free_seen_at) return null;
      const t0 = Date.parse(r.first_free_seen_at);
      const t1 = Date.parse(r.detected_at);
      if (!Number.isFinite(t0) || !Number.isFinite(t1)) return null;
      const d = Math.max(0, Math.round((t1 - t0) / (24 * 3600 * 1000)));
      return d;
    };
    return h(Card, { className: "border-dashed" },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-sm flex items-center gap-2" },
          h(Badge, { variant: within24h ? "destructive" : "outline" }, "Tier change"),
          h("span", null, `${rows.length} model${rows.length === 1 ? "" : "s"} flipped free→paid (72h)`),
        ),
      ),
      h(CardContent, { className: "py-2 space-y-1 text-xs font-courier" },
        rows.slice(0, 5).map((r, i) => {
          const d = daysFree(r);
          const provider = r.provider ? ` · ${r.provider}` : "";
          const wasFree = d !== null ? ` · was free for ${d}d` : "";
          return h("div", { key: i, className: "flex items-center gap-2" },
            h("strong", null, r.model),
            h("span", { className: "text-muted-foreground" },
              `${provider}${wasFree} · first charge ${fmtUsd(r.first_paid_cost_usd)}`),
          );
        }),
      ),
    );
  }

  function ModelUnavailableWidget() {
    // Surfaces models that 404'd in the last 72h (api_request_error hook,
    // issue #43). Sibling to TierTransitionsWidget — same happy-path
    // contract: render nothing when the table is empty for the window so
    // the slot stays invisible in normal operation.
    const [rows, setRows] = useState([]);
    useEffect(() => {
      api("/model-unavailable?window_hours=72")
        .then((d) => setRows((d && d.rows) || []))
        .catch(() => setRows([]));
    }, []);
    if (!rows.length) return null;
    const now = Date.now();
    const within24h = rows.some((r) => {
      const t = Date.parse(r.last_seen_at);
      return Number.isFinite(t) && (now - t) < 24 * 3600 * 1000;
    });
    return h(Card, { className: "border-dashed" },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-sm flex items-center gap-2" },
          h(Badge, { variant: within24h ? "destructive" : "outline" }, "Model unavailable"),
          h("span", null, `${rows.length} model${rows.length === 1 ? "" : "s"} 404'd (72h)`),
        ),
      ),
      h(CardContent, { className: "py-2 space-y-1 text-xs font-courier" },
        rows.slice(0, 5).map((r, i) => {
          const provider = r.provider ? ` on ${r.provider}` : "";
          const occ = r.occurrences > 1 ? ` · seen ${r.occurrences}×` : "";
          return h("div", { key: i, className: "flex items-center gap-2" },
            h("strong", null, r.model),
            h("span", { className: "text-muted-foreground" },
              `${provider} · HTTP ${r.error_code}${occ}`),
          );
        }),
      ),
    );
  }

  function SmellsWidget({ profile }) {
    // Surfaces AI anti-patterns detected in the last 24h. Same happy-path
    // contract as the sibling widgets: render nothing when no smells fire so
    // it stays invisible in normal operation.
    const [data, setData] = useState(null);
    useEffect(() => {
      api(`/smells?window_hours=24${profileQS(profile)}`).then(setData).catch(() => setData({ smells: [] }));
    }, [profile]);
    const smells = (data && data.smells) || [];
    if (!smells.length) return null;
    const hasHigh = smells.some((s) => s.severity === "high");
    return h(Card, { className: "border-dashed" },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-sm flex items-center gap-2" },
          h(Badge, { variant: hasHigh ? "destructive" : "outline" }, "AI smells"),
          h("span", null, `${smells.length} anti-pattern${smells.length === 1 ? "" : "s"} detected (24h)`),
        ),
      ),
      h(CardContent, { className: "py-2 space-y-1 text-xs font-courier" },
        smells.slice(0, 5).map((s, i) =>
          h("div", { key: i, className: "flex items-center gap-2" },
            h(Badge, { variant: s.severity === "high" ? "destructive" : "outline" },
              (s.severity || "").toUpperCase()),
            h("strong", null, (s.smell || "").replace(/_/g, " ")),
            h("span", { className: "text-muted-foreground" }, s.detail || ""),
          )),
      ),
    );
  }

  const P = window.__HERMES_PLUGINS__;
  P.register(PLUGIN, TelemetryPage);
  P.registerSlot(PLUGIN, "sessions:top", SessionsTopWidget);
  P.registerSlot(PLUGIN, "cron:top", CronTopWidget);
  P.registerSlot(PLUGIN, "header-right", HeaderRightWidget);
  P.registerSlot(PLUGIN, "analytics:bottom", AnalyticsBottomChart);
  // TierTransitionsWidget and ModelUnavailableWidget are rendered inside
  // TelemetryPage, NOT via registerSlot — the Hermes shell only renders
  // slots from its catalogue (sessions:top, cron:top, header-right,
  // analytics:bottom) and silently drops anything else.
  // See ONBOARDING.md § Slot widgets.
})();
