"""A stand-in for the ``MetaTrader5`` Python package, backed by the real matching engine.

WHY THIS EXISTS
    ``sidecar/atlas_mt5_sidecar.py`` is the second terminal-side implementation of the
    bridge protocol, and the one that runs when someone prefers not to compile an EA. It
    could not be executed anywhere in CI, because the ``MetaTrader5`` package is Windows-only
    and needs a live terminal, so the whole file sat in the "requires real environment"
    column -- including the parts that have nothing to do with MetaTrader, such as its
    framing, its dispatch table and its payload shapes.

    That is more unverified surface than necessary. This module implements the documented
    ``MetaTrader5`` API against a ``SimulatedVenue``, which lets the sidecar be run for real
    against a real ``BridgeVenue`` over a real socket.

WHAT THIS DOES AND DOES NOT ESTABLISH
    It establishes that the sidecar's protocol half is correct: framing, handshake, dispatch,
    argument handling, payload shapes, retcode propagation, idempotency lookup and error
    mapping.

    It establishes NOTHING about whether the real ``MetaTrader5`` package behaves the way
    this module does. Attribute names and call shapes here are transcribed from its
    documentation, and a transcription can be wrong. If the real package differs, the sidecar
    breaks in a way this cannot see. That is why the sidecar stays in the third column of
    docs/STATUS.md until it has run against a terminal.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from atlas.core.enums import OrderType, Side
from atlas.core.instrument import SymbolSpec
from atlas.core.trading import OrderRequest
from atlas.venues.sim.venue import SimulatedVenue

# --- constants, spelled as the real package spells them --------------------------------

TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = 16385, 16388, 16408, 32769

ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 2, 3
ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5

ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0

TRADE_ACTION_DEAL, TRADE_ACTION_PENDING = 1, 5
TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE = 6, 7, 8

SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC = 1, 2
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_FULL = 4

POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008


# --- record shapes ---------------------------------------------------------------------
#
# The real package returns named tuples; attribute access is all the sidecar uses, so
# dataclasses with the documented field names are an accurate stand-in for that surface.


@dataclass
class TerminalInfo:
    build: int = 4150
    trade_allowed: bool = True
    connected: bool = True


@dataclass
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    leverage: int
    trade_expert: bool = True
    margin_mode: int = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING


@dataclass
class SymbolInfo:
    name: str
    description: str
    digits: int
    point: float
    spread: int
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int
    trade_freeze_level: int
    currency_base: str
    currency_profit: str
    currency_margin: str
    margin_initial: float
    swap_long: float
    swap_short: float
    swap_mode: int
    swap_rollover3days: int
    trade_mode: int
    filling_mode: int


@dataclass
class Tick:
    time: int
    bid: float
    ask: float
    last: float = 0.0
    volume: int = 0


@dataclass
class PositionInfo:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    time: int
    sl: float
    tp: float
    price_current: float
    profit: float
    swap: float
    magic: int
    comment: str


@dataclass
class OrderInfo:
    ticket: int
    symbol: str
    type: int
    volume_current: float
    price_open: float
    sl: float
    tp: float
    time_setup: int
    magic: int
    comment: str


@dataclass
class DealInfo:
    ticket: int
    order: int
    position_id: int
    symbol: str
    type: int
    entry: int
    time: int
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    magic: int
    comment: str


@dataclass
class SendResult:
    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    comment: str
    request_id: int = 0


# --- module state ----------------------------------------------------------------------


@dataclass
class _State:
    venue: SimulatedVenue | None = None
    loop: asyncio.AbstractEventLoop | None = None
    server_offset_seconds: int = 3 * 3600
    initialize_ok: bool = True
    trade_allowed: bool = True
    last_error_value: tuple[int, str] = (1, "Success")
    bars: dict[tuple[str, int], list[tuple[int, float, float, float, float, int, int]]] = (
        field(default_factory=dict)
    )
    #: order_send requests, verbatim, so a test can assert on what the terminal was asked.
    sent: list[dict[str, Any]] = field(default_factory=list)


_state = _State()
_lock = threading.Lock()


def install(
    venue: SimulatedVenue,
    loop: asyncio.AbstractEventLoop,
    *,
    server_offset_seconds: int = 3 * 3600,
) -> _State:
    """Point the fake package at a matching engine and the loop that owns it."""
    global _state
    _state = _State(venue=venue, loop=loop, server_offset_seconds=server_offset_seconds)
    return _state


def _call(coro):
    """Run a venue coroutine, from whichever thread the caller is on.

    The sidecar's run loop lives on its own thread, and hopping to the event loop from there
    is what makes the round trip real rather than a same-thread simulation of one. But the
    sidecar's *setup* (``start_terminal``) is called from the test itself, which IS the loop
    thread -- scheduling onto a loop that is blocked waiting for you deadlocks.

    ``SimulatedVenue``'s coroutines never suspend, so on the loop thread the coroutine is
    stepped once directly. If one ever does suspend, that assumption has broken and this
    raises rather than hanging.
    """
    assert _state.loop is not None
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _state.loop:
        try:
            coro.send(None)
        except StopIteration as stop:
            return stop.value
        coro.close()
        raise RuntimeError(
            "a venue coroutine suspended; fake_mt5 can no longer drive it from the loop thread"
        )
    return asyncio.run_coroutine_threadsafe(coro, _state.loop).result(timeout=10)


def _server_seconds(utc_ms: int) -> int:
    """Server-stamped seconds, which is what the terminal actually hands out."""
    return int(utc_ms / 1000) + _state.server_offset_seconds


# --- the API surface the sidecar uses ----------------------------------------------------


def initialize(*args: Any, **kwargs: Any) -> bool:
    return _state.initialize_ok


def shutdown() -> None:
    return None


def last_error() -> tuple[int, str]:
    return _state.last_error_value


def terminal_info() -> TerminalInfo:
    return TerminalInfo(trade_allowed=_state.trade_allowed)


def account_info() -> AccountInfo | None:
    assert _state.venue is not None
    a = _state.venue.account_state()
    return AccountInfo(
        login=a.login, server=a.server, currency=a.currency, balance=a.balance,
        equity=a.equity, margin=a.margin, margin_free=a.free_margin,
        margin_level=a.margin_level, leverage=a.leverage,
        trade_expert=a.trade_allowed,
    )


def _spec(symbol: str) -> SymbolSpec | None:
    assert _state.venue is not None
    return _state.venue.specs.get(symbol)


def symbol_info(symbol: str) -> SymbolInfo | None:
    spec = _spec(symbol)
    if spec is None:
        return None
    mask = 0
    for mode in spec.filling_modes:
        if str(mode) == "FOK":
            mask |= SYMBOL_FILLING_FOK
        elif str(mode) == "IOC":
            mask |= SYMBOL_FILLING_IOC
    return SymbolInfo(
        name=spec.name, description=spec.description, digits=spec.digits, point=spec.point,
        spread=int(_spread_points(symbol)),
        trade_tick_size=spec.tick_size, trade_tick_value=spec.tick_value,
        trade_contract_size=spec.contract_size, volume_min=spec.volume_min,
        volume_max=spec.volume_max, volume_step=spec.volume_step,
        trade_stops_level=spec.stops_level_points, trade_freeze_level=spec.freeze_level_points,
        currency_base=spec.currency_base, currency_profit=spec.currency_profit,
        currency_margin=spec.currency_margin, margin_initial=spec.margin_initial,
        swap_long=spec.swap_long, swap_short=spec.swap_short, swap_mode=spec.swap_mode,
        # MT5 counts weekdays from Sunday; ATLAS counts from Monday. The sidecar converts,
        # so this must hand back the MT5 convention or the conversion is untested.
        swap_rollover3days=(spec.swap_rollover_3days + 1) % 7,
        trade_mode=SYMBOL_TRADE_MODE_FULL if spec.trade_allowed else SYMBOL_TRADE_MODE_DISABLED,
        filling_mode=mask,
    )


def _spread_points(symbol: str) -> float:
    spec = _spec(symbol)
    tick = symbol_info_tick(symbol)
    if spec is None or tick is None:
        return 0.0
    return round((tick.ask - tick.bid) / spec.point)


def symbols_get(*args: Any) -> list[SymbolInfo]:
    assert _state.venue is not None
    out = []
    for name in _state.venue.specs:
        info = symbol_info(name)
        if info is not None:
            out.append(info)
    return out


def symbol_select(symbol: str, enable: bool = True) -> bool:
    return _spec(symbol) is not None


def symbol_info_tick(symbol: str) -> Tick | None:
    assert _state.venue is not None
    try:
        q = _call(_state.venue.quote(symbol))
    except Exception:
        return None
    return Tick(time=_server_seconds(q.ts), bid=q.bid, ask=q.ask)


def positions_get(*, ticket: int | None = None, symbol: str | None = None) -> tuple:
    assert _state.venue is not None
    positions = _call(_state.venue.positions())
    out = []
    for p in positions:
        if ticket is not None and p.ticket != ticket:
            continue
        if symbol is not None and p.symbol != symbol:
            continue
        out.append(PositionInfo(
            ticket=p.ticket, symbol=p.symbol,
            type=POSITION_TYPE_BUY if str(p.side) == "BUY" else POSITION_TYPE_SELL,
            volume=p.volume, price_open=p.open_price, time=_server_seconds(p.open_time),
            # MT5 reports "no stop" as 0.0 rather than as a null.
            sl=p.stop_loss or 0.0, tp=p.take_profit or 0.0,
            price_current=p.current_price, profit=p.profit, swap=p.swap,
            magic=p.magic, comment=p.comment,
        ))
    return tuple(out)


def orders_get(*, ticket: int | None = None, symbol: str | None = None) -> tuple:
    assert _state.venue is not None
    orders = _call(_state.venue.pending_orders())
    kinds = {
        ("BUY", "LIMIT"): ORDER_TYPE_BUY_LIMIT, ("SELL", "LIMIT"): ORDER_TYPE_SELL_LIMIT,
        ("BUY", "STOP"): ORDER_TYPE_BUY_STOP, ("SELL", "STOP"): ORDER_TYPE_SELL_STOP,
    }
    out = []
    for o in orders:
        if ticket is not None and o.ticket != ticket:
            continue
        if symbol is not None and o.symbol != symbol:
            continue
        out.append(OrderInfo(
            ticket=o.ticket, symbol=o.symbol,
            type=kinds[(str(o.side), str(o.order_type))],
            volume_current=o.volume, price_open=o.price,
            sl=o.stop_loss or 0.0, tp=o.take_profit or 0.0,
            time_setup=_server_seconds(o.setup_time), magic=o.magic, comment="",
        ))
    return tuple(out)


_RATES_DTYPE = np.dtype([
    ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
    ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
])


def set_bars(symbol: str, timeframe: int, rows: list[tuple]) -> None:
    """Seed the history a ``copy_rates_from_pos`` call walks back through.

    Rows are newest-last, in server seconds, exactly as the terminal stores them.
    """
    _state.bars[(symbol, timeframe)] = list(rows)


def copy_rates_from_pos(symbol: str, timeframe: int, start: int, count: int):
    rows = _state.bars.get((symbol, timeframe))
    if not rows:
        return None
    # Index 0 is the bar still forming and indices grow into the past, which is the
    # convention the sidecar's `start=1` depends on.
    newest_first = list(reversed(rows))
    window = newest_first[start:start + count]
    if not window:
        return None
    # The real package returns oldest-first within the requested window.
    window = list(reversed(window))
    arr = np.zeros(len(window), dtype=_RATES_DTYPE)
    for i, row in enumerate(window):
        arr[i] = (row[0], row[1], row[2], row[3], row[4], row[5], row[6], 0)
    return arr


def history_deals_get(start, end) -> tuple:
    assert _state.venue is not None
    trades = _call(_state.venue.closed_trades(0))
    out: list[DealInfo] = []
    for t in trades:
        pid = int(t.trade_id) if str(t.trade_id).isdigit() else abs(hash(t.trade_id)) % 10**9
        is_buy = str(t.side) == "BUY"
        out.append(DealInfo(
            ticket=pid * 10, order=pid, position_id=pid, symbol=t.symbol,
            type=DEAL_TYPE_BUY if is_buy else DEAL_TYPE_SELL, entry=DEAL_ENTRY_IN,
            time=_server_seconds(t.entry_time), volume=t.volume, price=t.entry_price,
            profit=0.0, commission=t.commission / 2, swap=0.0, magic=_MAGIC,
            comment=t.decision_id or "",
        ))
        out.append(DealInfo(
            # The closing deal of a long is a SELL: the sidecar inverts it back, and that
            # inversion is exactly what this pair is here to exercise.
            ticket=pid * 10 + 1, order=pid, position_id=pid, symbol=t.symbol,
            type=DEAL_TYPE_SELL if is_buy else DEAL_TYPE_BUY, entry=DEAL_ENTRY_OUT,
            time=_server_seconds(t.exit_time), volume=t.volume, price=t.exit_price,
            profit=t.gross_profit, commission=t.commission / 2, swap=t.swap, magic=_MAGIC,
            comment="",
        ))
    return tuple(out)


_MAGIC = 20260823


def set_magic(magic: int) -> None:
    global _MAGIC
    _MAGIC = magic


def order_send(request: dict[str, Any]) -> SendResult | None:
    assert _state.venue is not None
    with _lock:
        _state.sent.append(dict(request))
    action = request.get("action")

    if action == TRADE_ACTION_SLTP:
        r = _call(_state.venue.modify_position(
            int(request["position"]),
            stop_loss=request.get("sl") or None,
            take_profit=request.get("tp") or None,
        ))
        return SendResult(retcode=r.retcode or TRADE_RETCODE_DONE, deal=0, order=0,
                          volume=0.0, price=0.0, comment=r.retcode_text)

    if action == TRADE_ACTION_REMOVE:
        r = _call(_state.venue.cancel_order(int(request["order"])))
        return SendResult(retcode=r.retcode or TRADE_RETCODE_DONE, deal=0, order=0,
                          volume=0.0, price=0.0, comment=r.retcode_text)

    if action == TRADE_ACTION_DEAL and request.get("position"):
        r = _call(_state.venue.close_position(
            int(request["position"]), volume=float(request["volume"])
        ))
        return SendResult(retcode=r.retcode or TRADE_RETCODE_DONE,
                          deal=r.deal_ticket or 0, order=r.order_ticket or 0,
                          volume=r.filled_volume, price=r.fill_price or 0.0,
                          comment=r.retcode_text)

    side = Side.BUY if request["type"] in (
        ORDER_TYPE_BUY, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_BUY_STOP
    ) else Side.SELL
    if action == TRADE_ACTION_PENDING:
        otype = OrderType.LIMIT if request["type"] in (
            ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT
        ) else OrderType.STOP
    else:
        otype = OrderType.MARKET
    req = OrderRequest(
        client_order_id=str(request.get("comment") or "X")[:16],
        decision_id="sidecar-conformance",
        symbol=request["symbol"], side=side, order_type=otype,
        volume=float(request["volume"]),
        price=float(request["price"]) if otype is not OrderType.MARKET else None,
        stop_loss=request.get("sl") or None, take_profit=request.get("tp") or None,
        deviation_points=int(request.get("deviation", 20)),
        magic=int(request.get("magic", _MAGIC)),
        comment=str(request.get("comment", "")),
    )
    r = _call(_state.venue.submit(req))
    if r.accepted:
        retcode = r.retcode or (
            TRADE_RETCODE_PLACED if r.status.value == "PENDING_NEW" else TRADE_RETCODE_DONE
        )
    else:
        retcode = r.retcode or 10013
    return SendResult(
        retcode=retcode, deal=r.deal_ticket or 0, order=r.order_ticket or 0,
        volume=r.filled_volume, price=r.fill_price or 0.0,
        comment=r.retcode_text or r.message or "",
    )
