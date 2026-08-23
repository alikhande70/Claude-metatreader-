/* Formatting helpers.
 *
 * The `unknown` sentinel is deliberate and is used everywhere a value may be absent. Rendering
 * an unknown as "0.00" is the specific failure ADR-011 exists to prevent: it turns "we have no
 * data" into "the number is zero", which are opposite statements during an incident.
 */

export const UNKNOWN = '—'

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return UNKNOWN
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '-∞'
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

export function signed(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return UNKNOWN
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '-∞'
  return (v >= 0 ? '+' : '') + num(v, digits)
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return UNKNOWN
  return `${(v * 100).toFixed(digits)}%`
}

export function money(v: number | null | undefined, currency = ''): string {
  if (v === null || v === undefined || Number.isNaN(v)) return UNKNOWN
  return `${v >= 0 ? '' : '-'}${currency}${Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function ts(ms: number | null | undefined): string {
  if (!ms) return UNKNOWN
  return new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + 'Z'
}

export function timeOnly(ms: number | null | undefined): string {
  if (!ms) return UNKNOWN
  return new Date(ms).toISOString().slice(11, 19)
}

/** Pick an axis time format from the span being plotted.
 *
 * A ten-month backtest labelled with times of day produced axis ticks reading
 * 05:05 -> 20:32 -> 12:00, which looks like the axis is not sorted. The span decides the
 * unit. */
export function axisTimeFormatter(spanMs: number): (ms: number) => string {
  if (spanMs > 120 * 86400000) return (ms) => new Date(ms).toISOString().slice(0, 7)
  if (spanMs > 3 * 86400000) return (ms) => new Date(ms).toISOString().slice(5, 10)
  if (spanMs > 86400000) return (ms) => new Date(ms).toISOString().slice(5, 16).replace('T', ' ')
  return timeOnly
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return UNKNOWN
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

export function age(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return 'never'
  return duration(ms) + ' ago'
}

export type Status = 'good' | 'warning' | 'serious' | 'critical' | 'neutral' | 'unknown'

/** Status colours always ship with an icon and a label, never colour alone. */
export const STATUS_ICON: Record<Status, string> = {
  good: '●', warning: '▲', serious: '▲', critical: '✕', neutral: '○', unknown: '?',
}

export function engineStatus(state: string | null): Status {
  switch (state) {
    case 'RUNNING': return 'good'
    case 'STARTING': case 'STOPPING': return 'warning'
    case 'DEGRADED': return 'serious'
    case 'HALTED': case 'ERROR': return 'critical'
    case 'STOPPED': case 'IDLE': return 'neutral'
    default: return 'unknown'
  }
}
