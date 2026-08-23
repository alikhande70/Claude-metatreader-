import { useState } from 'react'
import { Card, Empty, ErrorNote, Pill, Tile } from '../components/common'
import { api } from '../lib/api'
import { money, num } from '../lib/format'
import { usePoll } from '../lib/useLive'

/** Risk and the kill switch.
 *
 * The controls here are deliberately asymmetric. Halting is one click, because in an incident
 * you want the stop to be the easy action. Resuming is not: a drawdown or reconciliation halt
 * cannot be cleared from a browser button at all, because a kill switch that is dismissed
 * like a notification has stopped being one.
 */
export function Risk({ attached }: { attached: boolean }) {
  const { data, error, refresh } = usePoll(api.overview, 3000)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const risk = data?.risk
  const cfg = (data?.risk_config ?? {}) as Record<string, number | boolean>

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setMessage(null)
    try {
      await fn()
      await refresh()
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      <ErrorNote error={error} />

      {risk?.halted && (
        <div className="banner">
          <strong>✕ Trading halted — {risk.halt_reason}</strong>
          <div style={{ marginTop: 4 }}>{risk.halt_detail}</div>
          <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
            Open positions keep their broker-side stops and continue to be managed. A halt
            survives a restart by design.
          </div>
        </div>
      )}

      <Card title="Controls"
            sub={attached
              ? 'This dashboard is attached to a running engine.'
              : 'Read-only: this dashboard is pointed at a run directory, not a live engine.'}>
        <div className="row">
          <button className="action danger" disabled={!attached || busy || risk?.halted}
                  onClick={() => act(() => api.halt('halted from the dashboard'))}>
            Halt trading
          </button>
          <button className="action" disabled={!attached || busy || !risk?.halted}
                  onClick={() => act(() => api.resume('dashboard'))}>
            Resume
          </button>
          {risk && (
            <Pill status={risk.halted ? 'critical' : 'good'}>
              {risk.halted ? 'halted' : 'armed'}
            </Pill>
          )}
        </div>
        {message && <div className="error">{message}</div>}
        <p className="sub" style={{ marginTop: 8, marginBottom: 0 }}>
          A total-drawdown or reconciliation halt cannot be cleared here. Those say something
          is wrong that time does not fix; investigate the cause and clear it deliberately
          from the CLI.
        </p>
      </Card>

      {risk ? (
        <>
          <div className="grid cols-4">
            <Tile label="Today's loss" value={risk.daily_loss_pct} digits={2} suffix="%"
                  tone={risk.daily_loss_pct > 0 ? 'neg' : undefined}
                  note={`halt at ${num(Number(cfg.daily_loss_limit_pct), 1)}%`} />
            <Tile label="Drawdown" value={risk.drawdown_pct} digits={2} suffix="%"
                  tone={risk.drawdown_pct > 0 ? 'neg' : undefined}
                  note={`${cfg.trailing_drawdown ? 'trailing from the high-water mark'
                    : 'from the initial balance'} · halt at ${
                    num(Number(cfg.total_drawdown_limit_pct), 1)}%`} />
            <Tile label="Losses to daily breach" value={risk.losses_to_daily_breach} digits={1}
                  note="at full size; a run of three is normal for a 40% win rate" />
            <Tile label="Consecutive losses" value={risk.consecutive_losses} digits={0}
                  note={`limit ${cfg.max_consecutive_losses}`} />
          </div>

          <div className="grid cols-2">
            <Card title="Session state">
              <table>
                <tbody>
                  <tr><td className="muted">trading day</td>
                      <td className="mono">{risk.day_key || '—'}</td></tr>
                  <tr><td className="muted">day start equity</td>
                      <td className="mono">{money(risk.day_start_equity)}</td></tr>
                  <tr><td className="muted">equity high-water mark</td>
                      <td className="mono">{money(risk.equity_hwm)}</td></tr>
                  <tr><td className="muted">realised today</td>
                      <td className={`mono ${risk.realized_today >= 0 ? 'pos' : 'neg'}`}>
                        {money(risk.realized_today)}</td></tr>
                  <tr><td className="muted">trades today</td>
                      <td className="mono">{risk.trades_today}</td></tr>
                </tbody>
              </table>
            </Card>

            <Card title="Limits"
                  sub="Every threshold is set inside whatever external limit applies. If a
                       prop firm's daily limit is 5%, halting at 5% breaches it — the last
                       percent goes to slippage, spread and the trade already open.">
              <table>
                <tbody>
                  {Object.entries(cfg)
                    .filter(([k]) => k !== 'correlation_groups')
                    .map(([k, v]) => (
                      <tr key={k}>
                        <td className="muted">{k}</td>
                        <td className="mono num">{typeof v === 'boolean' ? String(v)
                          : num(Number(v), 2)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </Card>
          </div>
        </>
      ) : (
        <Card title="Risk state">
          <Empty>
            risk state is only available when the dashboard is attached to a running engine
          </Empty>
        </Card>
      )}
    </div>
  )
}
