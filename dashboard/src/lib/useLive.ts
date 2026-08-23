import { useCallback, useEffect, useRef, useState } from 'react'

/** Poll an async loader on an interval, exposing loading and error state honestly.
 *
 * `error` is surfaced rather than swallowed: a dashboard that silently keeps showing the last
 * good value while the API is down is lying about how current it is. */
export function usePoll<T>(loader: () => Promise<T>, intervalMs = 3000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const refresh = useCallback(async () => {
    try {
      setData(await loaderRef.current())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let live = true
    const tick = async () => { if (live) await refresh() }
    void tick()
    const id = setInterval(tick, intervalMs)
    return () => { live = false; clearInterval(id) }
  }, [refresh, intervalMs])

  return { data, error, loading, refresh }
}

export interface LiveEvent {
  type: string; seq?: number; ts?: number; kind?: string
  stream?: string; payload?: Record<string, unknown>
}

/** Subscribe to the engine's event stream.
 *
 * Reconnects with backoff, and reports `connected` so the UI can say whether it is watching a
 * live stream or looking at a snapshot. */
export function useEventStream(kinds?: string[], keep = 200) {
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let socket: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    let closed = false
    let backoff = 1000

    const connect = () => {
      if (closed) return
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const query = kinds && kinds.length ? `?kinds=${kinds.join(',')}` : ''
      socket = new WebSocket(`${proto}://${window.location.host}/ws/events${query}`)
      socket.onopen = () => { setConnected(true); backoff = 1000 }
      socket.onclose = () => {
        setConnected(false)
        if (!closed) { timer = setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 15000) }
      }
      socket.onerror = () => socket?.close()
      socket.onmessage = (message) => {
        try {
          const parsed = JSON.parse(message.data as string) as LiveEvent
          if (parsed.type !== 'event') return
          setEvents((prev) => [...prev.slice(-(keep - 1)), parsed])
        } catch { /* a malformed frame must not break the stream */ }
      }
    }
    connect()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      socket?.close()
    }
  }, [kinds?.join(','), keep])

  return { events, connected }
}
