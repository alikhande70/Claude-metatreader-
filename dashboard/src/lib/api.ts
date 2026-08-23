/* Typed client for the ATLAS API.
 *
 * Every value the API cannot know arrives as a `Stamped` with `known: false` (ADR-011). The
 * types make that explicit so a component cannot accidentally render an unknown as a zero.
 */

export interface Stamped<T = unknown> {
  value: T | null
  seq: number
  ts: number
  known: boolean
}

export interface Health {
  version: string
  attached: boolean
  run_dir: string
  run_available: boolean
  run_id: string
  engine_state: Stamped<string>
  mode: Stamped<string>
  venue: Stamped<Record<string, unknown>>
  heartbeat: Stamped<Record<string, unknown>>
  reconcile: Stamped<Record<string, unknown>>
  kill_switch: Stamped<Record<string, unknown>>
  last_seq: number
  last_event_ts: number | null
  staleness_ms: number | null
  stale: boolean | null
  server_time_ms: number
}

export interface Metrics {
  trades: number; wins: number; losses: number; win_rate: number
  expectancy_r: number; expectancy_money: number; total_r: number
  net_profit: number; gross_profit: number; gross_loss: number
  profit_factor: number; payoff_ratio: number
  avg_win_r: number; avg_loss_r: number; largest_win_r: number; largest_loss_r: number
  max_drawdown_pct: number; max_drawdown_money: number; max_drawdown_r: number
  longest_losing_streak: number; longest_flat_trades: number
  sharpe: number; sortino: number; calmar: number; sqn: number; recovery_factor: number
  total_commission: number; total_swap: number; cost_share_of_gross: number
  avg_mae_r: number; avg_mfe_r: number
  exit_reasons: Record<string, number>
  caveats: string[]
}

export interface RiskState {
  halted: boolean; halt_reason: string; halt_detail: string
  equity_hwm: number; day_key: string; day_start_equity: number
  consecutive_losses: number; trades_today: number; realized_today: number
  daily_loss_pct: number; drawdown_pct: number; losses_to_daily_breach: number
}

export interface Overview {
  account: Stamped<Record<string, number>>
  positions: Record<string, unknown>[]
  open_position_count: number
  metrics: Metrics
  risk: RiskState | null
  risk_config: Record<string, unknown> | null
  started: Stamped<Record<string, unknown>>
  stopped: Stamped<Record<string, unknown>>
  errors: Record<string, unknown>[]
}

export interface Gate {
  name: string; passed: boolean
  value: number | null; threshold: number | null
  comparison: string; detail: string; hard: boolean
}

export interface Evidence {
  name: string; score: number; weight: number; detail: string
}

export interface Proposal {
  side: string; entry_price: number; stop_loss: number
  take_profit: number | null; stop_points: number
  reward_risk: number | null; rationale: string
}

export interface OutcomeLink {
  decision_id: string; trade_id: string; r_multiple: number; net_profit: number
  mae_points: number; mfe_points: number; exit_reason: string; holding_ms: number
}

export interface Decision {
  decision_id: string; ts: number; symbol: string; strategy: string
  strategy_version: string; outcome: string; reason_code: string; reason_detail: string
  regime: string; bias: string | null; conviction: number
  features: Record<string, number>
  gates: Gate[]; evidence: Evidence[]
  proposal: Proposal | null
  risk_notes: string[]; sized_volume: number | null; client_order_id: string | null
  outcome_link?: OutcomeLink | null
}

export interface Trade {
  trade_id: string; decision_id: string; symbol: string; side: string
  volume: number; entry_price: number; entry_time: number
  exit_price: number; exit_time: number; initial_stop: number
  risk_money: number; gross_profit: number; commission: number; swap: number
  exit_reason: string; strategy: string
  r_multiple: number; net_profit: number; mae_r: number; mfe_r: number; duration_ms: number
}

export interface EquityPoint { ts: number; equity: number; balance: number }

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    const body = await response.text().catch(() => '')
    throw new Error(`${response.status} ${response.statusText}${body ? `: ${body}` : ''}`)
  }
  return response.json() as Promise<T>
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const text = await response.text()
  if (!response.ok) {
    let detail = text
    try { detail = (JSON.parse(text) as { detail?: string }).detail ?? text } catch { /* raw */ }
    throw new Error(detail)
  }
  return (text ? JSON.parse(text) : {}) as T
}

export const api = {
  health: () => get<Health>('/api/health'),
  overview: () => get<Overview>('/api/overview'),
  equity: (points = 1500) => get<{ points: EquityPoint[]; count: number }>(
    `/api/equity?points=${points}`),
  trades: (limit = 500) => get<Trade[]>(`/api/trades?limit=${limit}`),
  decisions: (params: { limit?: number; outcome?: string; reason?: string; symbol?: string }) => {
    const q = new URLSearchParams()
    if (params.limit) q.set('limit', String(params.limit))
    if (params.outcome) q.set('outcome', params.outcome)
    if (params.reason) q.set('reason', params.reason)
    if (params.symbol) q.set('symbol', params.symbol)
    return get<Decision[]>(`/api/decisions?${q.toString()}`)
  },
  decision: (id: string) => get<Decision>(`/api/decisions/${id}`),
  funnel: () => get<{ reason: string; count: number; share: number }[]>('/api/funnel'),
  performance: () => get<{
    metrics: Metrics; caveats: string[]
    conviction_vs_outcome: { bucket: string; trades: number; mean_r: number }[]
  }>('/api/performance'),
  orders: (limit = 100) => get<Record<string, unknown>[]>(`/api/orders?limit=${limit}`),
  events: (since = 0, limit = 200, criticalOnly = false) => get<{
    seq: number; ts: number; kind: string; stream: string; payload: Record<string, unknown>
  }[]>(`/api/events?since=${since}&limit=${limit}&critical_only=${criticalOnly}`),
  config: () => get<Record<string, unknown>>('/api/config'),
  runs: () => get<{ name: string; path: string; modified_ms: number }[]>('/api/runs'),
  halt: (reason: string) => post<{ halted: boolean }>('/api/control/halt', { reason }),
  resume: (operator: string) => post<{ resumed: boolean }>('/api/control/resume', { operator }),
}
