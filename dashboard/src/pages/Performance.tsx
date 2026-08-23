import { RHistogram, RankedBars } from '../components/charts'
import { Caveat, Card, Empty, ErrorNote, Tile } from '../components/common'
import { api } from '../lib/api'
import { duration, money, num, pct, signed, ts } from '../lib/format'
import { usePoll } from '../lib/useLive'

export function Performance() {
  const { data, error } = usePoll(api.performance, 5000)
  const { data: trades } = usePoll(() => api.trades(1000), 5000)
  const m = data?.metrics

  if (!m || m.trades === 0) {
    return (
      <div className="stack">
        <ErrorNote error={error} />
        <Card title="Performance">
          <Empty>no closed trades in this run yet</Empty>
        </Card>
      </div>
    )
  }

  return (
    <div className="stack">
      <ErrorNote error={error} />

      {data?.caveats.map((c) => <Caveat key={c}>{c}</Caveat>)}

      <div className="grid cols-4">
        <Tile label="Trades" value={m.trades} digits={0}
              note={`${m.wins} won, ${m.losses} lost`} />
        <Tile label="Expectancy" value={m.expectancy_r} digits={3} suffix=" R" tone="auto"
              note={`${money(m.expectancy_money)} per trade`} />
        <Tile label="Profit factor" value={m.profit_factor} digits={2} tone="auto" />
        <Tile label="SQN" value={m.sqn} digits={2} tone="auto"
              note="expectancy per unit of variability" />
      </div>

      <div className="grid cols-2">
        <Card title="Distribution of outcomes"
              sub="Expressed in R -- multiples of the risk actually taken at entry. Currency
                   profit is not comparable across position sizes; R is.">
          <RHistogram values={(trades ?? []).map((t) => t.r_multiple)} />
        </Card>

        <Card title="Does conviction predict anything?"
              sub="The conviction score is a hypothesis, and this is its test. If there is no
                   gradient here after a few hundred trades, the scoring layer is not earning
                   its parameters and should be deleted rather than re-weighted.">
          {/* Buckets with no trades are shown as such rather than as a zero-length bar --
              a row of identical empty bars reads as "conviction has no effect", which is a
              conclusion, not an absence of data. */}
          {(data?.conviction_vs_outcome ?? []).some((b) => b.trades > 0) ? (
            <table>
              <thead>
                <tr><th>conviction</th><th className="num">trades</th>
                    <th className="num">mean R</th></tr>
              </thead>
              <tbody>
                {data!.conviction_vs_outcome.map((b) => (
                  <tr key={b.bucket}>
                    <td className="mono">{b.bucket}</td>
                    <td className="num">{b.trades}</td>
                    <td className={`num ${b.trades === 0 ? 'muted'
                      : b.mean_r >= 0 ? 'pos' : 'neg'}`}>
                      {b.trades === 0 ? '—' : `${signed(b.mean_r, 3)}R`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>
              no trades are linked to a decision record in this run yet
            </Empty>
          )}
        </Card>
      </div>

      <div className="grid cols-4">
        <Tile label="Max drawdown" value={m.max_drawdown_pct} digits={2} suffix="%" tone="neg"
              note={`${num(m.max_drawdown_r, 1)} R · ${money(m.max_drawdown_money)}`} />
        <Tile label="Recovery factor" value={m.recovery_factor} digits={2}
              note="net profit per unit of drawdown" />
        <Tile label="Calmar" value={m.calmar} digits={2} note="total R per R of drawdown" />
        <Tile label="Sharpe" value={m.sharpe} digits={2}
              note="on the trade series, not annualised" />
      </div>

      <div className="grid cols-4">
        <Tile label="Avg MAE" value={m.avg_mae_r} digits={2} suffix=" R"
              note="how far trades went against before working" />
        <Tile label="Avg MFE" value={m.avg_mfe_r} digits={2} suffix=" R"
              note="how far they ran in favour" />
        <Tile label="Largest win" value={m.largest_win_r} digits={2} suffix=" R" tone="pos" />
        <Tile label="Largest loss" value={m.largest_loss_r} digits={2} suffix=" R" tone="neg"
              note={m.largest_loss_r < -1.2
                ? 'beyond 1R: slippage or a gap through the stop' : undefined} />
      </div>

      <Card title="Trades" sub={`${trades?.length ?? 0} most recent, newest first`}>
        <div className="scroll-x" style={{ maxHeight: 520, overflowY: 'auto' }}>
          <table>
            <thead style={{ position: 'sticky', top: 0, background: 'var(--surface-1)' }}>
              <tr>
                <th>opened</th><th>symbol</th><th>side</th><th className="num">volume</th>
                <th className="num">entry</th><th className="num">exit</th>
                <th>exit reason</th><th className="num">R</th><th className="num">net</th>
                <th className="num">held</th>
              </tr>
            </thead>
            <tbody>
              {(trades ?? []).slice().reverse().map((t) => (
                <tr key={t.trade_id}>
                  <td className="mono muted">{ts(t.entry_time)}</td>
                  <td>{t.symbol}</td>
                  <td>{t.side}</td>
                  <td className="num">{num(t.volume, 2)}</td>
                  <td className="num">{num(t.entry_price, 5)}</td>
                  <td className="num">{num(t.exit_price, 5)}</td>
                  <td className="muted">{t.exit_reason}</td>
                  <td className={`num ${t.r_multiple >= 0 ? 'pos' : 'neg'}`}>
                    {signed(t.r_multiple)}
                  </td>
                  <td className={`num ${t.net_profit >= 0 ? 'pos' : 'neg'}`}>
                    {money(t.net_profit)}
                  </td>
                  <td className="num muted">{duration(t.duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Exit reasons">
        <RankedBars
          rows={Object.entries(m.exit_reasons)
            .sort((a, b) => b[1] - a[1])
            .map(([k, v]) => ({ label: k, value: v, note: pct(v / m.trades) }))}
          valueFormat={(v) => String(v)}
        />
      </Card>
    </div>
  )
}
