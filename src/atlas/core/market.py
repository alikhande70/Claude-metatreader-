"""Market data value objects.

Time convention (enforced everywhere in ATLAS): **all timestamps are integer epoch
milliseconds in UTC**. Broker server time is converted at the edge (see
``atlas.data.calendar``) and never leaks inward. Storing an int avoids timezone-naive
``datetime`` ambiguity, makes journal records byte-stable, and makes bar alignment exact.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from atlas.core.enums import Timeframe


def utc_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected; supply an aware UTC datetime")
    return int(dt.astimezone(UTC).timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def floor_to_timeframe(ts_ms: int, tf: Timeframe) -> int:
    """Start-of-bar timestamp containing ``ts_ms``.

    Weekly bars are anchored to Monday 00:00 UTC. 1970-01-01 was a Thursday, so a naive
    ``ts // week`` would anchor weeks to Thursday. Shifting by 3 days makes the first Monday
    (1970-01-05, day 4) land on a week boundary: (4 + 3) % 7 == 0.
    """
    period_ms = tf.seconds * 1000
    if tf is Timeframe.W1:
        epoch_offset = 3 * 86_400_000  # 1970-01-01 Thu -> align weeks to Monday
        return ((ts_ms + epoch_offset) // period_ms) * period_ms - epoch_offset
    return (ts_ms // period_ms) * period_ms


class Bar(BaseModel):
    """One OHLCV bar. ``ts`` is the bar's OPEN time (MT5 convention).

    ``complete`` distinguishes a closed bar from the forming one. Signal generation may only
    read ``complete=True`` bars (ADR-004); the trade manager may read the forming bar.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    tf: Timeframe
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread_points: float = 0.0
    complete: bool = True

    @property
    def ts_close(self) -> int:
        return self.ts + self.tf.seconds * 1000

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0


class Quote(BaseModel):
    """Top-of-book snapshot. The only price object an order may be priced from."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    ts: int
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def spread_points(self, point: float) -> float:
        """Spread in points, rounded to remove binary-float residue (30.00000000001 -> 30.0)."""
        return round(self.spread / point, 3)

    def price_for(self, side: str) -> float:
        """Entry price for a side: buys lift the ask, sells hit the bid."""
        return self.ask if side == "BUY" else self.bid

    def exit_price_for(self, side: str) -> float:
        """Price a position of ``side`` would be closed at (the opposite side of the book)."""
        return self.bid if side == "BUY" else self.ask


class MarketSnapshot(BaseModel):
    """Everything the decision layer is allowed to see at one instant.

    Bundling it makes look-ahead structurally harder: a strategy receives a snapshot rather
    than a handle to a mutable series it could index into the future.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    ts: int
    quote: Quote
    spec_name: str
    bars: dict[Timeframe, tuple[Bar, ...]] = Field(default_factory=dict)

    def closed(self, tf: Timeframe) -> tuple[Bar, ...]:
        """Closed bars only, oldest first. The forming bar is filtered out here."""
        return tuple(b for b in self.bars.get(tf, ()) if b.complete)

    def last_closed(self, tf: Timeframe) -> Bar | None:
        c = self.closed(tf)
        return c[-1] if c else None
