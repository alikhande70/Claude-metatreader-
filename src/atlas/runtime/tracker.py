"""Engine-side position bookkeeping.

The venue is the authority on *what positions exist*; this module holds the things a venue
does not know: which decision opened a position, what its stop was at entry, how many
management bars it has survived, and how far it has run in its favour.

That last group is what makes R-multiples and MAE/MFE computable, and none of it survives a
restart unless it is persisted -- so it is written to the journal and rebuilt on recovery.
When a position is adopted after a restart without its original record, ``stop_reconstructed``
marks it, and every R figure derived from it is reported as approximate rather than quietly
presented as exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.core.enums import Side
from atlas.core.instrument import SymbolSpec
from atlas.core.trading import Position
from atlas.strategy.base import PositionView


@dataclass(slots=True)
class TrackedPosition:
    ticket: int
    symbol: str
    side: Side
    strategy: str
    decision_id: str
    client_order_id: str
    entry_price: float
    initial_stop: float
    initial_target: float | None
    opened_ts: int
    volume: float
    #: The stop currently attached at the broker. Distinct from ``initial_stop``, which is
    #: fixed at entry and defines the R unit; this one moves as the trade is managed.
    current_stop: float | None = None
    bars_held: int = 0
    best_price: float = 0.0
    worst_price: float = 0.0
    stop_reconstructed: bool = False
    last_managed_ts: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def risk_price_distance(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    def observe_venue(self, pos: Position) -> None:
        """Refresh the fields the venue is authoritative about."""
        self.current_stop = pos.stop_loss
        self.volume = pos.volume
        self.observe_price(pos.current_price or self.entry_price)

    def observe_price(self, price: float) -> None:
        if self.best_price == 0.0:
            self.best_price = self.worst_price = price
            return
        if self.side is Side.BUY:
            self.best_price = max(self.best_price, price)
            self.worst_price = min(self.worst_price, price)
        else:
            self.best_price = min(self.best_price, price)
            self.worst_price = max(self.worst_price, price)

    def r_multiple(self, price: float) -> float:
        risk = self.risk_price_distance
        if risk <= 0:
            return 0.0
        return (price - self.entry_price) * self.side.sign / risk

    def to_view(self, pos: Position) -> PositionView:
        return PositionView(
            ticket=self.ticket, side=self.side, entry_price=self.entry_price,
            stop_loss=pos.stop_loss, take_profit=pos.take_profit, open_time=self.opened_ts,
            bars_held=self.bars_held,
            r_multiple_open=self.r_multiple(pos.current_price or self.entry_price),
            mfe_r=self.r_multiple(self.best_price or self.entry_price),
            stop_reconstructed=self.stop_reconstructed,
        )

    def risk_money(self, spec: SymbolSpec, current_price: float) -> float:
        """Money still at risk between the **current** stop and the current price.

        The live stop, not the entry stop. Once a position is at breakeven its contribution
        to open risk is genuinely zero, and counting it at full size would block new trades
        the account can comfortably afford.

        Falls back to the entry stop only when the current one is unknown -- an unprotected
        position is not a zero-risk one, and assuming it were is the dangerous direction to
        be wrong in.
        """
        stop = self.current_stop if self.current_stop is not None else self.initial_stop
        distance = max(0.0, (current_price - stop) * self.side.sign)
        return spec.money_for_points(distance / spec.point, self.volume)


class PositionTracker:
    def __init__(self) -> None:
        self._by_ticket: dict[int, TrackedPosition] = {}
        self._by_client_id: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._by_ticket)

    def __iter__(self):
        return iter(self._by_ticket.values())

    def get(self, ticket: int) -> TrackedPosition | None:
        return self._by_ticket.get(ticket)

    def by_symbol(self, symbol: str) -> list[TrackedPosition]:
        return [t for t in self._by_ticket.values() if t.symbol == symbol]

    def by_client_id(self, client_order_id: str) -> TrackedPosition | None:
        t = self._by_client_id.get(client_order_id)
        return self._by_ticket.get(t) if t is not None else None

    def add(self, tp: TrackedPosition) -> None:
        self._by_ticket[tp.ticket] = tp
        if tp.client_order_id:
            self._by_client_id[tp.client_order_id] = tp.ticket

    def remove(self, ticket: int) -> TrackedPosition | None:
        tp = self._by_ticket.pop(ticket, None)
        if tp is not None and tp.client_order_id:
            self._by_client_id.pop(tp.client_order_id, None)
        return tp

    def tickets(self) -> set[int]:
        return set(self._by_ticket)

    def adopt(self, pos: Position, *, strategy: str, reason: str) -> TrackedPosition:
        """Take ownership of a position we have no record of.

        Happens after a restart, or when a human opened a trade with our magic number. The
        entry stop is reconstructed from whatever stop is currently attached, which is not
        the same thing -- it may already have been trailed -- so the position is flagged and
        its R-multiples are reported as approximate.
        """
        tp = TrackedPosition(
            ticket=pos.ticket, symbol=pos.symbol, side=pos.side, strategy=strategy,
            decision_id="", client_order_id=pos.comment.strip()[:16],
            entry_price=pos.open_price,
            initial_stop=pos.stop_loss if pos.stop_loss is not None else pos.open_price,
            initial_target=pos.take_profit, opened_ts=pos.open_time, volume=pos.volume,
            current_stop=pos.stop_loss, stop_reconstructed=True,
            notes=[reason],
        )
        tp.observe_price(pos.current_price or pos.open_price)
        self.add(tp)
        return tp
