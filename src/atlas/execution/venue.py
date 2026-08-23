"""The execution venue contract.

Exactly one interface, with three implementations: the simulator (backtest and paper), the
MT5 bridge client (live, talking to either the MQL5 EA or the Windows sidecar), and the
in-process fake used by the protocol conformance suite. Swapping the venue is the *only*
difference between a backtest and a live run (ADR-003).

Design notes that matter:

* **``find_by_client_id`` is not optional.** It is what makes order submission idempotent
  (ADR-013). MT5 has no server-side idempotency key, so "did my timed-out order actually
  land?" can only be answered by looking. A venue that cannot answer it cannot be retried
  against safely.
* **Every method may raise ``TransientError``**, which the router retries, or ``VenueError``,
  which it does not. Callers must never distinguish these by string-matching a message.
* **Specs are fetched, never assumed.** ``symbol_specs`` is called on connect and refreshed
  periodically, because a contract-size or stops-level change silently invalidates the risk
  maths on every open position.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from atlas.core.enums import ExitReason
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Quote
from atlas.core.trading import (
    AccountState,
    OrderRequest,
    OrderResult,
    PendingOrder,
    Position,
    Trade,
)


@dataclass(frozen=True, slots=True)
class VenueHealth:
    connected: bool
    trade_allowed: bool
    server_time_ms: int = 0
    latency_ms: float = 0.0
    detail: str = ""
    #: Measured (server_time - utc_time); used to build the ServerClockMapping.
    server_offset_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    """What this venue can actually do.

    Declared rather than assumed so the engine degrades explicitly. A venue that cannot
    stream ticks gets polled; one that cannot look up by client id forces the router into
    its conservative no-retry mode instead of risking a double fill.
    """

    streaming_quotes: bool = False
    streaming_trade_events: bool = False
    pending_orders: bool = True
    partial_close: bool = True
    client_id_lookup: bool = True
    name: str = "venue"
    extra: dict[str, str] = field(default_factory=dict)


class ExecutionVenue(ABC):
    """Everything ATLAS needs from a broker connection."""

    @property
    @abstractmethod
    def capabilities(self) -> VenueCapabilities: ...

    @abstractmethod
    async def connect(self) -> VenueHealth: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def health(self) -> VenueHealth: ...

    @abstractmethod
    async def account(self) -> AccountState: ...

    @abstractmethod
    async def symbol_specs(self, symbols: list[str]) -> dict[str, SymbolSpec]: ...

    @abstractmethod
    async def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    @abstractmethod
    async def pending_orders(self) -> list[PendingOrder]: ...

    @abstractmethod
    async def submit(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def modify_position(
        self, ticket: int, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> OrderResult: ...

    @abstractmethod
    async def close_position(
        self, ticket: int, *, volume: float | None = None,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> OrderResult:
        """Close a position.

        ``reason`` is a **hint** describing why the caller is closing. No broker records it,
        so a live venue ignores it; the simulator stores it on the resulting trade so that
        exit-reason analysis is faithful instead of labelling every managed exit "MANUAL".
        """

    @abstractmethod
    async def cancel_order(self, ticket: int) -> OrderResult: ...

    @abstractmethod
    async def closed_trades(self, since_ms: int) -> list[Trade]:
        """Round trips that finished at or after ``since_ms``.

        Needed by reconciliation and by performance analysis. A live implementation reads
        MT5's deal history and pairs deals into trades; the simulator already has them.
        """

    def observe_quote(self, quote: Quote) -> None:
        """Feed a quote to a venue that has no market feed of its own.

        The simulator needs this -- it is driven by the replay stream. Live venues have their
        own feed and ignore it. Having a default no-op here keeps the engine loop identical
        in backtest and live (ADR-003) instead of branching on venue type.
        """
        return None

    @abstractmethod
    async def find_by_client_id(self, client_order_id: str) -> Position | None:
        """Look up a position by its idempotency key.

        Returning ``None`` must mean "definitely not present", never "I could not check" --
        an unreliable answer here produces double fills. A venue that cannot check must set
        ``capabilities.client_id_lookup = False`` instead of guessing.
        """
