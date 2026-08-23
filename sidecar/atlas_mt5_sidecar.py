#!/usr/bin/env python3
"""ATLAS MetaTrader 5 sidecar -- the second implementation of the bridge protocol.

Runs on **Windows**, next to a running MetaTrader 5 terminal, and uses the official
``MetaTrader5`` Python package instead of an EA. It connects out to ATLAS and speaks exactly
the protocol in ``docs/PROTOCOL.md``, so the engine cannot tell it apart from
``mql5/AtlasBridge.mq5``.

When to use which
-----------------
The EA is the primary path: it delivers genuine push events through ``OnTick`` and
``OnTradeTransaction`` and needs no second process. This sidecar exists because betting a
live trading system on a single integration is the actual risk. It covers the cases the EA
cannot: a terminal where an EA may not be attached, a broker whose chart is occupied, or a
build where the MQL5 socket permission cannot be granted. It is also the reference used to
validate protocol semantics against a real terminal, because a bug here is a Python bug that
can be debugged with a debugger.

Its limitation is honest and structural: the ``MetaTrader5`` package has **no push API**. It
polls. Ticks and bars are read on an interval, so a fast market is sampled rather than
streamed. ATLAS makes no decisions from ticks (ADR-016), so this costs bar-close latency
rather than correctness — but it is the reason the EA is preferred.

Usage
-----
    pip install MetaTrader5
    set ATLAS_BRIDGE_TOKEN=...
    python atlas_mt5_sidecar.py --host 127.0.0.1 --port 5555 --symbols XAUUSD \
        --timeframes M5,M15,H1,H4 --magic 20260823

This file is dependency-free apart from ``MetaTrader5`` itself, so it can be copied to a
Windows box on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from datetime import UTC, datetime
from typing import Any

LOG = logging.getLogger("atlas.sidecar")
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 256 * 1024

try:  # pragma: no cover - Windows only
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None


TIMEFRAMES: dict[str, int] = {}


def _init_timeframe_map() -> None:
    if mt5 is None:
        return
    TIMEFRAMES.update({
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
    })


def utc_now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def server_offset_seconds() -> int:
    """Broker server time minus UTC, measured and quantised to 15 minutes.

    The ``MetaTrader5`` package has no direct "server time" call, so it is inferred from the
    most recent tick's timestamp, which the terminal stamps in server time. Quantising
    absorbs the sampling jitter without inventing a bogus offset.
    """
    if mt5 is None:
        return 0
    for symbol in mt5.symbols_get() or []:
        tick = mt5.symbol_info_tick(symbol.name)
        if tick and tick.time:
            raw = tick.time - int(time.time())
            return int(round(raw / 900.0) * 900)
    return 0


def to_utc_ms(server_epoch_seconds: int, offset: int) -> int:
    return (int(server_epoch_seconds) - offset) * 1000


class Sidecar:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.port = args.port
        self.token = args.token or os.environ.get("ATLAS_BRIDGE_TOKEN", "")
        self.magic = args.magic
        self.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        self.tf_names = [t.strip() for t in args.timeframes.split(",") if t.strip()]
        self.poll_ms = args.poll_ms
        self.sock: socket.socket | None = None
        self.rx = b""
        self.offset = 0
        self.last_bar_time: dict[tuple[str, str], int] = {}
        self.last_tick_ms: dict[str, int] = {}

    # -- terminal -------------------------------------------------------------------

    def start_terminal(self) -> None:
        if mt5 is None:
            raise SystemExit(
                "the MetaTrader5 package is not installed. It is Windows-only:\n"
                "    pip install MetaTrader5"
            )
        _init_timeframe_map()
        if not mt5.initialize():
            raise SystemExit(f"mt5.initialize() failed: {mt5.last_error()}")
        info = mt5.terminal_info()
        acct = mt5.account_info()
        if acct is None:
            raise SystemExit("no account is logged in to the terminal")
        if not info.trade_allowed:
            LOG.warning("algorithmic trading is DISABLED in the terminal; orders will fail")
        self.offset = server_offset_seconds()
        LOG.info("terminal build %s, account %s@%s, server offset %+.1f h",
                 info.build, acct.login, acct.server, self.offset / 3600)
        for symbol in self.symbols:
            if not mt5.symbol_select(symbol, True):
                LOG.error("symbol %s is not available at this broker (check for a suffix "
                          "such as .m or _i)", symbol)
        for symbol in self.symbols:
            for tf in self.tf_names:
                rates = mt5.copy_rates_from_pos(symbol, TIMEFRAMES[tf], 0, 1)
                if rates is not None and len(rates):
                    self.last_bar_time[(symbol, tf)] = int(rates[0]["time"])

    # -- socket ---------------------------------------------------------------------

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.settimeout(0.05)
        self.rx = b""
        self.send(self.hello())
        LOG.info("connected to ATLAS at %s:%s", self.host, self.port)

    def send(self, message: dict[str, Any]) -> None:
        if self.sock is None:
            return
        raw = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > MAX_FRAME_BYTES:
            LOG.error("refusing to send an oversized frame (%d bytes)", len(raw))
            return
        try:
            self.sock.sendall(raw)
        except OSError as exc:
            LOG.warning("send failed: %s", exc)
            self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def read_frames(self) -> list[dict[str, Any]]:
        if self.sock is None:
            return []
        try:
            chunk = self.sock.recv(65536)
            if not chunk:
                self.close()
                return []
            self.rx += chunk
        except TimeoutError:
            pass
        except OSError as exc:
            LOG.warning("recv failed: %s", exc)
            self.close()
            return []
        out: list[dict[str, Any]] = []
        while b"\n" in self.rx:
            line, self.rx = self.rx.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                LOG.error("bad frame from ATLAS: %s", exc)
        if len(self.rx) > MAX_FRAME_BYTES:
            LOG.error("receive buffer overflow; dropping the connection")
            self.close()
            self.rx = b""
        return out

    # -- protocol -------------------------------------------------------------------

    def hello(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION, "t": "hello", "ts": utc_now_ms(),
            "token": self.token, "impl": "python-sidecar",
            "build": int(getattr(mt5.terminal_info(), "build", 0)),
            "server_time_ms": utc_now_ms() + self.offset * 1000,
            "account": self.account_payload(),
            "symbols": self.symbols,
        }

    def account_payload(self) -> dict[str, Any]:
        a = mt5.account_info()
        t = mt5.terminal_info()
        if a is None:
            return {}
        return {
            "login": a.login, "server": a.server, "currency": a.currency,
            "balance": a.balance, "equity": a.equity, "margin": a.margin,
            "free_margin": a.margin_free, "margin_level": a.margin_level,
            "leverage": a.leverage, "ts": utc_now_ms(),
            "trade_allowed": bool(a.trade_expert and t and t.trade_allowed),
            "hedging": a.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        }

    def spec_payload(self, symbol: str) -> dict[str, Any] | None:
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        modes = []
        # SYMBOL_FILLING_MODE is a bit mask; sending a mode the symbol does not support is
        # retcode 10030 and is one of the most common cross-broker failures.
        if info.filling_mode & mt5.SYMBOL_FILLING_FOK:
            modes.append("FOK")
        if info.filling_mode & mt5.SYMBOL_FILLING_IOC:
            modes.append("IOC")
        modes.append("RETURN")
        return {
            "description": info.description, "digits": info.digits, "point": info.point,
            "tick_size": info.trade_tick_size, "tick_value": info.trade_tick_value,
            "contract_size": info.trade_contract_size,
            "volume_min": info.volume_min, "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "stops_level": info.trade_stops_level or max(int(info.spread) * 2, 10),
            "freeze_level": info.trade_freeze_level,
            "currency_base": info.currency_base, "currency_profit": info.currency_profit,
            "currency_margin": info.currency_margin,
            "margin_initial": info.margin_initial,
            "swap_long": info.swap_long, "swap_short": info.swap_short,
            "swap_mode": info.swap_mode,
            # MT5 counts weekdays from Sunday; ATLAS counts from Monday.
            "swap_rollover_3days": (int(info.swap_rollover3days) + 6) % 7,
            "trade_allowed": info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED,
            "filling_modes": modes,
        }

    def position_payload(self, pos) -> dict[str, Any]:
        return {
            "ticket": pos.ticket, "sym": pos.symbol,
            "side": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": pos.volume, "open_price": pos.price_open,
            "open_time": to_utc_ms(pos.time, self.offset),
            "sl": pos.sl, "tp": pos.tp, "price_current": pos.price_current,
            "profit": pos.profit, "swap": pos.swap, "commission": 0.0,
            "magic": pos.magic, "comment": pos.comment,
        }

    # -- command handling ------------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "welcome":
            LOG.info("handshake accepted by ATLAS")
        elif kind == "bye":
            LOG.error("ATLAS closed the session: %s", msg.get("reason"))
            self.close()
        elif kind == "ping":
            self.send({"v": 1, "t": "pong", "ts": utc_now_ms(), "id": msg.get("id")})
        elif kind == "req":
            self.handle_request(msg)
        # Unknown types are ignored: a newer ATLAS must be able to talk to an older sidecar.

    def handle_request(self, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        op = str(msg.get("op", ""))
        args = msg.get("args") or {}
        try:
            data = self.dispatch(op, args)
        except _OpError as exc:
            self.send({"v": 1, "t": "reply", "ts": utc_now_ms(), "id": req_id, "ok": False,
                       "error": {"code": exc.code, "message": str(exc),
                                 "retcode": exc.retcode}})
            return
        except Exception as exc:  # never let one bad command kill the bridge
            LOG.exception("op %s failed", op)
            self.send({"v": 1, "t": "reply", "ts": utc_now_ms(), "id": req_id, "ok": False,
                       "error": {"code": "SIDECAR_ERROR", "message": str(exc), "retcode": 0}})
            return
        self.send({"v": 1, "t": "reply", "ts": utc_now_ms(), "id": req_id, "ok": True,
                   "data": data})

    def dispatch(self, op: str, args: dict[str, Any]) -> Any:
        if op == "account":
            return self.account_payload()
        if op == "specs":
            wanted = args.get("symbols") or self.symbols
            out = {}
            for s in wanted:
                payload = self.spec_payload(s)
                if payload is not None:
                    out[s] = payload
            if not out:
                raise _OpError("UNKNOWN_SYMBOL",
                               "none of the requested symbols exist at this broker")
            return out
        if op == "quote":
            tick = mt5.symbol_info_tick(args["sym"])
            if tick is None:
                raise _OpError("NO_QUOTES", f"no tick for {args['sym']}")
            return {"bid": tick.bid, "ask": tick.ask, "ts": to_utc_ms(tick.time, self.offset)}
        if op == "positions":
            return [self.position_payload(p_) for p_ in (mt5.positions_get() or [])
                    if p_.magic == self.magic]
        if op == "orders":
            out = []
            for o in mt5.orders_get() or []:
                if o.magic != self.magic:
                    continue
                is_buy = o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP)
                kind = "LIMIT" if o.type in (mt5.ORDER_TYPE_BUY_LIMIT,
                                             mt5.ORDER_TYPE_SELL_LIMIT) else "STOP"
                out.append({"ticket": o.ticket, "sym": o.symbol,
                            "side": "BUY" if is_buy else "SELL", "type": kind,
                            "volume": o.volume_current, "price": o.price_open,
                            "sl": o.sl, "tp": o.tp,
                            "setup_time": to_utc_ms(o.time_setup, self.offset),
                            "magic": o.magic, "comment": o.comment})
            return out
        if op == "bars":
            tf = TIMEFRAMES.get(str(args.get("tf", "M5")))
            if tf is None:
                raise _OpError("INVALID_ARGS", f"unknown timeframe {args.get('tf')}")
            # Start at index 1: index 0 is the bar still forming, and shipping it as history
            # is the classic multi-timeframe look-ahead bug.
            rates = mt5.copy_rates_from_pos(args["sym"], tf, 1, int(args.get("count", 500)))
            if rates is None:
                raise _OpError("NO_HISTORY", f"copy_rates returned nothing: {mt5.last_error()}")
            return [[to_utc_ms(r["time"], self.offset), float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"]), int(r["tick_volume"]),
                     int(r["spread"])] for r in rates]
        if op == "order_send":
            return self.order_send(args)
        if op == "position_modify":
            return self.position_modify(args)
        if op == "position_close":
            return self.position_close(args)
        if op == "order_cancel":
            return self.order_cancel(args)
        if op == "find_by_comment":
            comment = str(args.get("comment", ""))
            if not comment:
                return None
            for pos in mt5.positions_get() or []:
                if pos.magic == self.magic and comment in (pos.comment or ""):
                    return self.position_payload(pos)
            return None
        if op == "history_deals":
            return self.history_deals(args)
        raise _OpError("UNKNOWN_OP", f"this sidecar does not implement '{op}'")

    # -- trading ---------------------------------------------------------------------

    def _normalize_volume(self, symbol: str, volume: float) -> float:
        info = mt5.symbol_info(symbol)
        if info is None:
            return 0.0
        step = info.volume_step or 0.01
        # Round DOWN, never to nearest: rounding up silently exceeds the risk budget.
        steps = int((volume + 1e-9) / step)
        vol = round(steps * step, 8)
        if vol < info.volume_min:
            return 0.0
        return min(vol, info.volume_max)

    def _filling(self, symbol: str, requested: str) -> int:
        info = mt5.symbol_info(symbol)
        mask = info.filling_mode if info else 0
        if requested == "FOK" and mask & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if requested == "IOC" and mask & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if mask & mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if mask & mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def order_send(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = args["sym"]
        info = mt5.symbol_info(symbol)
        if info is None:
            raise _OpError("UNKNOWN_SYMBOL", f"{symbol} is not available at this broker")
        volume = self._normalize_volume(symbol, float(args["volume"]))
        if volume <= 0:
            raise _OpError("INVALID_VOLUME",
                           f"volume {args['volume']} is below the {info.volume_min} minimum",
                           retcode=10014)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise _OpError("NO_QUOTES", f"no tick for {symbol}", retcode=10021)

        is_buy = args["side"] == "BUY"
        otype = args.get("type", "MARKET")
        reference = tick.ask if is_buy else tick.bid
        request: dict[str, Any] = {
            "symbol": symbol, "volume": volume,
            "magic": int(args.get("magic", self.magic)),
            "comment": str(args.get("comment", ""))[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(symbol, str(args.get("filling", "IOC"))),
        }
        if otype == "MARKET":
            request.update({
                "action": mt5.TRADE_ACTION_DEAL,
                "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price": reference,
                "deviation": int(args.get("deviation", 20)),
            })
        else:
            price = args.get("price")
            if not price:
                raise _OpError("INVALID_PRICE", "a pending order requires a price",
                               retcode=10015)
            reference = float(price)
            if otype == "LIMIT":
                order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
            else:
                order_type = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
            request.update({"action": mt5.TRADE_ACTION_PENDING, "type": order_type,
                            "price": reference})

        stops_level = info.trade_stops_level or max(int(info.spread) * 2, 10)
        for name, key in (("stop loss", "sl"), ("take profit", "tp")):
            level = args.get(key)
            if not level:
                continue
            distance = abs(reference - float(level)) / info.point
            if distance < stops_level:
                raise _OpError(
                    "INVALID_STOPS",
                    f"{name} is {distance:.0f} points away, inside the "
                    f"{stops_level}-point stops level",
                    retcode=10016,
                )
            request[key] = float(level)

        result = mt5.order_send(request)
        if result is None:
            raise _OpError("SEND_FAILED", f"order_send returned None: {mt5.last_error()}")
        return {
            "retcode": int(result.retcode), "retcode_text": str(result.comment),
            "order": int(result.order), "deal": int(result.deal),
            "position": int(result.order), "volume": float(result.volume),
            "price": float(result.price), "requested_price": reference,
            "comment": str(result.comment),
        }

    def _own_position(self, ticket: int):
        found = mt5.positions_get(ticket=ticket)
        if not found:
            raise _OpError("POSITION_NOT_FOUND", f"no open position with ticket {ticket}")
        pos = found[0]
        if pos.magic != self.magic:
            raise _OpError("NOT_OURS",
                           "the position belongs to a different magic number")
        return pos

    def position_modify(self, args: dict[str, Any]) -> dict[str, Any]:
        pos = self._own_position(int(args["ticket"]))
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket,
                   "symbol": pos.symbol,
                   "sl": float(args["sl"]) if args.get("sl") else pos.sl,
                   "tp": float(args["tp"]) if args.get("tp") else pos.tp}
        result = mt5.order_send(request)
        if result is None:
            raise _OpError("SEND_FAILED", f"order_send returned None: {mt5.last_error()}")
        return {"retcode": int(result.retcode), "retcode_text": str(result.comment)}

    def position_close(self, args: dict[str, Any]) -> dict[str, Any]:
        pos = self._own_position(int(args["ticket"]))
        volume = args.get("volume")
        vol = self._normalize_volume(pos.symbol, float(volume)) if volume else pos.volume
        vol = min(vol or pos.volume, pos.volume)
        tick = mt5.symbol_info_tick(pos.symbol)
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "position": pos.ticket, "symbol": pos.symbol,
            "volume": vol,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": int(args.get("deviation", 50)), "magic": self.magic,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(pos.symbol, "IOC"),
        }
        result = mt5.order_send(request)
        if result is None:
            raise _OpError("SEND_FAILED", f"order_send returned None: {mt5.last_error()}")
        return {"retcode": int(result.retcode), "retcode_text": str(result.comment),
                "deal": int(result.deal), "price": float(result.price),
                "volume": float(result.volume)}

    def order_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        ticket = int(args["ticket"])
        found = mt5.orders_get(ticket=ticket)
        if not found:
            raise _OpError("ORDER_NOT_FOUND", f"no pending order with ticket {ticket}")
        if found[0].magic != self.magic:
            raise _OpError("NOT_OURS", "the order belongs to a different magic number")
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        if result is None:
            raise _OpError("SEND_FAILED", f"order_send returned None: {mt5.last_error()}")
        return {"retcode": int(result.retcode), "retcode_text": str(result.comment)}

    def history_deals(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Pair opening and closing deals into round trips.

        Pairing happens here because MT5 exposes ``position_id`` on the deal; re-deriving that
        association on the engine side from a partial view would be guesswork.
        """
        from_ms = int(args.get("from_ms", 0)) or (utc_now_ms() - 30 * 86_400_000)
        start = datetime.fromtimestamp(from_ms / 1000 + self.offset, tz=UTC)
        end = datetime.fromtimestamp(time.time() + 86_400, tz=UTC)
        deals = mt5.history_deals_get(start, end) or []
        opens: dict[int, Any] = {}
        out: list[dict[str, Any]] = []
        for d in deals:
            if d.magic != self.magic:
                continue
            if d.entry == mt5.DEAL_ENTRY_IN:
                opens[d.position_id] = d
        for d in deals:
            if d.magic != self.magic or d.entry != mt5.DEAL_ENTRY_OUT:
                continue
            opener = opens.get(d.position_id)
            if opener is None:
                continue
            out.append({
                "trade_id": str(d.position_id), "position": d.position_id, "closed": True,
                "sym": d.symbol,
                # The CLOSING deal of a long is a sell, so the side is inverted here.
                "side": "BUY" if d.type == mt5.DEAL_TYPE_SELL else "SELL",
                "volume": d.volume,
                "entry_price": opener.price,
                "entry_time": to_utc_ms(opener.time, self.offset),
                "exit_price": d.price, "exit_time": to_utc_ms(d.time, self.offset),
                "profit": d.profit, "commission": d.commission + opener.commission,
                "swap": d.swap, "comment": opener.comment,
            })
        return out

    # -- streaming --------------------------------------------------------------------

    def publish(self) -> None:
        now = utc_now_ms()
        for symbol in self.symbols:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            if now - self.last_tick_ms.get(symbol, 0) >= self.poll_ms:
                self.send({"v": 1, "t": "tick", "ts": to_utc_ms(tick.time, self.offset),
                           "sym": symbol, "bid": tick.bid, "ask": tick.ask})
                self.last_tick_ms[symbol] = now
            for tf in self.tf_names:
                rates = mt5.copy_rates_from_pos(symbol, TIMEFRAMES[tf], 0, 2)
                if rates is None or len(rates) < 2:
                    continue
                current_open = int(rates[1]["time"])
                key = (symbol, tf)
                previous = self.last_bar_time.get(key)
                if previous is None:
                    self.last_bar_time[key] = current_open
                    continue
                if current_open == previous:
                    continue
                self.last_bar_time[key] = current_open
                closed = rates[0]
                self.send({"v": 1, "t": "bar", "ts": now, "sym": symbol, "tf": tf,
                           "open_time": to_utc_ms(closed["time"], self.offset),
                           "o": float(closed["open"]), "h": float(closed["high"]),
                           "l": float(closed["low"]), "c": float(closed["close"]),
                           "vol": int(closed["tick_volume"]), "spread": int(closed["spread"])})

    # -- main loop ---------------------------------------------------------------------

    def run(self) -> None:
        self.start_terminal()
        backoff = 1
        while True:
            if self.sock is None:
                try:
                    self.connect()
                    backoff = 1
                except OSError as exc:
                    LOG.warning("cannot reach ATLAS at %s:%s (%s); retrying in %ds. "
                                "ATLAS listens; the sidecar connects out.",
                                self.host, self.port, exc, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
            for msg in self.read_frames():
                self.handle(msg)
            if self.sock is None:
                continue
            try:
                self.publish()
            except Exception:  # a streaming hiccup must not drop the session
                LOG.exception("publish failed")
            time.sleep(self.poll_ms / 1000.0)


class _OpError(Exception):
    def __init__(self, code: str, message: str, retcode: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.retcode = retcode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--token", default="")
    parser.add_argument("--magic", type=int, default=20260823)
    parser.add_argument("--symbols", default="XAUUSD")
    parser.add_argument("--timeframes", default="M5,M15,H1,H4")
    parser.add_argument("--poll-ms", type=int, default=250,
                        help="polling interval; the MetaTrader5 package has no push API")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sidecar = Sidecar(args)
    try:
        sidecar.run()
    except KeyboardInterrupt:
        LOG.info("stopping")
    finally:
        sidecar.close()
        if mt5 is not None:
            mt5.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
