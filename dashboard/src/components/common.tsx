import type { ReactNode } from 'react'
import type { Stamped } from '../lib/api'
import { STATUS_ICON, type Status, UNKNOWN, age, num, ts } from '../lib/format'

export function Card({ title, sub, children, actions }: {
  title?: string; sub?: string; children: ReactNode; actions?: ReactNode
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          {title && <h2>{title}</h2>}
          {actions}
        </div>
      )}
      {sub && <p className="sub">{sub}</p>}
      {children}
    </section>
  )
}

/** A hero number.
 *
 * `value === null` renders as "unknown" in muted type, never as zero. That distinction is the
 * whole point of ADR-011: during an incident, "no data" and "the number is zero" mean
 * opposite things.
 */
export function Tile({ label, value, tone, note, digits = 2, suffix = '', prefix = '' }: {
  label: string
  value: number | string | null | undefined
  tone?: 'pos' | 'neg' | 'auto'
  note?: string
  digits?: number
  suffix?: string
  prefix?: string
}) {
  const isUnknown = value === null || value === undefined ||
    (typeof value === 'number' && Number.isNaN(value))
  let cls = ''
  if (!isUnknown && tone === 'auto' && typeof value === 'number') {
    cls = value > 0 ? 'pos' : value < 0 ? 'neg' : ''
  } else if (!isUnknown && tone) {
    cls = tone === 'auto' ? '' : tone
  }
  const text = isUnknown
    ? UNKNOWN
    : typeof value === 'number' ? `${prefix}${num(value, digits)}${suffix}` : String(value)
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className={`value ${isUnknown ? 'unknown' : cls}`}>{text}</div>
      {note && <div className="note">{note}</div>}
    </div>
  )
}

/** Status colours never carry meaning alone: every pill has an icon and a text label. */
export function Pill({ status, children, title }: {
  status: Status; children: ReactNode; title?: string
}) {
  return (
    <span className={`pill ${status}`} title={title}>
      <span aria-hidden>{STATUS_ICON[status]}</span>
      {children}
    </span>
  )
}

/** Shows how old a projection is, so no panel can silently present stale data as current. */
export function Freshness({ stamped, now }: { stamped: Stamped; now: number }) {
  if (!stamped.known) return <span className="muted" style={{ fontSize: 11 }}>never observed</span>
  const delta = now - stamped.ts
  // Beyond a week, "163d 6h ago" is technically true and useless -- it is almost always a
  // historical run whose journal carries simulated time. Show the absolute timestamp instead.
  const when = delta > 7 * 86400000 ? ts(stamped.ts) : age(delta)
  return (
    <span className="muted" style={{ fontSize: 11 }}>seq {stamped.seq} · {when}</span>
  )
}

export function Caveat({ children }: { children: ReactNode }) {
  return <div className="caveat">{children}</div>
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null
  return <div className="error">API error: {error}</div>
}
