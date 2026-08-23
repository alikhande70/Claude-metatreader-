"""Simulated execution venue.

Not a stub (ADR-006). It models bid/ask, commission per side, adverse entry and stop-fill
slippage scaled by spread, swap accrual with the broker's triple-swap day, margin and
stop-out, pending-order triggering, and partial closes. Its purpose is to be pessimistic
enough that a strategy which survives it has a chance live.

Fills are asynchronous (ADR-017): a market order submitted at time ``T`` is filled on the
first quote at or after ``T``, which in bar replay is the next bar's open. Intrabar ambiguity
is resolved against the trader (ADR-018).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from atlas.core.enums import (
    ExitReason,
    OrderStatus,
    OrderType,
    Side,
)
from atlas.core.errors import VenueError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Quote, ms_to_dt
from atlas.core.trading import (
    AccountState,
    OrderRequest,
    OrderResult,
    PendingOrder,
    Position,
    Trade,
)
from atlas.execution.venue import ExecutionVenue, VenueCapabilities, VenueHealth
from atlas.venues.sim.costs import CostModel

_RETCODE_DONE = 10009
_RETCODE_REJECT = 10006
_RETCODE_INVALID_STOPS = 10016
_RETCODE_INVALID_VOLUME = 10014
_RETCODE_NO_MONEY = 10019


@dataclass(slots=True)
class SimPosition:
    ticket: int
    symbol: str
    side: Side
    volume: float
    open_price: float
    open_time: int
    stop_loss: float | None
    take_profit: float | None
    initial_stop: float
    initial_target: float | None
    commission: float
    swap: float = 0.0
    magic: int = 0
    comment: str = ""
    client_order_id: str = ""
    decision_id: str = ""
    strategy: str = ""
    current_price: float = 0.0
    mae_points: float = 0.0
    mfe_points: float = 0.0
    last_swap_day: str = ""

    def to_position(self, spec: SymbolSpec) -> Position:
        profit = (
            (self.current_price - self.open_price) * self.side.sign
            / spec.point * spec.value_per_point_per_lot * self.volume
        )
        return Position(
            ticket=self.ticket, symbol=self.symbol, side=self.side, volume=self.volume,
            open_price=self.open_price, open_time=self.open_time, stop_loss=self.stop_loss,
            take_profit=self.take_profit, current_price=self.current_price, profit=profit,
            swap=self.swap, commission=self.commission, magic=self.magic,
            comment=self.comment,
        )

    def floating(self, spec: SymbolSpec) -> float:
        return (
            (self.current_price - self.open_price) * self.side.sign
            / spec.point * spec.value_per_point_per_lot * self.volume
        )


@dataclass(slots=True)
class SimConfig:
    starting_balance: float = 10_000.0
    currency: str = "USD"
    leverage: int = 100
    stop_out_level_pct: float = 50.0
    margin_call_level_pct: float = 100.0
    seed: int = 1234
    login: int = 999_000
    server: str = "AtlasSim"


class SimulatedVenue(ExecutionVenue):
    def __init__(
        self,
        specs: dict[str, SymbolSpec],
        *,
        config: SimConfig | None = None,
        costs: CostModel | None = None,
        on_fill: Callable[[SimPosition, OrderResult], None] | None = None,
        on_close: Callable[[Trade], None] | None = None,
    ) -> None:
        self.specs = dict(specs)
        self.cfg = config or SimConfig()
        self.costs = costs or CostModel()
        self._rng = np.random.default_rng(self.cfg.seed)
        self._on_fill = on_fill
        self._on_close = on_close

        self.balance = self.cfg.starting_balance
        self._positions: dict[int, SimPosition] = {}
        self._pending: dict[int, tuple[PendingOrder, OrderRequest]] = {}
        self._queued_market: list[OrderRequest] = []
        self._quotes: dict[str, Quote] = {}
        self._next_ticket = 1
        self._now = 0
        self._connected = False
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[int, float, float]] = []  # (ts, equity, balance)
        self.rejections: list[tuple[int, str, str]] = []
        #: client_order_id -> ticket, retained after close so idempotency lookups still work.
        self._by_client_id: dict[str, int] = {}
        self._closed_client_ids: set[str] = set()

    # -- venue plumbing ------------------------------------------------------------

    @property
    def capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            streaming_quotes=True, streaming_trade_events=True, pending_orders=True,
            partial_close=True, client_id_lookup=True, name="simulator",
        )

    async def connect(self) -> VenueHealth:
        self._connected = True
        return await self.health()

    async def disconnect(self) -> None:
        self._connected = False

    async def health(self) -> VenueHealth:
        return VenueHealth(connected=self._connected, trade_allowed=self._connected,
                           server_time_ms=self._now, latency_ms=0.0, detail="simulator",
                           server_offset_seconds=0)

    async def symbol_specs(self, symbols: list[str]) -> dict[str, SymbolSpec]:
        return {s: self.specs[s] for s in symbols if s in self.specs}

    async def quote(self, symbol: str) -> Quote:
        q = self._quotes.get(symbol)
        if q is None:
            raise VenueError(f"no quote yet for {symbol}")
        return q

    async def account(self) -> AccountState:
        return self.account_state()

    async def positions(self) -> list[Position]:
        return [p.to_position(self.specs[p.symbol]) for p in self._positions.values()]

    async def pending_orders(self) -> list[PendingOrder]:
        return [po for po, _ in self._pending.values()]

    async def closed_trades(self, since_ms: int) -> list[Trade]:
        return [t for t in self.trades if t.exit_time >= since_ms]

    def observe_quote(self, quote: Quote) -> None:
        self.on_quote(quote)

    async def find_by_client_id(self, client_order_id: str) -> Position | None:
        ticket = self._by_client_id.get(client_order_id)
        if ticket is None:
            return None
        p = self._positions.get(ticket)
        return p.to_position(self.specs[p.symbol]) if p else None

    def was_filled(self, client_order_id: str) -> bool:
        """True if this client id ever produced a fill, open or closed.

        Distinct from ``find_by_client_id``, which only sees currently-open positions. A
        retry decision needs both: an order that filled and has since closed must not be
        resubmitted.
        """
        return client_order_id in self._by_client_id or client_order_id in self._closed_client_ids

    # -- account -------------------------------------------------------------------

    def account_state(self) -> AccountState:
        floating = sum(p.floating(self.specs[p.symbol]) + p.swap for p in self._positions.values())
        equity = self.balance + floating
        margin = sum(self._margin_for(p) for p in self._positions.values())
        free = equity - margin
        level = (equity / margin * 100.0) if margin > 0 else 0.0
        return AccountState(
            login=self.cfg.login, server=self.cfg.server, currency=self.cfg.currency,
            balance=round(self.balance, 2), equity=round(equity, 2), margin=round(margin, 2),
            free_margin=round(free, 2), margin_level=round(level, 2),
            leverage=self.cfg.leverage, ts=self._now, trade_allowed=self._connected,
        )

    def _margin_for(self, p: SimPosition) -> float:
        spec = self.specs[p.symbol]
        if spec.margin_initial > 0:
            return spec.margin_initial * p.volume
        return spec.contract_size * p.volume / max(1, self.cfg.leverage)

    # -- market clock --------------------------------------------------------------

    def on_quote(self, q: Quote) -> None:
        """Advance the simulation to a new quote.

        Order of operations is deliberate and is the core of the simulator's honesty:
        queued market orders fill first (at this quote, not the previous one), then pending
        orders trigger, then open positions are marked and checked for stop/target, and the
        stop is always checked before the target.
        """
        self._quotes[q.symbol] = q
        self._now = max(self._now, q.ts)
        self._fill_queued_market(q)
        self._trigger_pending(q)
        self._mark_and_resolve(q)
        self._accrue_swap(q)
        self._check_stop_out(q)
        acct = self.account_state()
        self.equity_curve.append((q.ts, acct.equity, acct.balance))

    # -- order entry ---------------------------------------------------------------

    async def submit(self, request: OrderRequest) -> OrderResult:
        spec = self.specs.get(request.symbol)
        if spec is None:
            return self._reject(request, _RETCODE_REJECT, f"unknown symbol {request.symbol}")

        vol = spec.normalize_volume(request.volume)
        if vol <= 0:
            return self._reject(request, _RETCODE_INVALID_VOLUME,
                                f"volume {request.volume} is below the {spec.volume_min} minimum")
        # MT5 rejects an off-grid volume outright rather than rounding it. Reproducing that
        # strictly (rather than tolerating half a step) removes an ambiguous boundary and
        # forces callers to normalise, which is what they must do live anyway.
        if abs(vol - request.volume) > spec.volume_step * 1e-6:
            return self._reject(
                request, _RETCODE_INVALID_VOLUME,
                f"volume {request.volume} is not a multiple of the {spec.volume_step} lot step",
            )

        q = self._quotes.get(request.symbol)
        if q is None:
            return self._reject(request, _RETCODE_REJECT, "no market data for this symbol yet")

        if request.order_type is OrderType.MARKET:
            ref: float | None = q.price_for(str(request.side))
        else:
            ref = request.price
        if ref is None:
            return self._reject(request, _RETCODE_REJECT, "pending order requires a price")
        stops_err = self._validate_stops(spec, request, ref)
        if stops_err:
            return self._reject(request, _RETCODE_INVALID_STOPS, stops_err)

        if request.order_type is OrderType.MARKET:
            self._queued_market.append(request.model_copy(update={"volume": vol}))
            return OrderResult(
                client_order_id=request.client_order_id, accepted=True,
                status=OrderStatus.PENDING_NEW, retcode=_RETCODE_DONE,
                retcode_text="queued for the next quote", requested_price=ref, ts=self._now,
                message="market order accepted; fills on the next quote (ADR-017)",
            )

        ticket = self._take_ticket()
        po = PendingOrder(
            ticket=ticket, symbol=request.symbol, side=request.side,
            order_type=request.order_type, volume=vol, price=spec.normalize_price(ref),
            stop_loss=request.stop_loss, take_profit=request.take_profit,
            setup_time=self._now, expiry_ms=request.expiry_ms, magic=request.magic,
            comment=request.comment,
        )
        self._pending[ticket] = (po, request.model_copy(update={"volume": vol}))
        return OrderResult(
            client_order_id=request.client_order_id, accepted=True, status=OrderStatus.WORKING,
            retcode=_RETCODE_DONE, retcode_text="pending order placed", order_ticket=ticket,
            requested_price=po.price, ts=self._now,
        )

    def _validate_stops(self, spec: SymbolSpec, req: OrderRequest, ref: float) -> str | None:
        """Reproduce MT5's ``TRADE_RETCODE_INVALID_STOPS`` rather than silently accepting.

        A simulator that accepts a stop the broker would reject produces trades that cannot
        exist, which is a class of backtest inflation that is easy to miss.
        """
        min_pts = spec.min_stop_distance_points()
        for name, level, must_be_below in (
            ("stop_loss", req.stop_loss, req.side is Side.BUY),
            ("take_profit", req.take_profit, req.side is Side.SELL),
        ):
            if level is None:
                continue
            if must_be_below and level >= ref:
                return f"{name} {level} is not below the {req.side} reference price {ref}"
            if not must_be_below and level <= ref:
                return f"{name} {level} is not above the {req.side} reference price {ref}"
            dist = spec.points_between(ref, level)
            if dist < min_pts:
                return (f"{name} is {dist:.0f} points away, inside the broker's "
                        f"{min_pts:.0f}-point stops level")
        return None

    def _reject(self, req: OrderRequest, retcode: int, message: str) -> OrderResult:
        self.rejections.append((self._now, req.client_order_id, message))
        return OrderResult(
            client_order_id=req.client_order_id, accepted=False, status=OrderStatus.REJECTED,
            retcode=retcode, retcode_text=message, ts=self._now, message=message,
        )

    # -- fills ---------------------------------------------------------------------

    def _fill_queued_market(self, q: Quote) -> None:
        if not self._queued_market:
            return
        due = [r for r in self._queued_market if r.symbol == q.symbol]
        self._queued_market = [r for r in self._queued_market if r.symbol != q.symbol]
        for req in due:
            spec = self.specs[req.symbol]
            if self.costs.rejected(self._rng):
                self.rejections.append((q.ts, req.client_order_id, "simulated requote/reject"))
                continue
            spread_pts = q.spread_points(spec.point)
            slip = self.costs.entry_slippage(self._rng, spread_pts)
            base = q.price_for(str(req.side))
            fill = spec.normalize_price(base + req.side.sign * slip * spec.point)

            if req.deviation_points and slip > req.deviation_points:
                self.rejections.append(
                    (q.ts, req.client_order_id,
                     f"slippage {slip:.1f} pts exceeded the {req.deviation_points}-pt deviation")
                )
                continue
            self._open_position(req, fill, q, spec)

    def _open_position(
        self, req: OrderRequest, fill: float, q: Quote, spec: SymbolSpec
    ) -> SimPosition | None:
        margin_needed = (
            spec.margin_initial * req.volume if spec.margin_initial > 0
            else spec.contract_size * req.volume / max(1, self.cfg.leverage)
        )
        acct = self.account_state()
        if margin_needed > acct.free_margin:
            self.rejections.append(
                (q.ts, req.client_order_id,
                 f"insufficient margin: need {margin_needed:.2f}, free {acct.free_margin:.2f}")
            )
            return None
        commission = self.costs.commission_one_side(req.volume)
        ticket = self._take_ticket()
        pos = SimPosition(
            ticket=ticket, symbol=req.symbol, side=req.side, volume=req.volume,
            open_price=fill, open_time=q.ts, stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            initial_stop=req.stop_loss if req.stop_loss is not None else fill,
            initial_target=req.take_profit, commission=commission, magic=req.magic,
            comment=req.comment, client_order_id=req.client_order_id,
            decision_id=req.decision_id, current_price=q.exit_price_for(str(req.side)),
            last_swap_day=ms_to_dt(q.ts).strftime("%Y-%m-%d"),
        )
        self._positions[ticket] = pos
        self._by_client_id[req.client_order_id] = ticket
        result = OrderResult(
            client_order_id=req.client_order_id, accepted=True, status=OrderStatus.FILLED,
            retcode=_RETCODE_DONE, retcode_text="filled", order_ticket=ticket,
            deal_ticket=ticket, position_ticket=ticket, filled_volume=req.volume,
            fill_price=fill, requested_price=q.price_for(str(req.side)),
            commission=commission, ts=q.ts,
        )
        if self._on_fill is not None:
            self._on_fill(pos, result)
        return pos

    def _trigger_pending(self, q: Quote) -> None:
        for ticket, (po, req) in list(self._pending.items()):
            if po.symbol != q.symbol:
                continue
            if po.expiry_ms and q.ts >= po.expiry_ms:
                del self._pending[ticket]
                continue
            px = q.price_for(str(po.side))
            triggered = (
                (po.order_type is OrderType.STOP and (
                    (po.side is Side.BUY and px >= po.price) or
                    (po.side is Side.SELL and px <= po.price)))
                or (po.order_type is OrderType.LIMIT and (
                    (po.side is Side.BUY and px <= po.price) or
                    (po.side is Side.SELL and px >= po.price)))
            )
            if not triggered:
                continue
            del self._pending[ticket]
            spec = self.specs[po.symbol]
            spread_pts = q.spread_points(spec.point)
            # Stop orders slip like stops (they trigger into one-way moves); limit orders
            # fill at their price or better, so they get no adverse slippage.
            is_stop_order = po.order_type is OrderType.STOP
            slip = self.costs.stop_slippage(self._rng, spread_pts) if is_stop_order else 0.0
            fill = spec.normalize_price(po.price + po.side.sign * slip * spec.point)
            self._open_position(req, fill, q, spec)

    # -- position lifecycle ----------------------------------------------------------

    def _mark_and_resolve(self, q: Quote) -> None:
        for pos in list(self._positions.values()):
            if pos.symbol != q.symbol:
                continue
            spec = self.specs[pos.symbol]
            exit_px = q.exit_price_for(str(pos.side))
            pos.current_price = exit_px
            move_pts = (exit_px - pos.open_price) * pos.side.sign / spec.point
            pos.mae_points = min(pos.mae_points, move_pts)
            pos.mfe_points = max(pos.mfe_points, move_pts)

            # ADR-018: the stop is always checked before the target.
            if pos.stop_loss is not None and self._hit(pos.side, exit_px, pos.stop_loss, stop=True):
                slip = self.costs.stop_slippage(self._rng, q.spread_points(spec.point))
                fill = spec.normalize_price(pos.stop_loss - pos.side.sign * slip * spec.point)
                self._close(pos, fill, q.ts, ExitReason.STOP_LOSS)
                continue
            if pos.take_profit is not None and self._hit(
                pos.side, exit_px, pos.take_profit, stop=False
            ):
                self._close(
                    pos, spec.normalize_price(pos.take_profit), q.ts, ExitReason.TAKE_PROFIT
                )

    @staticmethod
    def _hit(side: Side, price: float, level: float, *, stop: bool) -> bool:
        if stop:
            return price <= level if side is Side.BUY else price >= level
        return price >= level if side is Side.BUY else price <= level

    def _close(
        self, pos: SimPosition, price: float, ts: int, reason: ExitReason,
        volume: float | None = None,
    ) -> Trade:
        spec = self.specs[pos.symbol]
        vol = pos.volume if volume is None else min(volume, pos.volume)
        frac = vol / pos.volume if pos.volume else 1.0
        gross = (
            (price - pos.open_price) * pos.side.sign
            / spec.point * spec.value_per_point_per_lot * vol
        )
        commission = pos.commission * frac + self.costs.commission_one_side(vol)
        swap = pos.swap * frac
        self.balance += gross + commission + swap

        trade = Trade(
            trade_id=f"T{pos.ticket}" + ("" if volume is None else f"-{ts}"),
            decision_id=pos.decision_id, symbol=pos.symbol, side=pos.side, volume=vol,
            entry_price=pos.open_price, entry_time=pos.open_time, exit_price=price,
            exit_time=ts, initial_stop=pos.initial_stop, initial_target=pos.initial_target,
            risk_money=spec.money_for_points(
                spec.points_between(pos.open_price, pos.initial_stop), vol
            ),
            gross_profit=gross, commission=commission, swap=swap, exit_reason=reason,
            mae_points=abs(pos.mae_points), mfe_points=pos.mfe_points, strategy=pos.strategy,
            point=spec.point,
        )
        self.trades.append(trade)
        if volume is None or vol >= pos.volume:
            self._positions.pop(pos.ticket, None)
            self._by_client_id.pop(pos.client_order_id, None)
            self._closed_client_ids.add(pos.client_order_id)
        else:
            pos.volume -= vol
            pos.commission *= 1 - frac
            pos.swap *= 1 - frac
        if self._on_close is not None:
            self._on_close(trade)
        return trade

    def _accrue_swap(self, q: Quote) -> None:
        day = ms_to_dt(q.ts).strftime("%Y-%m-%d")
        for pos in self._positions.values():
            if pos.last_swap_day == day or pos.symbol != q.symbol:
                continue
            spec = self.specs[pos.symbol]
            pos.swap += self.costs.swap_charge(
                spec, pos.volume, pos.side is Side.BUY, ms_to_dt(q.ts).weekday()
            )
            pos.last_swap_day = day

    def _check_stop_out(self, q: Quote) -> None:
        acct = self.account_state()
        if acct.margin <= 0 or acct.margin_level >= self.cfg.stop_out_level_pct:
            return
        # Brokers close the largest loser first; repeat until back above the stop-out level.
        while self._positions:
            worst = min(self._positions.values(), key=lambda p: p.floating(self.specs[p.symbol]))
            self._close(worst, worst.current_price, q.ts, ExitReason.STOP_OUT)
            acct = self.account_state()
            if acct.margin <= 0 or acct.margin_level >= self.cfg.stop_out_level_pct:
                return

    # -- modification ----------------------------------------------------------------

    async def modify_position(
        self, ticket: int, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> OrderResult:
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(client_order_id="", accepted=False, status=OrderStatus.REJECTED,
                               retcode=_RETCODE_REJECT, retcode_text="position not found",
                               ts=self._now)
        spec = self.specs[pos.symbol]
        q = self._quotes.get(pos.symbol)
        ref = q.exit_price_for(str(pos.side)) if q else pos.current_price
        for name, level, must_be_below in (
            ("stop_loss", stop_loss, pos.side is Side.BUY),
            ("take_profit", take_profit, pos.side is Side.SELL),
        ):
            if level is None:
                continue
            ok_side = level < ref if must_be_below else level > ref
            if not ok_side or spec.points_between(ref, level) < spec.min_stop_distance_points():
                return OrderResult(
                    client_order_id=pos.client_order_id, accepted=False,
                    status=OrderStatus.REJECTED, retcode=_RETCODE_INVALID_STOPS,
                    retcode_text=f"{name} {level} violates the stops level around {ref}",
                    position_ticket=ticket, ts=self._now,
                )
        if stop_loss is not None:
            pos.stop_loss = spec.normalize_price(stop_loss)
        if take_profit is not None:
            pos.take_profit = spec.normalize_price(take_profit)
        return OrderResult(client_order_id=pos.client_order_id, accepted=True,
                           status=OrderStatus.FILLED, retcode=_RETCODE_DONE,
                           retcode_text="modified", position_ticket=ticket, ts=self._now)

    async def close_position(
        self, ticket: int, *, volume: float | None = None,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> OrderResult:
        pos = self._positions.get(ticket)
        if pos is None:
            return OrderResult(client_order_id="", accepted=False, status=OrderStatus.REJECTED,
                               retcode=_RETCODE_REJECT, retcode_text="position not found",
                               ts=self._now)
        q = self._quotes.get(pos.symbol)
        if q is None:
            return OrderResult(client_order_id=pos.client_order_id, accepted=False,
                               status=OrderStatus.REJECTED, retcode=_RETCODE_REJECT,
                               retcode_text="no market data", ts=self._now)
        spec = self.specs[pos.symbol]
        slip = self.costs.entry_slippage(self._rng, q.spread_points(spec.point))
        px = spec.normalize_price(
            q.exit_price_for(str(pos.side)) - pos.side.sign * slip * spec.point
        )
        trade = self._close(pos, px, q.ts, reason, volume)
        return OrderResult(
            client_order_id=pos.client_order_id, accepted=True, status=OrderStatus.FILLED,
            retcode=_RETCODE_DONE, retcode_text="closed", position_ticket=ticket,
            filled_volume=trade.volume, fill_price=px, ts=q.ts,
        )

    async def cancel_order(self, ticket: int) -> OrderResult:
        if ticket not in self._pending:
            return OrderResult(client_order_id="", accepted=False, status=OrderStatus.REJECTED,
                               retcode=_RETCODE_REJECT, retcode_text="order not found",
                               ts=self._now)
        self._pending.pop(ticket)
        return OrderResult(client_order_id="", accepted=True, status=OrderStatus.CANCELLED,
                           retcode=_RETCODE_DONE, retcode_text="cancelled",
                           order_ticket=ticket, ts=self._now)

    # -- helpers ---------------------------------------------------------------------

    def _take_ticket(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def sim_position(self, ticket: int) -> SimPosition | None:
        return self._positions.get(ticket)

    def open_sim_positions(self) -> list[SimPosition]:
        return list(self._positions.values())
