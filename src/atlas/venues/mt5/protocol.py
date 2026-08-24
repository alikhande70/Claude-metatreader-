"""Wire protocol codec for the MetaTrader 5 bridge.

The authority is ``docs/PROTOCOL.md``. This module is the Python half of it: framing,
message construction, and the conversions between broker representations and ATLAS domain
objects.

Every conversion lives here rather than being spread across the client, so there is exactly
one place where a broker's field naming, its side encoding, or its point/tick semantics are
interpreted -- and exactly one place the conformance suite has to cover.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from atlas.core.enums import FillPolicy, OrderStatus, OrderType, Side
from atlas.core.errors import VenueError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, Quote
from atlas.core.trading import AccountState, OrderRequest, OrderResult, PendingOrder, Position

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 256 * 1024

# --- MT5 return codes we interpret explicitly ---------------------------------------
RETCODE_DONE = 10009
RETCODE_DONE_PARTIAL = 10010
RETCODE_PLACED = 10008

#: Retcodes that mean the request succeeded in some form.
SUCCESS_RETCODES = frozenset({RETCODE_DONE, RETCODE_DONE_PARTIAL, RETCODE_PLACED})

RETCODE_TEXT: dict[int, str] = {
    10004: "REQUOTE -- the price changed before the request was processed",
    10006: "REJECT -- the request was rejected by the dealer",
    10007: "CANCEL -- cancelled by the trader",
    10008: "PLACED -- the order was placed but not yet executed",
    10009: "DONE -- the request was completed",
    10010: "DONE_PARTIAL -- only part of the requested volume was filled",
    10011: "ERROR -- request processing error",
    10012: "TIMEOUT -- the request was cancelled by timeout",
    10013: "INVALID -- malformed request",
    10014: "INVALID_VOLUME -- volume is not a multiple of the lot step, or out of range",
    10015: "INVALID_PRICE -- price is invalid for this order type",
    10016: "INVALID_STOPS -- SL/TP violate the broker's stops level or are on the wrong side",
    10017: "TRADE_DISABLED -- trading is disabled for this account or symbol",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY -- insufficient free margin",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF -- no quotes available to process the request",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS -- the terminal is rate limiting",
    10025: "NO_CHANGES -- the modification would change nothing",
    10026: "SERVER_DISABLES_AT -- algorithmic trading disabled by the server",
    10027: "CLIENT_DISABLES_AT -- algorithmic trading disabled in the terminal",
    10028: "LOCKED -- the request is locked for processing",
    10029: "FROZEN -- the position or order is inside the freeze level and cannot be modified",
    10030: "INVALID_FILL -- the requested filling mode is not supported by this symbol",
    10031: "CONNECTION -- no connection to the trade server",
    10033: "LIMIT_ORDERS -- the pending-order limit was reached",
    10034: "LIMIT_VOLUME -- the volume limit for this symbol was reached",
    10036: "POSITION_CLOSED -- the position has already been closed",
}


def retcode_text(code: int) -> str:
    return RETCODE_TEXT.get(code, f"unmapped MT5 retcode {code}")


class ProtocolError(VenueError):
    """The peer sent something that violates docs/PROTOCOL.md."""


# --- framing ---------------------------------------------------------------------------


def encode(message: dict[str, Any]) -> bytes:
    """Serialise one message as a newline-terminated JSON frame."""
    raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) + 1 > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(raw)} bytes exceeds the {MAX_FRAME_BYTES} limit")
    return raw + b"\n"


def decode(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        if len(line) > MAX_FRAME_BYTES:
            raise ProtocolError(f"frame of {len(line)} bytes exceeds the {MAX_FRAME_BYTES} limit")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"frame is not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError(f"frame must be a JSON object, got {type(obj).__name__}")
    if "t" not in obj:
        raise ProtocolError("frame has no message type field 't'")
    return obj


def envelope(msg_type: str, ts: int, **fields: Any) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "t": msg_type, "ts": ts, **fields}


def request(req_id: str, op: str, args: dict[str, Any], ts: int) -> dict[str, Any]:
    return envelope("req", ts, id=req_id, op=op, args=args)


def reply_ok(req_id: str, data: Any, ts: int) -> dict[str, Any]:
    return envelope("reply", ts, id=req_id, ok=True, data=data)


def reply_error(req_id: str, code: str, message: str, ts: int, retcode: int = 0) -> dict:
    return envelope("reply", ts, id=req_id, ok=False,
                    error={"code": code, "message": message, "retcode": retcode})


# --- conversions -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hello:
    token: str
    impl: str
    build: int
    server_time_ms: int
    sent_ts: int
    account: AccountState
    symbols: tuple[str, ...]
    version: int = PROTOCOL_VERSION

    @property
    def server_offset_seconds(self) -> int:
        """Broker server time minus UTC, quantised to 15 minutes.

        Measured rather than assumed: UTC+2/+3 is a convention, not a rule, and it moves at
        DST boundaries that differ by region.
        """
        raw = (self.server_time_ms - self.sent_ts) / 1000.0
        return int(round(raw / 900.0) * 900)


def parse_hello(msg: dict[str, Any]) -> Hello:
    version = int(msg.get("v", 0))
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"protocol version {version} is not supported by this build (expected "
            f"{PROTOCOL_VERSION}); upgrade the terminal-side bridge"
        )
    try:
        return Hello(
            token=str(msg.get("token", "")),
            impl=str(msg.get("impl", "unknown")),
            build=int(msg.get("build", 0)),
            server_time_ms=int(msg.get("server_time_ms", msg.get("ts", 0))),
            sent_ts=int(msg.get("ts", 0)),
            account=parse_account(msg.get("account", {})),
            symbols=tuple(msg.get("symbols", ())),
            version=version,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"malformed hello: {exc}") from exc


def parse_account(d: dict[str, Any]) -> AccountState:
    return AccountState(
        login=int(d.get("login", 0)), server=str(d.get("server", "")),
        currency=str(d.get("currency", "USD")),
        balance=float(d.get("balance", 0.0)), equity=float(d.get("equity", 0.0)),
        margin=float(d.get("margin", 0.0)), free_margin=float(d.get("free_margin", 0.0)),
        margin_level=float(d.get("margin_level", 0.0)), leverage=int(d.get("leverage", 0)),
        ts=int(d.get("ts", 0)), trade_allowed=bool(d.get("trade_allowed", True)),
    )


_FILL_BY_NAME = {"FOK": FillPolicy.FOK, "IOC": FillPolicy.IOC, "RETURN": FillPolicy.RETURN}


def parse_spec(name: str, d: dict[str, Any]) -> SymbolSpec:
    """Build a SymbolSpec from a broker payload.

    Two derived values are computed defensively rather than trusted, because a broker (or a
    bridge bug) that reports a zero here would otherwise produce a silent divide-by-zero deep
    in the sizing maths:

    * ``point`` falls back to ``10 ** -digits``.
    * ``tick_size`` falls back to ``point``.
    """
    digits = int(d.get("digits", 5))
    point = float(d.get("point", 0.0)) or 10.0**-digits
    tick_size = float(d.get("tick_size", 0.0)) or point
    tick_value = float(d.get("tick_value", 0.0))
    if tick_value <= 0:
        raise ProtocolError(
            f"{name}: broker reported tick_value={tick_value}. Every position size depends on "
            f"it, so this is refused rather than defaulted."
        )
    modes = tuple(
        _FILL_BY_NAME[m] for m in d.get("filling_modes", ["FOK"]) if m in _FILL_BY_NAME
    ) or (FillPolicy.FOK,)
    return SymbolSpec(
        name=name, description=str(d.get("description", "")), digits=digits, point=point,
        tick_size=tick_size, tick_value=tick_value,
        contract_size=float(d.get("contract_size", 1.0)) or 1.0,
        volume_min=float(d.get("volume_min", 0.01)) or 0.01,
        volume_max=float(d.get("volume_max", 100.0)) or 100.0,
        volume_step=float(d.get("volume_step", 0.01)) or 0.01,
        stops_level_points=int(d.get("stops_level", 0)),
        freeze_level_points=int(d.get("freeze_level", 0)),
        currency_base=str(d.get("currency_base", "")),
        currency_profit=str(d.get("currency_profit", "")),
        currency_margin=str(d.get("currency_margin", "")),
        margin_initial=float(d.get("margin_initial", 0.0)),
        swap_long=float(d.get("swap_long", 0.0)), swap_short=float(d.get("swap_short", 0.0)),
        swap_mode=int(d.get("swap_mode", 0)),
        swap_rollover_3days=int(d.get("swap_rollover_3days", 3)),
        filling_modes=modes,
        trade_allowed=bool(d.get("trade_allowed", True)),
    )


def parse_quote(symbol: str, d: dict[str, Any]) -> Quote:
    return Quote(symbol=symbol, ts=int(d.get("ts", 0)),
                 bid=float(d["bid"]), ask=float(d["ask"]))


def parse_position(d: dict[str, Any]) -> Position:
    return Position(
        ticket=int(d["ticket"]), symbol=str(d["sym"]), side=Side(str(d["side"]).upper()),
        volume=float(d["volume"]), open_price=float(d["open_price"]),
        open_time=int(d.get("open_time", 0)),
        stop_loss=_opt_float(d.get("sl")), take_profit=_opt_float(d.get("tp")),
        current_price=float(d.get("price_current", 0.0)),
        profit=float(d.get("profit", 0.0)), swap=float(d.get("swap", 0.0)),
        commission=float(d.get("commission", 0.0)), magic=int(d.get("magic", 0)),
        comment=str(d.get("comment", "")),
    )


def parse_pending(d: dict[str, Any]) -> PendingOrder:
    return PendingOrder(
        ticket=int(d["ticket"]), symbol=str(d["sym"]), side=Side(str(d["side"]).upper()),
        order_type=OrderType(str(d.get("type", "LIMIT")).upper()),
        volume=float(d["volume"]), price=float(d["price"]),
        stop_loss=_opt_float(d.get("sl")), take_profit=_opt_float(d.get("tp")),
        setup_time=int(d.get("setup_time", 0)), expiry_ms=_opt_int(d.get("expiry_ms")),
        magic=int(d.get("magic", 0)), comment=str(d.get("comment", "")),
    )


def parse_bar(symbol: str, tf, row: list[Any]) -> Bar:
    open_time, o, h, low, c, v, spread = row
    return Bar(symbol=symbol, tf=tf, ts=int(open_time), open=float(o), high=float(h),
               low=float(low), close=float(c), volume=float(v),
               spread_points=float(spread), complete=True)


def order_send_args(req: OrderRequest, spec: SymbolSpec) -> dict[str, Any]:
    """Render an OrderRequest as ``order_send`` arguments.

    The filling mode is resolved from the **symbol's** advertised modes rather than
    hardcoded: sending FOK to a symbol that only supports IOC is retcode 10030, and it is one
    of the most common reasons an EA that works on one broker never fills on another.
    """
    fill = req.fill_policy or spec.preferred_filling()
    return {
        "sym": req.symbol,
        "side": str(req.side),
        "type": str(req.order_type),
        "volume": round(req.volume, 8),
        "price": req.price,
        "sl": req.stop_loss,
        "tp": req.take_profit,
        "deviation": int(req.deviation_points),
        "magic": int(req.magic),
        "comment": req.comment or req.client_order_id,
        "filling": str(fill),
        "tif": str(req.tif),
        "expiry_ms": req.expiry_ms,
    }


def parse_order_result(
    client_order_id: str,
    d: dict[str, Any],
    ts: int,
    *,
    order_type: OrderType | None = None,
) -> OrderResult:
    """Turn a terminal's ``order_send`` reply into an ``OrderResult``.

    ``order_type`` is what ATLAS *asked for*, and it takes precedence over the retcode when
    deciding whether a position now exists.

    The retcode alone cannot answer that. MT5 has two success codes -- 10008 PLACED and
    10009 DONE -- and which one a server returns for a resting order is the server's choice;
    current builds answer 10009 for `TRADE_ACTION_PENDING`. Reading that as FILLED would
    make the engine believe it holds a position when it holds a limit order, with a fill
    price of 0.0 standing in for a price that does not exist yet. ATLAS knows it sent a LIMIT
    or a STOP, so it does not have to guess.

    Omit ``order_type`` for replies that are not order placements (a modify or a close),
    where the retcode is the only thing on offer.
    """
    retcode = int(d.get("retcode", 0))
    accepted = retcode in SUCCESS_RETCODES
    resting = order_type is not None and order_type is not OrderType.MARKET
    if not accepted:
        status = OrderStatus.REJECTED
    elif resting:
        # An accepted pending order is resting at the venue, whichever success code it used.
        status = OrderStatus.WORKING
    elif retcode == RETCODE_PLACED:
        status = OrderStatus.WORKING
    elif retcode == RETCODE_DONE_PARTIAL:
        status = OrderStatus.PARTIALLY_FILLED
    else:
        status = OrderStatus.FILLED
    return OrderResult(
        client_order_id=client_order_id, accepted=accepted, status=status, retcode=retcode,
        retcode_text=d.get("retcode_text") or retcode_text(retcode),
        order_ticket=int(d.get("order", 0)), deal_ticket=int(d.get("deal", 0)),
        position_ticket=int(d.get("position", 0)),
        filled_volume=float(d.get("volume", 0.0)), fill_price=float(d.get("price", 0.0)),
        requested_price=float(d.get("requested_price", 0.0)),
        commission=float(d.get("commission", 0.0)), ts=ts,
        message=str(d.get("comment", "")),
    )


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    f = float(v)
    # MT5 reports "no stop" as 0.0, which is not a price. Treating it as one would make the
    # trade manager think a position is protected when it is not.
    return None if f == 0.0 else f


def _opt_int(v: Any) -> int | None:
    return None if v in (None, 0) else int(v)
