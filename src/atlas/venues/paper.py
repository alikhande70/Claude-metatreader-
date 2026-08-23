"""Paper venue: live market data, simulated execution.

Paper trading offline is just a backtest, and ``atlas backtest`` already does that better.
The useful meaning of "paper" is **real market data with pretend money**: the same quotes,
the same spreads, the same session behaviour and the same news spikes the live system would
face, with orders routed to the simulator instead of the broker.

So this composes the two venues rather than replacing either:

* **market state** -- symbol specifications, quotes, bars, server clock -- comes from the
  real terminal, because those are exactly the things that differ between a broker and an
  assumption;
* **everything that touches money** -- account, positions, orders, fills, history -- comes
  from the simulator.

That split is also what makes a paper session a genuine test of the cost model: the spreads
are real, so the realised slippage the simulator produces from them is comparable with what
live would do.
"""

from __future__ import annotations

from atlas.core.enums import ExitReason, Timeframe
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
from atlas.venues.sim.venue import SimulatedVenue


class PaperVenue(ExecutionVenue):
    def __init__(self, data: ExecutionVenue, execution: SimulatedVenue) -> None:
        self.data = data
        self.execution = execution

    @property
    def capabilities(self) -> VenueCapabilities:
        upstream = self.data.capabilities
        return VenueCapabilities(
            streaming_quotes=upstream.streaming_quotes,
            streaming_trade_events=True, pending_orders=True, partial_close=True,
            client_id_lookup=True, name="paper",
            extra={"data": upstream.name, "execution": "simulator"},
        )

    # -- lifecycle: the data side owns the connection ------------------------------

    async def connect(self) -> VenueHealth:
        health = await self.data.connect()
        await self.execution.connect()
        return VenueHealth(
            connected=health.connected,
            trade_allowed=health.connected,  # the simulator always accepts; data must be live
            server_time_ms=health.server_time_ms, latency_ms=health.latency_ms,
            detail=f"paper: data from {self.data.capabilities.name} ({health.detail})",
            server_offset_seconds=health.server_offset_seconds,
        )

    async def disconnect(self) -> None:
        await self.execution.disconnect()
        await self.data.disconnect()

    async def health(self) -> VenueHealth:
        health = await self.data.health()
        return VenueHealth(
            connected=health.connected, trade_allowed=health.connected,
            server_time_ms=health.server_time_ms, latency_ms=health.latency_ms,
            detail=f"paper: {health.detail}",
            server_offset_seconds=health.server_offset_seconds,
        )

    # -- market state: from the real terminal ---------------------------------------

    async def symbol_specs(self, symbols: list[str]) -> dict[str, SymbolSpec]:
        specs = await self.data.symbol_specs(symbols)
        # The simulator must price against the broker's real contract specification, or the
        # paper session sizes positions differently from the live one it is meant to rehearse.
        self.execution.specs.update(specs)
        return specs

    async def quote(self, symbol: str) -> Quote:
        return await self.data.quote(symbol)

    async def bars(self, symbol: str, tf: Timeframe, count: int) -> list[Bar]:
        fetch = getattr(self.data, "bars", None)
        if fetch is None:
            return []
        return await fetch(symbol, tf, count)

    def observe_quote(self, quote: Quote) -> None:
        # Drives the simulator's matching engine from the real feed.
        self.execution.observe_quote(quote)

    # -- money: from the simulator ----------------------------------------------------

    async def account(self) -> AccountState:
        return await self.execution.account()

    async def positions(self) -> list[Position]:
        return await self.execution.positions()

    async def pending_orders(self) -> list[PendingOrder]:
        return await self.execution.pending_orders()

    async def submit(self, request: OrderRequest) -> OrderResult:
        return await self.execution.submit(request)

    async def modify_position(
        self, ticket: int, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> OrderResult:
        return await self.execution.modify_position(
            ticket, stop_loss=stop_loss, take_profit=take_profit
        )

    async def close_position(
        self, ticket: int, *, volume: float | None = None,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> OrderResult:
        return await self.execution.close_position(ticket, volume=volume, reason=reason)

    async def cancel_order(self, ticket: int) -> OrderResult:
        return await self.execution.cancel_order(ticket)

    async def find_by_client_id(self, client_order_id: str) -> Position | None:
        return await self.execution.find_by_client_id(client_order_id)

    async def closed_trades(self, since_ms: int) -> list[Trade]:
        return await self.execution.closed_trades(since_ms)
