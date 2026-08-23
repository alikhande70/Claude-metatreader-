import { LineChart } from '../components/charts'
import { Caveat, Card, Empty, ErrorNote, Freshness, Tile } from '../components/common'
import { api } from '../lib/api'
import { axisTimeFormatter, money, num, pct, signed, ts } from '../lib/format'
import { usePoll } from '../lib/useLive'

export function Overview({ now }: { now: number }) {
  const { data, error } = usePoll(api.overview, 3000)
  const { data: equity } = usePoll(() => api.equity(1200), 5000)

  const m = data?.metrics
  const account = data?.account
  const acct = (account?.value ?? {}) as Record<string, number>
  const currency = String((data?.started.value as Record<string, unknown>)?.currency ?? '')

  return (
    <div className="stack">
      <ErrorNote error={error} />

      <div className="grid cols-4">
        <Tile label="Equity" value={account?.known ? acct.equity : null}
              note={account?.known ? `balance ${money(acct.balance)}` : 'no account snapshot yet'}
              prefix={currency} />
        <Tile label="Open positions" value={data ? data.open_position_count : null} digits={0}
              note={data?.risk ? `${num(data.risk.losses_to_daily_breach, 1)} full-size losses reach today's halt` : undefined} />
        <Tile label="Expectancy" value={m && m.trades ? m.expectancy_r : null} tone="auto"
              digits={3} suffix=" R"
              note={m ? `${m.trades} trades` : 'no trades yet'} />
        <Tile label="Max drawdown" value={m && m.trades ? m.max_drawdown_pct : null}
              tone="neg" digits={2} suffix="%"
              note={m && m.trades ? `${num(m.max_drawdown_r, 1)} R` : undefined} />
      </div>

      {m?.caveats?.map((c) => <Caveat key={c}>{c}</Caveat>)}

      <Card title="Equity curve"
            sub="Equity includes floating profit and loss; balance is realised only. Every
                 drawdown limit is measured on equity, so it is the line that matters.">
        {equity && equity.points.length > 1 ? (
          <LineChart
            height={260}
            baseline={equity.points[0].balance}
            xFormat={axisTimeFormatter(
              equity.points[equity.points.length - 1].ts - equity.points[0].ts)}
            yFormat={(y) => num(y, 0)}
            series={[
              { name: 'equity', color: 'var(--series-1)',
                points: equity.points.map((p) => ({ x: p.ts, y: p.equity })) },
              { name: 'balance', color: 'var(--series-2)',
                points: equity.points.map((p) => ({ x: p.ts, y: p.balance })) },
            ]}
          />
        ) : <Empty>no equity points recorded yet</Empty>}
      </Card>

      <div className="grid cols-2">
        <Card title="Open positions"
              actions={account && <Freshness stamped={account} now={now} />}>
          {data && data.positions.length ? (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr>
                    <th>ticket</th><th>symbol</th><th className="num">volume</th>
                    <th className="num">entry</th><th className="num">stop</th>
                    <th className="num">target</th><th>opened</th>
                  </tr>
                </thead>
                <tbody>
                  {data.positions.map((p) => (
                    <tr key={String(p.ticket)}>
                      <td className="mono">{String(p.ticket)}</td>
                      <td>{String(p.symbol ?? '')}</td>
                      <td className="num">{num(Number(p.volume), 2)}</td>
                      <td className="num">{num(Number(p.entry), 5)}</td>
                      <td className="num">{p.stop_loss == null ? '—'
                        : num(Number(p.stop_loss), 5)}</td>
                      <td className="num">{p.take_profit == null ? '—'
                        : num(Number(p.take_profit), 5)}</td>
                      <td className="mono muted">{ts(Number(p.opened_ts))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <Empty>no open positions</Empty>}
        </Card>

        <Card title="Session"
              sub="What this run was configured to do. Journalled at start, so it describes
                   the run you are looking at rather than the file on disk now.">
          {data?.started.known ? (
            <table>
              <tbody>
                {Object.entries((data.started.value ?? {}) as Record<string, unknown>)
                  .filter(([k]) => !['parameters', 'warmup_bars'].includes(k))
                  .map(([k, v]) => (
                    <tr key={k}>
                      <td className="muted">{k}</td>
                      <td className="mono" style={{ wordBreak: 'break-word' }}>
                        {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : <Empty>this run has not started</Empty>}
        </Card>
      </div>

      {m && m.trades > 0 && (
        <Card title="Cost accounting"
              sub="An edge that is not quoted net of costs is not an edge.">
          <div className="grid cols-4">
            <Tile label="Commission" value={m.total_commission} tone="neg" />
            <Tile label="Swap" value={m.total_swap} tone="auto" />
            <Tile label="Costs as share of gross" value={m.cost_share_of_gross * 100}
                  digits={1} suffix="%"
                  note={m.cost_share_of_gross > 0.3 ? 'cost-constrained' : undefined} />
            <Tile label="Net profit" value={m.net_profit} tone="auto" />
          </div>
        </Card>
      )}

      {data?.errors?.length ? (
        <Card title="Recent errors">
          <table>
            <tbody>
              {data.errors.map((e, i) => (
                <tr key={i}>
                  <td className="mono muted">{ts(Number(e.ts))}</td>
                  <td>{String(e.type ?? '')}</td>
                  <td>{String(e.error ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ) : null}

      {m && m.trades > 0 && (
        <Card title="Exit mix"
              sub="A distribution dominated by one reason is a finding: mostly stop-outs means
                   the stop is too tight for the target, mostly time stops means the trades
                   are not resolving inside their window.">
          <table>
            <tbody>
              {Object.entries(m.exit_reasons).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td className="num">{v}</td>
                  <td className="num muted">{pct(v / m.trades)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {m && m.trades > 0 && (
        <div className="grid cols-4">
          <Tile label="Win rate" value={m.win_rate * 100} digits={1} suffix="%" />
          <Tile label="Profit factor" value={m.profit_factor} digits={2} tone="auto"
                note="net of modelled costs" />
          <Tile label="Payoff" value={m.payoff_ratio} digits={2}
                note={`avg win ${signed(m.avg_win_r)}R / avg loss ${signed(m.avg_loss_r)}R`} />
          <Tile label="Longest losing streak" value={m.longest_losing_streak} digits={0}
                note={`flat for ${m.longest_flat_trades} trades`} />
        </div>
      )}
    </div>
  )
}
