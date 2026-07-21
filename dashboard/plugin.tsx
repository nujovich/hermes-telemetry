/**
 * Hermes Telemetry Desktop Plugin
 *
 * HermesPlugin for the Hermes Desktop shell. Registers a pane contribution
 * that polls the /desktop backend endpoint and renders budget status at a
 * glance, plus an "Open Dashboard" quick action.
 *
 * Installation:
 *   1. Compile to JS (esbuild / tsc):
 *      esbuild plugin.tsx --bundle --outfile=plugin.js \
 *        --external:@hermes/plugin-sdk --external:react --external:react/jsx-runtime
 *   2. Place plugin.js at:
 *      $HERMES_HOME/desktop-plugins/hermes-telemetry/plugin.js
 *   3. The Hermes Desktop shell discovers it automatically (disk door).
 *
 * The plugin id ('hermes-telemetry') matches manifest.json 'name' so
 * ctx.rest(path) resolves to /api/plugins/hermes-telemetry/<path> —
 * the same backend that serves the web dashboard.
 *
 * Bundled alternative: cp plugin.tsx to
 *   apps/desktop/src/plugins/hermes-telemetry/plugin.tsx
 * in the hermes-agent repo — vite glob picks it up as a bundled plugin.
 */

import {
  HermesPlugin,
  PluginContext,
  PluginContribution,
  PANES_AREA,
  useQuery,
  host,
  icons,
  Badge,
  Button,
  Loader,
  ErrorState,
  EmptyState,
  Separator,
  cn,
  relativeTime,
} from '@hermes/plugin-sdk'

/* ------------------------------------------------------------------ */
/*  Types                                                               */
/* ------------------------------------------------------------------ */

interface DesktopPayload {
  last_run: {
    session_id: string
    platform: string
    status: string
    timestamp: string
    cost: number
  } | null
  session_count: number
  running_count: number
  month_to_date: {
    cost: number
    tokens_in: number
    tokens_out: number
  }
  budget: Record<string, {
    used: number
    limit: number
    remaining: number
    exhausted: boolean
  }>
  actions: Record<string, string>
}

interface BudgetScopeProps {
  name: string
  scope: {
    used: number
    limit: number
    remaining: number
    exhausted: boolean
  }
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                             */
/* ------------------------------------------------------------------ */

function formatCost(cents: number): string {
  if (cents >= 100) {
    return `$${(cents / 100).toFixed(2)}`
  }
  return `${cents.toFixed(1)}c`
}

function formatTokens(count: number): string {
  if (count >= 1_000_000) {
    return `${(count / 1_000_000).toFixed(1)}M`
  }
  if (count >= 1_000) {
    return `${(count / 1_000).toFixed(0)}K`
  }
  return String(count)
}

function pct(used: number, limit: number): number {
  if (limit <= 0) return 0
  return Math.min(100, Math.round((used / limit) * 100))
}

function BudgetScope({ name, scope }: BudgetScopeProps) {
  const usedPct = pct(scope.used, scope.limit)

  return (
    <div className="hermes-telemetry-scope">
      <div className="hermes-telemetry-scope-header">
        <span className="hermes-telemetry-scope-name">{name}</span>
        {scope.exhausted && (
          <Badge variant="destructive">Exhausted</Badge>
        )}
      </div>
      <div className="hermes-telemetry-bar-track">
        <div
          className={cn(
            'hermes-telemetry-bar-fill',
            usedPct >= 90 && 'hermes-telemetry-bar-critical',
            usedPct >= 75 && usedPct < 90 && 'hermes-telemetry-bar-warning',
          )}
          style={{ width: `${usedPct}%` }}
        />
      </div>
      <div className="hermes-telemetry-scope-detail">
        <span>{formatCost(scope.used)} / {formatCost(scope.limit)}</span>
        <span>{scope.remaining > 0 ? `${formatCost(scope.remaining)} remaining` : 'Limit reached'}</span>
      </div>
    </div>
  )
}

function DesktopPanel() {
  const { data, isLoading, error, refetch } = useQuery<DesktopPayload>({
    queryKey: ['hermes-telemetry', 'desktop'],
    queryFn: async () => {
      const res = await ctx.rest('/desktop')
      return res as DesktopPayload
    },
    refetchInterval: 30_000,
  })

  if (isLoading) {
    return (
      <div className="hermes-telemetry-loading">
        <Loader type="lemniscate-bloom" />
      </div>
    )
  }

  if (error) {
    return (
      <ErrorState
        title="Telemetry unavailable"
        message={error instanceof Error ? error.message : 'Failed to load telemetry data'}
        action={{ label: 'Retry', onClick: () => refetch() }}
      />
    )
  }

  if (!data) {
    return <EmptyState icon={icons.Activity} message="No telemetry data yet" />
  }

  const budgetScopes = data.budget ? Object.entries(data.budget) : []

  return (
    <div className="hermes-telemetry-panel">
      {/* Last run status */}
      {data.last_run && (
        <div className="hermes-telemetry-last-run">
          <div className="hermes-telemetry-section-label">Last run</div>
          <div className="hermes-telemetry-last-run-details">
            <span className="hermes-telemetry-last-run-status" data-status={data.last_run.status}>
              {data.last_run.status}
            </span>
            <span className="hermes-telemetry-meta">
              {data.last_run.platform} &middot; {relativeTime(new Date(data.last_run.timestamp))}
            </span>
          </div>
          {data.last_run.cost > 0 && (
            <div className="hermes-telemetry-cost">{formatCost(data.last_run.cost)}</div>
          )}
        </div>
      )}

      <Separator />

      {/* Session counts */}
      <div className="hermes-telemetry-counts">
        <div className="hermes-telemetry-stat">
          <span className="hermes-telemetry-stat-value">{data.session_count}</span>
          <span className="hermes-telemetry-stat-label">Sessions</span>
        </div>
        {data.running_count > 0 && (
          <div className="hermes-telemetry-stat hermes-telemetry-stat-running">
            <span className="hermes-telemetry-stat-value">{data.running_count}</span>
            <span className="hermes-telemetry-stat-label">Running</span>
          </div>
        )}
      </div>

      <Separator />

      {/* Month to date */}
      <div className="hermes-telemetry-section-label">Month to date</div>
      <div className="hermes-telemetry-mtd-grid">
        <div className="hermes-telemetry-mtd-item">
          <span className="hermes-telemetry-mtd-value">{formatCost(data.month_to_date.cost)}</span>
          <span className="hermes-telemetry-mtd-label">Cost</span>
        </div>
        <div className="hermes-telemetry-mtd-item">
          <span className="hermes-telemetry-mtd-value">{formatTokens(data.month_to_date.tokens_in)}</span>
          <span className="hermes-telemetry-mtd-label">Tokens in</span>
        </div>
        <div className="hermes-telemetry-mtd-item">
          <span className="hermes-telemetry-mtd-value">{formatTokens(data.month_to_date.tokens_out)}</span>
          <span className="hermes-telemetry-mtd-label">Tokens out</span>
        </div>
      </div>

      {/* Budget scopes */}
      {budgetScopes.length > 0 && (
        <>
          <Separator />
          <div className="hermes-telemetry-section-label">Budgets</div>
          {budgetScopes.map(([name, scope]) => (
            <BudgetScope key={name} name={name} scope={scope} />
          ))}
        </>
      )}

      {/* Quick actions */}
      <Separator />
      <div className="hermes-telemetry-actions">
        <Button
          variant="secondary"
          size="sm"
          onClick={async () => {
            try {
              const result = await ctx.rest('/desktop/open-dashboard') as { url: string }
              host.navigate(result.url)
            } catch {
              host.notifyError('Could not open dashboard')
            }
          }}
        >
          <icons.LayoutDashboard /> Open Dashboard
        </Button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Plugin                                                              */
/* ------------------------------------------------------------------ */

const plugin: HermesPlugin = {
  id: 'hermes-telemetry',
  name: 'Telemetry',
  defaultEnabled: true,

  register(ctx: PluginContext) {
    ctx.register({
      id: 'telemetry',
      area: PANES_AREA,
      title: 'Telemetry',
      order: 50,
      render: () => <DesktopPanel />,
    } satisfies PluginContribution)
  },
}

export default plugin
