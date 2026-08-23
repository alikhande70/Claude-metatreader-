import { useEffect, useState } from 'react'
import { Pill } from './components/common'
import { api } from './lib/api'
import { UNKNOWN, age, engineStatus, num } from './lib/format'
import { usePoll } from './lib/useLive'
import { Decisions } from './pages/Decisions'
import { Overview } from './pages/Overview'
import { Performance } from './pages/Performance'
import { Risk } from './pages/Risk'
import { System } from './pages/System'

const TABS = ['Overview', 'Decisions', 'Performance', 'Risk', 'System'] as const
type Tab = (typeof TABS)[number]

export function App() {
  const [tab, setTab] = useState<Tab>('Overview')
  const [now, setNow] = useState(Date.now())
  const { data: health, error } = usePoll(api.health, 2000)

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const state = (health?.engine_state.value as string | null) ?? null
  const halted = Boolean((health?.kill_switch.value as Record<string, unknown>)?.reason
    && state === 'HALTED')

  return (
    <div className="app">
      <header className="statusbar">
        <span className="brand">ATLAS<span>{health?.run_id || 'no run'}</span></span>

        {/* Engine state is the first thing on screen because it is the first thing that
            matters. Status colour is always paired with an icon and a word. */}
        <Pill status={health ? engineStatus(state) : 'unknown'}
              title={health?.engine_state.known ? `seq ${health.engine_state.seq}`
                : 'no engine state has been observed'}>
          {state ?? 'unknown'}
        </Pill>

        <Pill status={halted ? 'critical' : health?.attached ? 'good' : 'neutral'}>
          {halted ? 'kill switch tripped' : health?.attached ? 'attached' : 'read-only'}
        </Pill>

        {health?.mode.known && <Pill status="neutral">{String(health.mode.value)}</Pill>}

        {/* Staleness is three-valued on purpose: fresh, stale, or never seen. Collapsing
            "no data" into "stale" would hide a different failure.

            In BACKTEST mode it is suppressed entirely: the journal carries SIMULATED time,
            so comparing it with the wall clock reported a ten-month-old backtest as "stale",
            which is true and useless. */}
        {health && (
          health.mode.value === 'BACKTEST'
            ? <Pill status="neutral">historical run</Pill>
            : health.staleness_ms === null
              ? <Pill status="unknown">no events yet</Pill>
              : health.stale
                ? <Pill status="serious">stale · {age(health.staleness_ms)}</Pill>
                : <span className="age">updated {age(health.staleness_ms)}</span>
        )}

        <span className="spacer" />
        {error && <span className="error">API unreachable: {error}</span>}
        <span className="age">
          seq {health?.last_seq ?? UNKNOWN} · skew{' '}
          {health ? `${num((now - health.server_time_ms) / 1000, 0)}s` : UNKNOWN}
        </span>
      </header>

      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>

      <main>
        {tab === 'Overview' && <Overview now={now} />}
        {tab === 'Decisions' && <Decisions />}
        {tab === 'Performance' && <Performance />}
        {tab === 'Risk' && <Risk attached={Boolean(health?.attached)} />}
        {tab === 'System' && <System health={health} now={now} />}
      </main>
    </div>
  )
}
