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

from atlas.core.instrument import SymbolSpec
from atlas.core.market import Quote
from atlas.core.trading import (
    AccountState,
    OrderRequest,
    OrderResult,
    PendingOrder,
    Position,
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
    async def close_position(self, ticket: int, *, volume: float | None = None) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, ticket: int) -> OrderResult: ...

    @abstractmethod
    async def find_by_client_id(self, client_order_id: str) -> Position | None:
        """Look up a position by its idempotency key.

        Returning ``None`` must mean "definitely not present", never "I could not check" --
        an unreliable answer here produces double fills. A venue that cannot check must set
        ``capabilities.client_id_lookup = False`` instead of guessing.
        """
