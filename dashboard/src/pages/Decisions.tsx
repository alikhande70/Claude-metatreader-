import { useState } from 'react'
import { RankedBars } from '../components/charts'
import { Card, Empty, ErrorNote } from '../components/common'
import { type Decision, api } from '../lib/api'
import { num, pct, signed, ts } from '../lib/format'
import { usePoll } from '../lib/useLive'

const OUTCOMES = ['', 'SIGNAL', 'NO_SETUP', 'VETOED', 'SUPPRESSED']

/** The decision explorer.
 *
 * This is the page the whole audit design exists to make possible: it answers "why did the
 * system trade" and, just as importantly, "why did it not". The funnel shows what the system
 * spends its time rejecting, and each record decomposes into the gates and evidence that
 * produced it.
 */
export function Decisions() {
  const [outcome, setOutcome] = useState('')
  const [reason, setReason] = useState('')
  const [selected, setSelected] = useState<Decision | null>(null)
  const [limit, setLimit] = useState(100)
  const { data: funnel } = usePoll(api.funnel, 5000)
  const { data, error } = usePoll(
    () => api.decisions({ limit, outcome: outcome || undefined,
                          reason: reason || undefined }),
    4000,
  )

  return (
    <div className="stack">
      <ErrorNote error={error} />

      <Card title="Decision funnel"
            sub="Where evaluations stop. A system that stands aside almost always is working
                 as designed; what matters is whether the reason it stands aside is the one
                 you intended.">
        {funnel?.length ? (
          <RankedBars
            rows={funnel.map((f) => ({
              label: f.reason, value: f.count, note: pct(f.share),
            }))}
            valueFormat={(v) => String(v)}
            emphasise={(label) => label === 'SETUP_CONFIRMED' ? 'var(--series-3)' : undefined}
          />
        ) : <Empty>no decisions recorded yet</Empty>}
      </Card>

      {selected && <DecisionDetail decision={selected} onClose={() => setSelected(null)} />}

      <Card title="Decisions">
        <div className="row">
          <label>
            outcome{' '}
            <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
              {OUTCOMES.map((o) => <option key={o} value={o}>{o || 'all'}</option>)}
            </select>
          </label>
          <label>
            reason{' '}
            <select value={reason} onChange={(e) => setReason(e.target.value)}>
              <option value="">all</option>
              {(funnel ?? []).map((f) => (
                <option key={f.reason} value={f.reason}>{f.reason}</option>
              ))}
            </select>
          </label>
          <label>
            show{' '}
            <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              {[50, 100, 300, 1000].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <span className="muted">{data ? `${data.length} shown, newest first` : ''}</span>
        </div>

        {data?.length ? (
          <div className="scroll-x" style={{ maxHeight: 520, overflowY: 'auto' }}>
            <table>
              <thead style={{ position: 'sticky', top: 0, background: 'var(--surface-1)' }}>
                <tr>
                  <th>time</th><th>symbol</th><th>outcome</th><th>reason</th>
                  <th>regime</th><th className="num">conviction</th>
                  <th className="num">result</th><th />
                </tr>
              </thead>
              <tbody>
                {data.map((d) => (
                  <tr key={d.decision_id + d.ts}>
                    <td className="mono muted">{ts(d.ts)}</td>
                    <td>{d.symbol}</td>
                    <td>{d.outcome}</td>
                    <td className="mono">{d.reason_code}</td>
                    <td className="muted">{d.regime}</td>
                    <td className="num">
                      {d.outcome === 'SIGNAL' ? num(d.conviction, 2) : '—'}
                    </td>
                    <td className={`num ${d.outcome_link
                      ? (d.outcome_link.r_multiple >= 0 ? 'pos' : 'neg') : ''}`}>
                      {d.outcome_link ? `${signed(d.outcome_link.r_multiple)}R` : '—'}
                    </td>
                    <td>
                      <button className="action" onClick={() => setSelected(d)}>explain</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>no decisions match this filter</Empty>}
      </Card>

    </div>
  )
}

function DecisionDetail({ decision, onClose }: { decision: Decision; onClose: () => void }) {
  const totalWeight = decision.evidence.reduce((a, e) => a + e.weight, 0) || 1
  return (
    <Card title={`Decision ${decision.decision_id}`}
          sub={`${decision.strategy} v${decision.strategy_version} · ${ts(decision.ts)}`}
          actions={<button className="action" onClick={onClose}>close</button>}>
      <div className="grid cols-2">
        <div>
          <h2 style={{ marginTop: 0 }}>Gates</h2>
          <p className="sub">
            Every condition evaluated, with the numbers that decided it. A rejection is never
            anonymous.
          </p>
          {decision.gates.map((g, i) => (
            <div key={i} className={`gate ${g.passed ? 'pass' : 'fail'}`}>
              <span className="mark" aria-label={g.passed ? 'passed' : 'failed'}>
                {g.passed ? '✓' : '✕'}
              </span>
              <span>
                {g.name}
                {!g.hard && <span className="muted"> (soft)</span>}
                {g.detail && <div className="detail">{g.detail}</div>}
              </span>
              <span className="nums">
                {g.value !== null
                  ? `${num(g.value, 3)} ${g.comparison} ${g.threshold !== null
                      ? num(g.threshold, 3) : ''}`
                  : ''}
              </span>
            </div>
          ))}
        </div>

        <div>
          {decision.proposal && (
            <>
              <h2 style={{ marginTop: 0 }}>Proposal</h2>
              <table>
                <tbody>
                  <tr><td className="muted">side</td><td className="mono">
                    {decision.proposal.side}</td></tr>
                  <tr><td className="muted">entry</td><td className="mono">
                    {num(decision.proposal.entry_price, 5)}</td></tr>
                  <tr><td className="muted">stop</td><td className="mono">
                    {num(decision.proposal.stop_loss, 5)} ({num(
                      decision.proposal.stop_points, 0)} pts)</td></tr>
                  <tr><td className="muted">target</td><td className="mono">
                    {decision.proposal.take_profit
                      ? num(decision.proposal.take_profit, 5) : '—'}</td></tr>
                  <tr><td className="muted">sized volume</td><td className="mono">
                    {decision.sized_volume !== null
                      ? num(decision.sized_volume, 2) : 'not sized'}</td></tr>
                  <tr><td className="muted">rationale</td>
                      <td>{decision.proposal.rationale}</td></tr>
                </tbody>
              </table>
            </>
          )}

          {decision.evidence.length > 0 && (
            <>
              <h2>Evidence</h2>
              <p className="sub">
                Conviction {num(decision.conviction, 3)} decomposes exactly into these
                contributions. Conviction scales position size between half and full risk; it
                never gates a trade.
              </p>
              {decision.evidence.map((e) => (
                <div className="evidence-row" key={e.name} title={e.detail}>
                  <span className="mono" style={{ fontSize: 11.5 }}>{e.name}</span>
                  <span className="evidence-bar">
                    <i style={{
                      width: `${Math.max(0, Math.min(100,
                        (e.score * e.weight / totalWeight) * 100 * (totalWeight / e.weight)))}%`,
                      background: e.score >= 0 ? 'var(--series-1)' : 'var(--neg)',
                    }} />
                  </span>
                  <span className="mono muted" style={{ fontSize: 11.5, textAlign: 'right' }}>
                    {signed(e.score, 2)}×{num(e.weight, 1)}
                  </span>
                </div>
              ))}
            </>
          )}

          {decision.risk_notes.length > 0 && (
            <>
              <h2>Risk notes</h2>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
                {decision.risk_notes.map((n) => <li key={n}>{n}</li>)}
              </ul>
            </>
          )}

          {decision.outcome_link && (
            <>
              <h2>What happened</h2>
              <table>
                <tbody>
                  <tr><td className="muted">result</td>
                      <td className={decision.outcome_link.r_multiple >= 0 ? 'pos' : 'neg'}>
                        {signed(decision.outcome_link.r_multiple)}R</td></tr>
                  <tr><td className="muted">exit</td>
                      <td>{decision.outcome_link.exit_reason}</td></tr>
                  <tr><td className="muted">MAE / MFE</td>
                      <td className="mono">{num(decision.outcome_link.mae_points, 0)} /{' '}
                        {num(decision.outcome_link.mfe_points, 0)} pts</td></tr>
                </tbody>
              </table>
            </>
          )}
        </div>
      </div>

      <details style={{ marginTop: 12 }}>
        <summary className="muted" style={{ cursor: 'pointer', fontSize: 12.5 }}>
          feature snapshot ({Object.keys(decision.features).length} values)
        </summary>
        <div className="scroll-x">
          <table>
            <tbody>
              {Object.entries(decision.features).sort().map(([k, v]) => (
                <tr key={k}>
                  <td className="mono muted">{k}</td>
                  <td className="num">{num(v, 5)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </Card>
  )
}
