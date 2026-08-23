"""Dashboard API.

Serves projections derived from the event journal (ADR-011). Nothing here computes a
plausible value for something the journal does not contain: every response carries the
journal position it was derived from, and a value that is not known is ``null`` with
``known: false``, so the UI renders "unknown" rather than a confident zero.

Two operating modes, and the difference is visible to the client rather than implied:

* **attached** -- a live ``TradingEngine`` is in the same process. Operator controls work.
* **read-only** -- the API is pointed at a run directory after the fact. Controls return 409
  rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from atlas import __version__
from atlas.bus.events import CRITICAL_KINDS, Event
from atlas.core.enums import HaltReason
from atlas.store.projections import ProjectionStore, list_runs

#: A run whose newest event is older than this is shown as stale rather than current.
STALE_AFTER_MS = 120_000


class ControlRequest(BaseModel):
    reason: str = ""
    operator: str = "dashboard"


class EventHub:
    """Fan-out of journal events to WebSocket subscribers.

    Subscribers get bounded queues and are dropped when they fall behind. A dashboard that
    cannot keep up must never be able to slow down or stall the trading engine, so back
    pressure is resolved by disconnecting the viewer, not by blocking the producer.
    """

    def __init__(self, max_queue: int = 512) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue = max_queue
        self.dropped = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: Event) -> None:
        payload = {"seq": event.seq, "ts": event.ts, "kind": event.kind,
                   "stream": event.stream, "payload": event.payload}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self.dropped += 1
                self._subscribers.discard(q)


def create_app(settings, run_dir: Path | str, engine=None) -> FastAPI:
    run_path = Path(run_dir)
    store = ProjectionStore(run_path)
    hub = EventHub()
    app = FastAPI(title="ATLAS", version=__version__,
                  description="Dashboard API for the ATLAS trading system")
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"], allow_headers=["*"],
    )
    app.state.store = store
    app.state.hub = hub
    app.state.engine = engine
    app.state.settings = settings
    app.state.run_dir = run_path

    if engine is not None:
        engine.journal.add_listener(hub.publish)
        engine.journal.add_listener(store.apply)

    def now_ms() -> int:
        return int(time.time() * 1000)

    def refresh() -> None:
        # When attached to a live engine the store is updated by the journal listener, so a
        # re-read would double-count. Read-only mode polls instead.
        if engine is None:
            store.refresh()

    def require_engine():
        if engine is None:
            raise HTTPException(
                status_code=409,
                detail="this dashboard is attached to a run directory, not to a running "
                       "engine, so controls are unavailable",
            )
        return engine

    # -- status -------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        refresh()
        s = store.state
        stale = store.staleness_ms(now_ms())
        return {
            "version": __version__,
            "attached": engine is not None,
            "run_dir": str(run_path),
            "run_available": store.available,
            "run_id": s.run_id,
            "engine_state": s.engine_state.to_json(),
            "mode": s.mode.to_json(),
            "venue": s.venue.to_json(),
            "heartbeat": s.heartbeat.to_json(),
            "reconcile": s.reconcile.to_json(),
            "kill_switch": s.kill_switch.to_json(),
            "last_seq": s.last_seq,
            "last_event_ts": s.last_ts or None,
            "staleness_ms": stale,
            # Explicitly three-valued: fresh, stale, or never seen. A dashboard that
            # collapses "no data" into "stale" hides a different failure.
            "stale": None if stale is None else stale > STALE_AFTER_MS,
            "server_time_ms": now_ms(),
        }

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        refresh()
        s = store.state
        m = store.metrics(_starting_equity(settings))
        risk_state = None
        if engine is not None:
            rs = engine.risk.state
            risk_state = {
                "halted": rs.halted, "halt_reason": str(rs.halt_reason),
                "halt_detail": rs.halt_detail, "equity_hwm": rs.equity_hwm,
                "day_key": rs.day_key, "day_start_equity": rs.day_start_equity,
                "consecutive_losses": rs.consecutive_losses,
                "trades_today": rs.trades_today, "realized_today": rs.realized_today,
                "daily_loss_pct": rs.daily_loss_pct(rs.last_equity),
                "drawdown_pct": rs.drawdown_pct(rs.last_equity,
                                                trailing=engine.risk.cfg.trailing_drawdown),
                "losses_to_daily_breach":
                    engine.risk.cfg.consecutive_losses_to_daily_breach(),
            }
        return {
            "account": s.account.to_json(),
            "positions": list(s.positions.values()),
            "open_position_count": len(s.positions),
            "metrics": m.to_dict(),
            "risk": risk_state,
            "risk_config": settings.risk.model_dump() if settings else None,
            "started": s.started.to_json(),
            "stopped": s.stopped.to_json(),
            "errors": s.errors[-10:],
        }

    @app.get("/api/positions")
    def positions() -> list[dict[str, Any]]:
        refresh()
        return list(store.state.positions.values())

    @app.get("/api/equity")
    def equity(points: int = Query(2000, ge=10, le=50_000)) -> dict[str, Any]:
        refresh()
        curve = store.state.equity
        if len(curve) > points:
            step = len(curve) // points
            # Always keep the last point: the newest equity value is the one an operator is
            # actually looking at, and dropping it to fit a stride would misreport it.
            curve = [*curve[::step], curve[-1]]
        return {"points": [{"ts": t, "equity": e, "balance": b} for t, e, b in curve],
                "count": len(store.state.equity)}

    @app.get("/api/trades")
    def trades(limit: int = Query(200, ge=1, le=5000)) -> list[dict[str, Any]]:
        refresh()
        out = []
        for t in store.state.trades[-limit:]:
            d = t.model_dump(mode="json")
            d["r_multiple"] = t.r_multiple
            d["net_profit"] = t.net_profit
            d["mae_r"] = t.mae_r
            d["mfe_r"] = t.mfe_r
            d["duration_ms"] = t.duration_ms
            out.append(d)
        return out

    @app.get("/api/decisions")
    def decisions(
        limit: int = Query(100, ge=1, le=2000),
        outcome: str | None = None,
        reason: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        refresh()
        return store.recent_decisions(limit, outcome, reason, symbol)

    @app.get("/api/decisions/{decision_id}")
    def decision(decision_id: str) -> dict[str, Any]:
        refresh()
        found = store.decision(decision_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id}")
        return found

    @app.get("/api/funnel")
    def funnel() -> list[dict[str, Any]]:
        refresh()
        return store.funnel()

    @app.get("/api/performance")
    def performance() -> dict[str, Any]:
        refresh()
        m = store.metrics(_starting_equity(settings))
        by_conviction = []
        if store.state.trades:
            from atlas.analytics.report import conviction_vs_outcome

            by_conviction = [
                {"bucket": b, "trades": n, "mean_r": r}
                for b, n, r in conviction_vs_outcome(store.state.decisions,
                                                     store.state.trades)
            ]
        return {"metrics": m.to_dict(), "caveats": m.caveats,
                "conviction_vs_outcome": by_conviction}

    @app.get("/api/orders")
    def orders(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        refresh()
        return store.state.orders[-limit:]

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        if settings is None:
            return {}
        data = settings.model_dump(mode="json")
        # Never serve the bridge token to a browser.
        data.get("venue", {}).pop("token", None)
        return data

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        root = Path(settings.journal_dir) if settings else run_path.parent
        return list_runs(root)

    # -- controls ------------------------------------------------------------------

    @app.post("/api/control/halt")
    def halt(req: ControlRequest) -> dict[str, Any]:
        eng = require_engine()
        eng.command_halt(req.reason or f"halted from the dashboard by {req.operator}")
        return {"halted": True, "reason": str(eng.risk.state.halt_reason),
                "detail": eng.risk.state.halt_detail}

    @app.post("/api/control/resume")
    def resume(req: ControlRequest) -> dict[str, Any]:
        eng = require_engine()
        reason = eng.risk.state.halt_reason
        # A drawdown or reconciliation halt is not a notification to dismiss. Resuming one
        # from a dashboard button, with no record of who decided the underlying problem was
        # resolved, is exactly how a kill switch stops being one.
        if reason in (HaltReason.TOTAL_DRAWDOWN_LIMIT, HaltReason.RECONCILIATION_DIVERGENCE):
            raise HTTPException(
                status_code=409,
                detail=f"a {reason} halt cannot be cleared from the dashboard. Investigate "
                       f"the cause, then clear it deliberately with the CLI.",
            )
        applied = eng.command_resume(req.operator)
        return {"resumed": applied, "state": str(eng.state)}

    # -- streaming ------------------------------------------------------------------

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket, kinds: str | None = None) -> None:
        await ws.accept()
        wanted = set(kinds.split(",")) if kinds else None
        q = hub.subscribe()
        try:
            await ws.send_text(json.dumps({"type": "hello", "attached": engine is not None,
                                           "last_seq": store.state.last_seq}))
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                except TimeoutError:
                    # A periodic keepalive lets the client distinguish "quiet" from "dead".
                    await ws.send_text(json.dumps({"type": "keepalive",
                                                   "ts": now_ms(),
                                                   "last_seq": store.state.last_seq}))
                    continue
                if wanted and event["kind"] not in wanted:
                    continue
                await ws.send_text(json.dumps({"type": "event", **event}, default=str))
        except (WebSocketDisconnect, ConnectionError):
            pass
        finally:
            hub.unsubscribe(q)
            with contextlib.suppress(Exception):
                await ws.close()

    @app.get("/api/events")
    def events(since: int = 0, limit: int = Query(200, ge=1, le=2000),
               critical_only: bool = False) -> list[dict[str, Any]]:
        j = store._open()
        if j is None:
            return []
        kinds = list(CRITICAL_KINDS) if critical_only else None
        return [
            {"seq": e.seq, "ts": e.ts, "kind": e.kind, "stream": e.stream,
             "payload": e.payload}
            for e in j.read(since, kinds=kinds, limit=limit)
        ]

    # -- static dashboard --------------------------------------------------------------

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and (static_dir / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/{path:path}")
        def spa(path: str) -> FileResponse:
            # Containment is checked explicitly rather than relying on the router to have
            # normalised the path first. Starlette does normalise it today, so a `../`
            # traversal is already collapsed before it arrives -- but a route that serves
            # arbitrary files should not depend on a framework behaviour to be safe.
            root = static_dir.resolve()
            candidate = (root / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html")
    else:
        @app.get("/")
        def missing_ui() -> JSONResponse:
            return JSONResponse({
                "error": "the dashboard bundle is not built",
                "fix": "cd dashboard && npm ci && npm run build",
                "api": "the JSON API is available under /api",
            }, status_code=503)

    return app


def _starting_equity(settings) -> float:
    return float(getattr(getattr(settings, "venue", None), "starting_balance", 10_000.0))
