"""MetaTrader 5 bridge venue.

ATLAS listens; the terminal connects out (see ``docs/PROTOCOL.md`` §1 for why -- MQL5's
native socket API provides outbound client sockets only, and making the terminal the client
is what removes the ZeroMQ DLL dependency entirely).

This one class is the only Python code that talks to MetaTrader, and it cannot tell whether
the peer is the MQL5 EA or the Windows sidecar. That is the point of having a protocol rather
than an integration.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

from atlas.core.enums import ExitReason, OrderStatus, Timeframe
from atlas.core.errors import TransientError, VenueError, VenueUnavailable
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, Quote
from atlas.core.trading import (
    AccountState,
    OrderRequest,
    OrderResult,
    PendingOrder,
    Position,
    Trade,
)
from atlas.execution.venue import ExecutionVenue, VenueCapabilities, VenueHealth
from atlas.venues.mt5 import protocol as p


class BridgeVenue(ExecutionVenue):
    """Speaks the ATLAS bridge protocol to whichever terminal-side implementation connects."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 5555,
        token: str = "",
        request_timeout_ms: int = 5000,
        command_endpoint: str | None = None,
        event_endpoint: str | None = None,  # accepted for config compatibility; unused
        on_tick: Callable[[Quote], None] | None = None,
        on_bar: Callable[[Bar], None] | None = None,
        on_transaction: Callable[[dict[str, Any]], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        clock=None,
    ) -> None:
        if command_endpoint:
            host, port = _parse_endpoint(command_endpoint, host, port)
        self.host = host
        self.port = port
        self.token = token
        self.timeout = request_timeout_ms / 1000.0
        self._on_tick = on_tick
        self._on_bar = on_bar
        self._on_transaction = on_transaction
        self._on_log = on_log
        self._clock = clock

        self._server: asyncio.AbstractServer | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._req_seq = 0
        self._connected = asyncio.Event()
        self._hello: p.Hello | None = None
        self._account = AccountState()
        self._quotes: dict[str, Quote] = {}
        self._specs: dict[str, SymbolSpec] = {}
        self._last_message_ms = 0
        self._latency_ms = 0.0
        self._peer: str = ""
        self.connection_count = 0

    # -- lifecycle ------------------------------------------------------------------

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            streaming_quotes=True, streaming_trade_events=True, pending_orders=True,
            partial_close=True, client_id_lookup=True, name="mt5-bridge",
            extra={"impl": self._hello.impl if self._hello else "", "peer": self._peer},
        )

    def _now(self) -> int:
        return self._clock.now_ms() if self._clock is not None else int(time.time() * 1000)

    async def start(self) -> None:
        """Begin listening. Returns as soon as the socket is bound, not when a terminal
        connects -- the engine treats "no terminal yet" as an unhealthy venue rather than as
        a startup failure, so a terminal that comes up second still works."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def connect(self, wait_seconds: float = 30.0) -> VenueHealth:
        await self.start()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=wait_seconds)
        except TimeoutError as exc:
            raise VenueUnavailable(
                f"no MetaTrader terminal connected to {self.host}:{self.port} within "
                f"{wait_seconds:.0f}s. Check that AtlasBridge is attached to a chart, that "
                f"algorithmic trading is enabled, and that the address is permitted in "
                f"Tools > Options > Expert Advisors."
            ) from exc
        return await self.health()

    async def disconnect(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._connected.clear()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if self._writer is not None and not self._writer.is_closing():
            # Two terminals trading one strategy is a double-size accident. The newcomer
            # displaces the incumbent and the event is journalled by the caller.
            old = self._writer
            self._writer = None
            old.close()
        self._reader, self._writer = reader, writer
        self._peer = str(peer)
        self.connection_count += 1
        try:
            await self._read_loop()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._connected.clear()
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(VenueUnavailable("terminal disconnected mid-request"))
            self._pending.clear()
            with contextlib.suppress(Exception):
                writer.close()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                return
            if len(line) > p.MAX_FRAME_BYTES:
                raise p.ProtocolError("oversized frame")
            try:
                msg = p.decode(line)
            except p.ProtocolError as exc:
                if self._on_log:
                    self._on_log("ERROR", f"bad frame from terminal: {exc}")
                continue
            self._last_message_ms = self._now()
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "hello":
            await self._on_hello(msg)
        elif kind == "reply":
            fut = self._pending.pop(str(msg.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif kind == "tick":
            q = Quote(symbol=msg["sym"], ts=int(msg.get("ts", 0)),
                      bid=float(msg["bid"]), ask=float(msg["ask"]))
            self._quotes[q.symbol] = q
            if self._on_tick:
                self._on_tick(q)
        elif kind == "bar":
            if self._on_bar:
                self._on_bar(Bar(
                    symbol=msg["sym"], tf=Timeframe(msg["tf"]), ts=int(msg["open_time"]),
                    open=float(msg["o"]), high=float(msg["h"]), low=float(msg["l"]),
                    close=float(msg["c"]), volume=float(msg.get("v", 0.0)),
                    spread_points=float(msg.get("spread", 0.0)), complete=True,
                ))
        elif kind == "txn":
            if self._on_transaction:
                self._on_transaction(msg)
        elif kind == "log":
            if self._on_log:
                self._on_log(str(msg.get("level", "INFO")), str(msg.get("msg", "")))
        elif kind == "pong":
            fut = self._pending.pop(str(msg.get("id")), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        # Unknown types are ignored on purpose: a newer terminal build must be able to talk
        # to an older engine (docs/PROTOCOL.md §2).

    async def _on_hello(self, msg: dict[str, Any]) -> None:
        try:
            hello = p.parse_hello(msg)
        except p.ProtocolError as exc:
            await self._send(p.envelope("bye", self._now(), reason=str(exc)))
            self._close_writer()
            raise
        if self.token and hello.token != self.token:
            await self._send(p.envelope("bye", self._now(), reason="authentication failed"))
            self._close_writer()
            raise VenueError(
                "terminal presented the wrong bridge token; the connection was refused"
            )
        self._hello = hello
        self._account = hello.account
        self._connected.set()
        await self._send(p.envelope("welcome", self._now(), server="atlas",
                                    accepted_version=p.PROTOCOL_VERSION))

    def _close_writer(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._connected.clear()

    # -- request/response --------------------------------------------------------------

    async def _send(self, message: dict[str, Any]) -> None:
        if self._writer is None or self._writer.is_closing():
            raise VenueUnavailable("no terminal connection")
        self._writer.write(p.encode(message))
        await self._writer.drain()

    async def _request(self, op: str, args: dict[str, Any] | None = None) -> Any:
        """Send a command and await its reply.

        A timeout raises ``TransientError``, which the order router treats as retryable --
        but only after confirming via ``find_by_client_id`` that the request did not land
        (ADR-013). That distinction is the whole reason timeouts are typed rather than
        described in a message string.
        """
        if not self._connected.is_set():
            raise VenueUnavailable("no MetaTrader terminal is connected")
        self._req_seq += 1
        req_id = f"r{self._req_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        started = time.perf_counter()
        await self._send(p.request(req_id, op, args or {}, self._now()))
        try:
            reply = await asyncio.wait_for(fut, timeout=self.timeout)
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise TransientError(
                f"the terminal did not answer '{op}' within {self.timeout:.1f}s"
            ) from exc
        finally:
            self._latency_ms = (time.perf_counter() - started) * 1000.0
        if not reply.get("ok", False):
            err = reply.get("error", {}) or {}
            raise VenueError(
                f"{op} failed: {err.get('message', 'unknown error')}",
                retcode=int(err.get("retcode", 0)),
                retcode_text=str(err.get("code", "")),
            )
        return reply.get("data")

    # -- ExecutionVenue ------------------------------------------------------------------

    async def health(self) -> VenueHealth:
        connected = self._connected.is_set()
        offset = self._hello.server_offset_seconds if self._hello else None
        if not connected:
            return VenueHealth(connected=False, trade_allowed=False,
                               detail="no terminal connected", server_offset_seconds=offset)
        try:
            await asyncio.wait_for(self._ping(), timeout=self.timeout)
            alive = True
            detail = f"{self._hello.impl if self._hello else '?'} @ {self._peer}"
        except (TimeoutError, VenueUnavailable, TransientError) as exc:
            alive = False
            detail = f"terminal did not answer ping: {exc}"
        return VenueHealth(
            connected=alive, trade_allowed=alive and self._account.trade_allowed,
            server_time_ms=self._hello.server_time_ms if self._hello else 0,
            latency_ms=self._latency_ms, detail=detail, server_offset_seconds=offset,
        )

    async def _ping(self) -> None:
        self._req_seq += 1
        req_id = f"p{self._req_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send(p.envelope("ping", self._now(), id=req_id))
        await fut

    async def account(self) -> AccountState:
        data = await self._request("account")
        self._account = p.parse_account(data or {})
        return self._account

    async def symbol_specs(self, symbols: list[str]) -> dict[str, SymbolSpec]:
        data = await self._request("specs", {"symbols": symbols}) or {}
        out: dict[str, SymbolSpec] = {}
        for name, payload in data.items():
            out[name] = p.parse_spec(name, payload)
        self._specs.update(out)
        return out

    async def quote(self, symbol: str) -> Quote:
        cached = self._quotes.get(symbol)
        if cached is not None:
            return cached
        data = await self._request("quote", {"sym": symbol})
        q = p.parse_quote(symbol, data or {})
        self._quotes[symbol] = q
        return q

    async def positions(self) -> list[Position]:
        data = await self._request("positions", {}) or []
        return [p.parse_position(d) for d in data]

    async def pending_orders(self) -> list[PendingOrder]:
        data = await self._request("orders", {}) or []
        return [p.parse_pending(d) for d in data]

    async def bars(self, symbol: str, tf: Timeframe, count: int) -> list[Bar]:
        data = await self._request("bars", {"sym": symbol, "tf": str(tf), "count": count}) or []
        return [p.parse_bar(symbol, tf, row) for row in data]

    async def submit(self, request: OrderRequest) -> OrderResult:
        spec = self._specs.get(request.symbol)
        if spec is None:
            spec = (await self.symbol_specs([request.symbol])).get(request.symbol)
        if spec is None:
            raise VenueError(f"no symbol specification for {request.symbol}")
        args = p.order_send_args(request, spec)
        data = await self._request("order_send", args) or {}
        return p.parse_order_result(request.client_order_id, data, self._now())

    async def modify_position(
        self, ticket: int, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> OrderResult:
        data = await self._request(
            "position_modify", {"ticket": ticket, "sl": stop_loss, "tp": take_profit}
        ) or {}
        return p.parse_order_result("", {**data, "position": ticket}, self._now())

    async def close_position(
        self, ticket: int, *, volume: float | None = None,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> OrderResult:
        # `reason` is ATLAS bookkeeping; no broker records why a position was closed, so it
        # is deliberately not sent. The engine journals it alongside the result.
        data = await self._request(
            "position_close", {"ticket": ticket, "volume": volume}
        ) or {}
        return p.parse_order_result("", {**data, "position": ticket}, self._now())

    async def cancel_order(self, ticket: int) -> OrderResult:
        data = await self._request("order_cancel", {"ticket": ticket}) or {}
        result = p.parse_order_result("", data, self._now())
        if result.accepted:
            return result.model_copy(update={"status": OrderStatus.CANCELLED})
        return result

    async def find_by_client_id(self, client_order_id: str) -> Position | None:
        """The idempotency lookup (ADR-013).

        A failure here must not be reported as "not found": that would let the router
        resubmit an order that may already be live. Any error propagates, and the router's
        own handling degrades to not retrying.
        """
        data = await self._request("find_by_comment", {"comment": client_order_id})
        return p.parse_position(data) if data else None

    async def closed_trades(self, since_ms: int) -> list[Trade]:
        """Reconstruct round trips from the broker's deal history.

        The bridge returns deals already paired terminal-side, because pairing needs
        position ids that MT5 exposes on the deal and that would otherwise have to be
        re-derived here from an incomplete view.
        """
        data = await self._request("history_deals", {"from_ms": since_ms}) or []
        out: list[Trade] = []
        for d in data:
            if not d.get("closed"):
                continue
            spec = self._specs.get(d.get("sym", ""))
            out.append(Trade(
                trade_id=str(d.get("trade_id") or d.get("position")),
                decision_id=str(d.get("decision_id", "")),
                symbol=str(d["sym"]),
                side=p.Side(str(d["side"]).upper()),
                volume=float(d["volume"]),
                entry_price=float(d["entry_price"]), entry_time=int(d["entry_time"]),
                exit_price=float(d["exit_price"]), exit_time=int(d["exit_time"]),
                initial_stop=float(d.get("initial_stop", 0.0)),
                initial_target=d.get("initial_target"),
                risk_money=float(d.get("risk_money", 0.0)),
                gross_profit=float(d.get("profit", 0.0)),
                commission=float(d.get("commission", 0.0)),
                swap=float(d.get("swap", 0.0)),
                exit_reason=ExitReason(str(d.get("exit_reason", "UNKNOWN")).upper())
                if str(d.get("exit_reason", "")).upper() in ExitReason.__members__
                else ExitReason.UNKNOWN,
                mae_points=float(d.get("mae_points", 0.0)),
                mfe_points=float(d.get("mfe_points", 0.0)),
                point=spec.point if spec else 0.0,
            ))
        return out


def _parse_endpoint(endpoint: str, default_host: str, default_port: int) -> tuple[str, int]:
    """Accept ``tcp://host:port`` or ``host:port``.

    ``tcp://`` is tolerated so an existing ZeroMQ-style config keeps working after the
    transport change documented in docs/PROTOCOL.md §1.
    """
    raw = endpoint.split("://", 1)[-1]
    if ":" not in raw:
        return raw or default_host, default_port
    host, _, port = raw.rpartition(":")
    try:
        return (host or default_host), int(port)
    except ValueError:
        return default_host, default_port
