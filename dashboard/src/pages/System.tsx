import { useState } from 'react'
import { Card, Empty, ErrorNote, Freshness, Pill } from '../components/common'
import { type Health, api } from '../lib/api'
import { age, num, ts } from '../lib/format'
import { usePoll, useEventStream } from '../lib/useLive'

/** System health, reconciliation and the raw event log.
 *
 * The event log is the system's actual record. Everything else on this dashboard is a
 * projection of it, so being able to read it directly is what makes the projections
 * trustworthy rather than merely plausible.
 */
export function System({ health, now }: { health: Health | null; now: number }) {
  const [criticalOnly, setCriticalOnly] = useState(true)
  const { events, connected } = useEventStream(undefined, 300)
  const { data: journal, error } = usePoll(
    () => api.events(Math.max(0, (health?.last_seq ?? 0) - 400), 400, criticalOnly),
    6000,
  )
  const { data: runs } = usePoll(api.runs, 30000)

  const combined = journal ?? []

  return (
    <div className="stack">
      <ErrorNote error={error} />

      <div className="grid cols-2">
        <Card title="Connectivity">
          <table>
            <tbody>
              <tr>
                <td className="muted">event stream</td>
                <td>
                  <Pill status={connected ? 'good' : 'warning'}>
                    {connected ? 'live' : 'polling only'}
                  </Pill>
                  <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>
                    {events.length} events received this session
                  </span>
                </td>
              </tr>
              <tr>
                <td className="muted">venue</td>
                <td>
                  {health?.venue.known ? (
                    <>
                      <span className="mono">
                        {String((health.venue.value as Record<string, unknown>)?.kind ?? '')}
                      </span>
                      <div className="muted" style={{ fontSize: 12 }}>
                        {String((health.venue.value as Record<string, unknown>)?.detail ?? '')}
                      </div>
                      <Freshness stamped={health.venue} now={now} />
                    </>
                  ) : <span className="muted">never observed</span>}
                </td>
              </tr>
              <tr>
                <td className="muted">heartbeat</td>
                <td>
                  {health?.heartbeat.known
                    ? <><span className="mono">{now - health.heartbeat.ts > 7 * 86400000
                        ? ts(health.heartbeat.ts) : age(now - health.heartbeat.ts)}</span>
                        <div className="muted" style={{ fontSize: 12 }}>
                          {JSON.stringify(health.heartbeat.value)}
                        </div></>
                    : <span className="muted">never observed</span>}
                </td>
              </tr>
              <tr>
                <td className="muted">journal</td>
                <td className="mono">
                  seq {health?.last_seq ?? 0}
                  {health?.staleness_ms !== null && health?.staleness_ms !== undefined && (
                    <span className="muted"> · newest event {age(health.staleness_ms)}</span>
                  )}
                </td>
              </tr>
            </tbody>
          </table>
        </Card>

        <Card title="Reconciliation"
              sub="Comparing our belief about open positions with the broker's. The broker
                   always wins; a divergence that cannot be repaired halts trading.">
          {health?.reconcile.known ? (
            <>
              <Pill status={String((health.reconcile.value as Record<string, unknown>)?.kind)
                .includes('divergence') ? 'serious' : 'good'}>
                {String((health.reconcile.value as Record<string, unknown>)?.kind ?? '')}
              </Pill>
              <pre className="mono" style={{ fontSize: 11.5, marginTop: 8, whiteSpace: 'pre-wrap' }}>
                {JSON.stringify(health.reconcile.value, null, 2)}
              </pre>
              <Freshness stamped={health.reconcile} now={now} />
            </>
          ) : <Empty>no reconciliation has run yet</Empty>}
        </Card>
      </div>

      <Card title="Event journal"
            sub="The source of truth. Every other panel is derived from these records."
            actions={
              <label style={{ fontSize: 12.5 }}>
                <input type="checkbox" checked={criticalOnly}
                       onChange={(e) => setCriticalOnly(e.target.checked)} />{' '}
                money and safety events only
              </label>
            }>
        {combined.length ? (
          <div className="scroll-x" style={{ maxHeight: 460, overflowY: 'auto' }}>
            <table>
              <thead>
                <tr><th className="num">seq</th><th>time</th><th>kind</th>
                    <th>stream</th><th>payload</th></tr>
              </thead>
              <tbody>
                {combined.slice().reverse().map((e) => (
                  <tr key={e.seq}>
                    <td className="num muted">{e.seq}</td>
                    <td className="mono muted">{ts(e.ts)}</td>
                    <td className="mono">{e.kind}</td>
                    <td className="muted">{e.stream}</td>
                    <td className="mono" style={{ fontSize: 11, maxWidth: 620,
                                                  overflow: 'hidden',
                                                  textOverflow: 'ellipsis',
                                                  whiteSpace: 'nowrap' }}
                        title={JSON.stringify(e.payload)}>
                      {JSON.stringify(e.payload)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>no events</Empty>}
      </Card>

      <Card title="Runs" sub="Every journal directory found under the configured run root.">
        {runs?.length ? (
          <table>
            <thead><tr><th>name</th><th>modified</th><th>path</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.path}>
                  <td className="mono">{r.name}</td>
                  <td className="mono muted">{ts(r.modified_ms)}</td>
                  <td className="muted" style={{ fontSize: 11.5 }}>{r.path}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <Empty>no runs found</Empty>}
      </Card>

      <Card title="Live stream" sub={`last ${Math.min(events.length, 40)} events pushed`}>
        {events.length ? (
          <div className="scroll-x" style={{ maxHeight: 260, overflowY: 'auto' }}>
            <table>
              <tbody>
                {events.slice(-40).reverse().map((e, i) => (
                  <tr key={`${e.seq}-${i}`}>
                    <td className="num muted">{e.seq}</td>
                    <td className="mono">{e.kind}</td>
                    <td className="mono muted" style={{ fontSize: 11 }}>
                      {JSON.stringify(e.payload).slice(0, 120)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>nothing pushed yet — the engine may be idle or not attached</Empty>}
      </Card>

      {health && (
        <Card title="Build">
          <table>
            <tbody>
              <tr><td className="muted">ATLAS version</td>
                  <td className="mono">{health.version}</td></tr>
              <tr><td className="muted">run id</td>
                  <td className="mono">{health.run_id || '—'}</td></tr>
              <tr><td className="muted">run directory</td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{health.run_dir}</td></tr>
              <tr><td className="muted">attached to engine</td>
                  <td className="mono">{String(health.attached)}</td></tr>
              <tr><td className="muted">clock skew (browser vs API)</td>
                  <td className="mono">{num((now - health.server_time_ms) / 1000, 1)} s</td></tr>
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}
