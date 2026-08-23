/* SVG chart primitives.
 *
 * Hand-rolled rather than pulled from a library, for three reasons: the mark specs are a
 * short list and easy to follow exactly, the colour roles come from CSS custom properties so
 * light/dark and theming work with no JS, and it removes a dependency from a page that an
 * operator may be looking at during an incident.
 *
 * Every chart here ships a hover layer by default. A chart that cannot be interrogated is a
 * picture, and an operator needs to read values off it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

interface TipState { x: number; y: number; text: string }

function useTooltip() {
  const [tip, setTip] = useState<TipState | null>(null)
  const show = useCallback((e: { clientX: number; clientY: number }, text: string) => {
    setTip({ x: e.clientX, y: e.clientY, text })
  }, [])
  const hide = useCallback(() => setTip(null), [])
  const node = tip ? (
    <div className="tooltip" style={{ left: tip.x + 12, top: tip.y + 12 }}>{tip.text}</div>
  ) : null
  return { show, hide, node }
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(600)
  useEffect(() => {
    if (!ref.current) return
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w && w > 0) setWidth(w)
    })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [])
  return { ref, width }
}

function niceTicks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min]
  const span = max - min
  const raw = span / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10
  const out: number[] = []
  for (let v = Math.ceil(min / step) * step; v <= max + step / 2; v += step) out.push(v)
  return out
}

// ---------------------------------------------------------------------------------------

export interface Series {
  name: string
  color: string
  points: { x: number; y: number }[]
}

/** Time-series line chart.
 *
 * One y-axis, always. Two measures of different scale get two charts -- never a second
 * y-scale, which is the single most misleading thing a chart can do.
 */
export function LineChart({
  series, height = 220, yLabel = '', xFormat, yFormat, baseline,
}: {
  series: Series[]
  height?: number
  yLabel?: string
  xFormat: (x: number) => string
  yFormat: (y: number) => string
  baseline?: number
}) {
  const { ref, width } = useWidth<HTMLDivElement>()
  const { show, hide, node } = useTooltip()
  const [hover, setHover] = useState<number | null>(null)

  const all = series.flatMap((s) => s.points)
  if (!all.length) return <div className="empty">no data yet</div>

  const pad = { top: 10, right: 14, bottom: 24, left: 62 }
  const w = Math.max(240, width)
  const innerW = w - pad.left - pad.right
  const innerH = height - pad.top - pad.bottom

  const xs = all.map((p) => p.x)
  const ys = all.map((p) => p.y)
  const x0 = Math.min(...xs)
  const x1 = Math.max(...xs)
  let y0 = Math.min(...ys)
  let y1 = Math.max(...ys)
  if (baseline !== undefined) { y0 = Math.min(y0, baseline); y1 = Math.max(y1, baseline) }
  const padY = (y1 - y0) * 0.08 || Math.abs(y1) * 0.02 || 1
  y0 -= padY; y1 += padY

  const sx = (x: number) => pad.left + (x1 === x0 ? innerW / 2 : ((x - x0) / (x1 - x0)) * innerW)
  const sy = (y: number) => pad.top + innerH - (y1 === y0 ? innerH / 2 : ((y - y0) / (y1 - y0)) * innerH)

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left
    if (px < pad.left || px > w - pad.right) { setHover(null); hide(); return }
    const value = x0 + ((px - pad.left) / innerW) * (x1 - x0)
    setHover(value)
    const lines = series.map((s) => {
      let best = s.points[0]
      for (const p of s.points) {
        if (Math.abs(p.x - value) < Math.abs(best.x - value)) best = p
      }
      return `${s.name}: ${yFormat(best.y)}`
    })
    const nearest = series[0].points.reduce((a, b) =>
      Math.abs(b.x - value) < Math.abs(a.x - value) ? b : a)
    show(e, [xFormat(nearest.x), ...lines].join('\n'))
  }

  return (
    <div ref={ref}>
      {series.length >= 2 && (
        <div className="legend">
          {series.map((s) => (
            <span className="item" key={s.name}>
              <span className="swatch" style={{ background: s.color }} />{s.name}
            </span>
          ))}
        </div>
      )}
      <svg className="chart" viewBox={`0 0 ${w} ${height}`} height={height}
           onMouseMove={onMove} onMouseLeave={() => { setHover(null); hide() }}
           role="img" aria-label={yLabel || series.map((s) => s.name).join(', ')}>
        {niceTicks(y0, y1).map((t) => (
          <g key={t}>
            <line className="gridline" x1={pad.left} x2={w - pad.right} y1={sy(t)} y2={sy(t)} />
            <text x={pad.left - 7} y={sy(t) + 3.5} textAnchor="end">{yFormat(t)}</text>
          </g>
        ))}
        {baseline !== undefined && (
          <line className="axis" strokeDasharray="3 3"
                x1={pad.left} x2={w - pad.right} y1={sy(baseline)} y2={sy(baseline)} />
        )}
        <line className="axis" x1={pad.left} x2={w - pad.right}
              y1={pad.top + innerH} y2={pad.top + innerH} />
        {[x0, (x0 + x1) / 2, x1].map((t, i) => (
          <text key={i} x={sx(t)} y={height - 6}
                textAnchor={i === 0 ? 'start' : i === 2 ? 'end' : 'middle'}>{xFormat(t)}</text>
        ))}
        {series.map((s) => (
          <path key={s.name} className="series-line" stroke={s.color}
                d={s.points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x)},${sy(p.y)}`).join(' ')} />
        ))}
        {hover !== null && (
          <line className="axis" x1={sx(hover)} x2={sx(hover)}
                y1={pad.top} y2={pad.top + innerH} strokeDasharray="2 3" />
        )}
      </svg>
      {node}
    </div>
  )
}

// ---------------------------------------------------------------------------------------

/** Horizontal bar chart for ranked magnitudes (the decision funnel).
 *
 * One hue: this encodes magnitude, not identity, so giving each row its own colour would
 * imply a categorical distinction that is not there.
 */
export function RankedBars({
  rows, valueFormat, height = 26, color = 'var(--series-1)', emphasise,
}: {
  rows: { label: string; value: number; note?: string }[]
  valueFormat: (v: number) => string
  height?: number
  color?: string
  emphasise?: (label: string) => string | undefined
}) {
  const { show, hide, node } = useTooltip()
  if (!rows.length) return <div className="empty">no data yet</div>
  const max = Math.max(...rows.map((r) => r.value)) || 1

  return (
    <div>
      {rows.map((r) => (
        <div key={r.label} style={{ display: 'grid', gridTemplateColumns: '190px 1fr 84px',
                                    gap: 10, alignItems: 'center', height }}
             onMouseMove={(e) => show(e, `${r.label}\n${valueFormat(r.value)}${
               r.note ? `\n${r.note}` : ''}`)}
             onMouseLeave={hide}>
          <span className="mono" style={{ fontSize: 11.5, overflow: 'hidden',
                                          textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {r.label}
          </span>
          <div style={{ background: 'var(--surface-2)', borderRadius: 4, height: 12 }}>
            <div style={{
              width: `${Math.max(1.5, (r.value / max) * 100)}%`, height: '100%',
              background: emphasise?.(r.label) ?? color, borderRadius: 4,
            }} />
          </div>
          <span className="mono muted" style={{ fontSize: 11.5, textAlign: 'right' }}>
            {valueFormat(r.value)}
          </span>
        </div>
      ))}
      {node}
    </div>
  )
}

// ---------------------------------------------------------------------------------------

/** Histogram of R multiples.
 *
 * Diverging by sign: losses take the red pole, wins the blue one, with zero as the neutral
 * boundary. Sign is the polarity the reader cares about, and it is the one thing the colour
 * should encode.
 */
export function RHistogram({ values, height = 200, bins = 21 }: {
  values: number[]; height?: number; bins?: number
}) {
  const { ref, width } = useWidth<HTMLDivElement>()
  const { show, hide, node } = useTooltip()
  if (!values.length) return <div className="empty">no trades yet</div>

  const lo = Math.min(-1.2, Math.floor(Math.min(...values) * 2) / 2)
  const hi = Math.max(1.2, Math.ceil(Math.max(...values) * 2) / 2)
  const step = (hi - lo) / bins
  const counts = new Array<number>(bins).fill(0)
  for (const v of values) {
    const i = Math.min(bins - 1, Math.max(0, Math.floor((v - lo) / step)))
    counts[i] += 1
  }
  const maxCount = Math.max(...counts) || 1

  const pad = { top: 8, right: 10, bottom: 26, left: 34 }
  const w = Math.max(240, width)
  const innerW = w - pad.left - pad.right
  const innerH = height - pad.top - pad.bottom
  const barW = innerW / bins

  return (
    <div ref={ref}>
      <div className="legend">
        <span className="item"><span className="swatch"
          style={{ background: 'var(--neg)', height: 9 }} />losing trades</span>
        <span className="item"><span className="swatch"
          style={{ background: 'var(--pos)', height: 9 }} />winning trades</span>
      </div>
      <svg className="chart" viewBox={`0 0 ${w} ${height}`} height={height}
           role="img" aria-label="distribution of trade outcomes in R">
        {niceTicks(0, maxCount, 3).map((t) => (
          <g key={t}>
            <line className="gridline" x1={pad.left} x2={w - pad.right}
                  y1={pad.top + innerH - (t / maxCount) * innerH}
                  y2={pad.top + innerH - (t / maxCount) * innerH} />
            <text x={pad.left - 6} y={pad.top + innerH - (t / maxCount) * innerH + 3.5}
                  textAnchor="end">{t}</text>
          </g>
        ))}
        {counts.map((c, i) => {
          const binLo = lo + i * step
          const h = (c / maxCount) * innerH
          // 2px surface gap between adjacent fills so bars read as separate marks.
          return (
            <rect key={i} x={pad.left + i * barW + 1} width={Math.max(1, barW - 2)}
                  y={pad.top + innerH - h} height={Math.max(c ? 2 : 0, h)}
                  rx={3}
                  fill={binLo + step / 2 < 0 ? 'var(--neg)' : 'var(--pos)'}
                  onMouseMove={(e) => show(e,
                    `${binLo.toFixed(2)}R to ${(binLo + step).toFixed(2)}R\n${c} trade${
                      c === 1 ? '' : 's'}`)}
                  onMouseLeave={hide} />
          )
        })}
        <line className="axis" x1={pad.left} x2={w - pad.right}
              y1={pad.top + innerH} y2={pad.top + innerH} />
        {(() => {
          const zeroX = pad.left + ((0 - lo) / (hi - lo)) * innerW
          return <line className="axis" x1={zeroX} x2={zeroX} y1={pad.top} y2={pad.top + innerH} />
        })()}
        {[lo, 0, hi].map((t, i) => (
          <text key={i} x={pad.left + ((t - lo) / (hi - lo)) * innerW} y={height - 8}
                textAnchor={i === 0 ? 'start' : i === 2 ? 'end' : 'middle'}>
            {t.toFixed(1)}R
          </text>
        ))}
      </svg>
      {node}
    </div>
  )
}
