"""A terminal-side implementation of the ATLAS bridge protocol, for conformance testing.

This is not a mock of the bridge -- it is a **second implementation of the protocol**, and it
is backed by the real ``SimulatedVenue`` matching engine. That means a conformance run
exercises the full path:

    BridgeVenue  ->  JSON frames over a real TCP socket  ->  fake terminal  ->  matching engine

so order semantics, retcodes, idempotency lookups and stops-level rejections all round-trip
through the wire format rather than being asserted against a hand-written stub.

What this verifies and what it does not: it verifies the **Python half of the bridge and the
protocol design**, on Linux, in CI. It cannot verify ``AtlasBridge.mq5``, which needs
MetaEditor and a real terminal. That boundary is stated in docs/PROTOCOL.md §8 and in the
project status matrix.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from atlas.core.enums import OrderType, Side, Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, Quote
from atlas.core.trading import OrderRequest
from atlas.venues.mt5 import protocol as p
from atlas.venues.sim.venue import SimulatedVenue


class FakeTerminal:
    """Connects out to ATLAS and serves the protocol from a SimulatedVenue."""

    def __init__(
        self,
        venue: SimulatedVenue,
        *,
        token: str = "",
        impl: str = "fake-terminal",
        version: int = p.PROTOCOL_VERSION,
        magic: int = 20260823,
        server_offset_seconds: int = 3 * 3600,
        fail_ops: set[str] | None = None,
        drop_reply_ops: set[str] | None = None,
        on_request: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.venue = venue
        self.token = token
        self.impl = impl
        self.version = version
        self.magic = magic
        self.server_offset_seconds = server_offset_seconds
        #: Ops that answer with a protocol error, for negative testing.
        self.fail_ops = fail_ops or set()
        #: Ops that are executed but whose reply is never sent -- the exact shape of the
        #: lost-response failure that idempotency exists to survive.
        self.drop_reply_ops = drop_reply_ops or set()
        self.on_request = on_request
        self.requests: list[tuple[str, dict]] = []
        self.welcomed = asyncio.Event()
        self.closed_by_server: str | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._now = 0

    # -- lifecycle -----------------------------------------------------------------

    async def connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.open_connection(host, port)
        await self._send_hello()
        self._task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    def advance(self, quote: Quote) -> None:
        """Feed the underlying matching engine, as a real market would."""
        self._now = max(self._now, quote.ts)
        self.venue.on_quote(quote)

    # -- protocol ------------------------------------------------------------------

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write(p.encode(message))
        await self._writer.drain()

    async def _send_hello(self) -> None:
        acct = self.venue.account_state()
        await self._send({
            "v": self.version, "t": "hello", "ts": self._now,
            "token": self.token, "impl": self.impl, "build": 4150,
            "server_time_ms": self._now + self.server_offset_seconds * 1000,
            "account": {
                "login": acct.login, "server": acct.server, "currency": acct.currency,
                "balance": acct.balance, "equity": acct.equity, "margin": acct.margin,
                "free_margin": acct.free_margin, "margin_level": acct.margin_level,
                "leverage": acct.leverage, "ts": acct.ts,
                "trade_allowed": acct.trade_allowed, "hedging": True,
            },
            "symbols": list(self.venue.specs),
        })

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                return
            msg = p.decode(line)
            kind = msg.get("t")
            if kind == "welcome":
                self.welcomed.set()
            elif kind == "bye":
                self.closed_by_server = str(msg.get("reason", ""))
                return
            elif kind == "ping":
                await self._send({"v": 1, "t": "pong", "ts": self._now, "id": msg.get("id")})
            elif kind == "req":
                await self._handle_request(msg)

    async def _handle_request(self, msg: dict[str, Any]) -> None:
        req_id = str(msg.get("id"))
        op = str(msg.get("op"))
        args = msg.get("args") or {}
        self.requests.append((op, args))
        if self.on_request is not None:
            self.on_request(op, args)

        if op in self.fail_ops:
            await self._send(p.reply_error(req_id, "SIMULATED_FAILURE",
                                           f"{op} was configured to fail", self._now))
            return
        try:
            data = await self._dispatch(op, args)
        except Exception as exc:
            await self._send(p.reply_error(req_id, "TERMINAL_ERROR", str(exc), self._now))
            return

        if op in self.drop_reply_ops:
            # The command executed; the answer never arrives. This is the failure the
            # idempotency key exists for, and it must be reproducible on demand.
            return
        await self._send(p.reply_ok(req_id, data, self._now))

    async def _dispatch(self, op: str, args: dict[str, Any]) -> Any:
        v = self.venue
        if op == "account":
            a = v.account_state()
            return {
                "login": a.login, "server": a.server, "currency": a.currency,
                "balance": a.balance, "equity": a.equity, "margin": a.margin,
                "free_margin": a.free_margin, "margin_level": a.margin_level,
                "leverage": a.leverage, "ts": a.ts, "trade_allowed": a.trade_allowed,
            }
        if op == "specs":
            wanted = args.get("symbols") or list(v.specs)
            return {name: _spec_payload(v.specs[name]) for name in wanted if name in v.specs}
        if op == "quote":
            q = await v.quote(args["sym"])
            return {"bid": q.bid, "ask": q.ask, "ts": q.ts}
        if op == "positions":
            return [_position_payload(pos) for pos in await v.positions()]
        if op == "orders":
            return [_pending_payload(o) for o in await v.pending_orders()]
        if op == "bars":
            return []
        if op == "order_send":
            return await self._order_send(args)
        if op == "position_modify":
            r = await v.modify_position(int(args["ticket"]), stop_loss=args.get("sl"),
                                        take_profit=args.get("tp"))
            return {"retcode": r.retcode, "retcode_text": r.retcode_text}
        if op == "position_close":
            r = await v.close_position(int(args["ticket"]), volume=args.get("volume"))
            return {"retcode": r.retcode, "retcode_text": r.retcode_text,
                    "deal": r.deal_ticket, "price": r.fill_price, "volume": r.filled_volume}
        if op == "order_cancel":
            r = await v.cancel_order(int(args["ticket"]))
            return {"retcode": r.retcode, "retcode_text": r.retcode_text}
        if op == "find_by_comment":
            pos = await v.find_by_client_id(str(args.get("comment", "")))
            return _position_payload(pos) if pos else None
        if op == "history_deals":
            since = int(args.get("from_ms", 0))
            return [_trade_payload(t) for t in await v.closed_trades(since)]
        raise ValueError(f"unknown op {op!r}")

    async def _order_send(self, args: dict[str, Any]) -> dict[str, Any]:
        req = OrderRequest(
            client_order_id=str(args.get("comment") or "X")[:16],
            decision_id="conformance",
            symbol=args["sym"], side=Side(args["side"]),
            order_type=OrderType(args.get("type", "MARKET")),
            volume=float(args["volume"]), price=args.get("price"),
            stop_loss=args.get("sl"), take_profit=args.get("tp"),
            deviation_points=int(args.get("deviation", 20)),
            magic=int(args.get("magic", self.magic)),
            comment=str(args.get("comment", "")),
        )
        r = await self.venue.submit(req)
        retcode = r.retcode or (p.RETCODE_DONE if r.accepted else 10013)
        if r.accepted and r.status.value == "PENDING_NEW":
            retcode = p.RETCODE_PLACED
        return {
            "retcode": retcode, "retcode_text": r.retcode_text,
            "order": r.order_ticket, "deal": r.deal_ticket, "position": r.position_ticket,
            "volume": r.filled_volume, "price": r.fill_price,
            "requested_price": r.requested_price, "comment": r.message,
        }


# --- payload builders (the shapes docs/PROTOCOL.md specifies) ---------------------------


def _spec_payload(spec: SymbolSpec) -> dict[str, Any]:
    return {
        "description": spec.description, "digits": spec.digits, "point": spec.point,
        "tick_size": spec.tick_size, "tick_value": spec.tick_value,
        "contract_size": spec.contract_size, "volume_min": spec.volume_min,
        "volume_max": spec.volume_max, "volume_step": spec.volume_step,
        "stops_level": spec.stops_level_points, "freeze_level": spec.freeze_level_points,
        "currency_base": spec.currency_base, "currency_profit": spec.currency_profit,
        "currency_margin": spec.currency_margin, "margin_initial": spec.margin_initial,
        "swap_long": spec.swap_long, "swap_short": spec.swap_short,
        "swap_mode": spec.swap_mode, "swap_rollover_3days": spec.swap_rollover_3days,
        "trade_allowed": spec.trade_allowed,
        "filling_modes": [str(f) for f in spec.filling_modes],
    }


def _position_payload(pos) -> dict[str, Any]:
    return {
        "ticket": pos.ticket, "sym": pos.symbol, "side": str(pos.side),
        "volume": pos.volume, "open_price": pos.open_price, "open_time": pos.open_time,
        # MT5 reports "no stop" as 0.0, and the codec must turn that back into None.
        "sl": pos.stop_loss or 0.0, "tp": pos.take_profit or 0.0,
        "price_current": pos.current_price, "profit": pos.profit, "swap": pos.swap,
        "commission": pos.commission, "magic": pos.magic, "comment": pos.comment,
    }


def _pending_payload(o) -> dict[str, Any]:
    return {
        "ticket": o.ticket, "sym": o.symbol, "side": str(o.side), "type": str(o.order_type),
        "volume": o.volume, "price": o.price, "sl": o.stop_loss or 0.0,
        "tp": o.take_profit or 0.0, "setup_time": o.setup_time, "magic": o.magic,
    }


def _trade_payload(t) -> dict[str, Any]:
    return {
        "trade_id": t.trade_id, "closed": True, "sym": t.symbol, "side": str(t.side),
        "volume": t.volume, "entry_price": t.entry_price, "entry_time": t.entry_time,
        "exit_price": t.exit_price, "exit_time": t.exit_time,
        "initial_stop": t.initial_stop, "risk_money": t.risk_money,
        "profit": t.gross_profit, "commission": t.commission, "swap": t.swap,
        "exit_reason": str(t.exit_reason), "mae_points": t.mae_points,
        "mfe_points": t.mfe_points, "decision_id": t.decision_id,
    }


def bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "v": 1, "t": "bar", "ts": bar.ts_close, "sym": bar.symbol, "tf": str(bar.tf),
        "open_time": bar.ts, "o": bar.open, "h": bar.high, "l": bar.low, "c": bar.close,
        "vol": bar.volume, "spread": bar.spread_points,
    }


def tick_payload(q: Quote) -> dict[str, Any]:
    return {"v": 1, "t": "tick", "ts": q.ts, "sym": q.symbol, "bid": q.bid, "ask": q.ask}


TF_M5 = Timeframe.M5
