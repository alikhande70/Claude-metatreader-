"""Market data sources and the temporal model of the whole system.

The temporal contract (ADR-015)
-------------------------------
This is the most important paragraph in the data layer, because getting it wrong produces
backtests that cannot be reproduced live.

* A bar with open time ``T`` and period ``P`` becomes **knowable at ``T + P``**, not at ``T``.
  The ``BAR`` update carrying it is therefore stamped ``ts = T + P``.
* The engine clock is set from the update's ``ts``. So when a strategy evaluates a just-closed
  bar, the clock reads ``T + P`` -- the instant the next bar opens.
* A market order issued from that evaluation is filled at the **next quote at or after
  ``T + P``**, which in bar-driven replay is the next bar's open, adjusted for spread and
  slippage. It is never filled at the close of the bar that generated the signal.

That last point is the difference between a system that looks profitable and one that is.
The simulated venue enforces it (it refuses to fill from a quote earlier than the order) and
``tests/unit/test_no_lookahead.py`` asserts it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum

from atlas.core.enums import Timeframe
from atlas.core.errors import DataError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, Quote
from atlas.data.aggregator import MultiTimeframeAggregator
from atlas.data.series import BarSeries


class UpdateKind(StrEnum):
    QUOTE = "QUOTE"
    BAR = "BAR"


@dataclass(slots=True, frozen=True)
class MarketUpdate:
    """One thing that happened in the market, at a known instant."""

    kind: UpdateKind
    symbol: str
    ts: int
    quote: Quote | None = None
    bar: Bar | None = None
    tf: Timeframe | None = None


class MarketDataSource(ABC):
    """Where bars and quotes come from. Swapped between backtest and live (ADR-003)."""

    @property
    @abstractmethod
    def symbols(self) -> tuple[str, ...]: ...

    @abstractmethod
    def spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    async def warmup(self) -> dict[tuple[str, Timeframe], BarSeries]:
        """Historical bars available *before* the first live update.

        Returned series contain closed bars only and are already aggregated to every
        timeframe the strategy needs.
        """

    @abstractmethod
    def stream(self) -> AsyncIterator[MarketUpdate]:
        """Ordered stream of updates. Must be non-decreasing in ``ts``."""

    async def close(self) -> None:
        return None


class ReplayDataSource(MarketDataSource):
    """Historical replay from a base-timeframe bar series.

    Emits, for each base bar, the intrabar quote path first and the closed-bar update last,
    with the closed-bar update stamped at the bar's *close* time per the temporal contract.
    """

    def __init__(
        self,
        bars_by_symbol: dict[str, Sequence[Bar]],
        specs: dict[str, SymbolSpec],
        *,
        base_tf: Timeframe,
        timeframes: Iterable[Timeframe],
        warmup_bars: int = 500,
        quotes_per_bar: int = 4,
    ) -> None:
        if not bars_by_symbol:
            raise DataError("replay source needs at least one symbol")
        self._specs = specs
        self._base_tf = base_tf
        self._timeframes = tuple(dict.fromkeys([base_tf, *timeframes]))
        self._warmup_bars = warmup_bars
        self._quotes_per_bar = max(1, quotes_per_bar)
        self._bars = {s: list(b) for s, b in bars_by_symbol.items()}
        for sym, bl in self._bars.items():
            if len(bl) <= warmup_bars:
                raise DataError(
                    f"{sym}: {len(bl)} bars is not more than the {warmup_bars}-bar warm-up"
                )
            if any(b.tf is not base_tf for b in bl):
                raise DataError(f"{sym}: all replay bars must be {base_tf}")
        self._aggs: dict[str, MultiTimeframeAggregator] = {}

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._bars)

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        return self._timeframes

    def spec(self, symbol: str) -> SymbolSpec:
        try:
            return self._specs[symbol]
        except KeyError as exc:
            raise DataError(f"no symbol spec for {symbol}") from exc

    async def warmup(self) -> dict[tuple[str, Timeframe], BarSeries]:
        out: dict[tuple[str, Timeframe], BarSeries] = {}
        for sym, bl in self._bars.items():
            agg = MultiTimeframeAggregator(sym, self._base_tf, self._timeframes)
            self._aggs[sym] = agg
            series = {tf: BarSeries(sym, tf, capacity=1024) for tf in self._timeframes}
            for bar in bl[: self._warmup_bars]:
                for tf, closed in agg.push(bar).items():
                    series[tf].append(closed)
            for tf, s in series.items():
                out[(sym, tf)] = s
        return out

    async def stream(self) -> AsyncIterator[MarketUpdate]:
        """Merge every symbol's post-warm-up bars into one time-ordered stream."""
        from atlas.data.synthetic import generate_quotes_from_bar

        pending: list[tuple[int, str, Bar]] = []
        for sym, bl in self._bars.items():
            pending.extend((b.ts, sym, b) for b in bl[self._warmup_bars :])
        pending.sort(key=lambda x: (x[0], x[1]))

        for _, sym, bar in pending:
            spec = self.spec(sym)
            for ts, bid, ask in generate_quotes_from_bar(bar, spec.point, self._quotes_per_bar):
                yield MarketUpdate(
                    kind=UpdateKind.QUOTE, symbol=sym, ts=ts,
                    quote=Quote(symbol=sym, ts=ts, bid=bid, ask=ask),
                )
            close_ts = bar.ts_close
            agg = self._aggs.get(sym)
            if agg is None:
                agg = self._aggs[sym] = MultiTimeframeAggregator(
                    sym, self._base_tf, self._timeframes
                )
            for tf, closed in agg.push(bar).items():
                yield MarketUpdate(
                    kind=UpdateKind.BAR, symbol=sym, ts=close_ts, bar=closed, tf=tf
                )


class StaticSpecProvider:
    """Symbol specs supplied by configuration rather than by a live terminal.

    Used in backtests and in tests. In live runs the specs come from the venue and any
    change is journalled, because a contract-size or stops-level change silently invalidates
    the risk maths on every open position.
    """

    def __init__(self, specs: dict[str, SymbolSpec]) -> None:
        self._specs = dict(specs)

    def __getitem__(self, symbol: str) -> SymbolSpec:
        return self._specs[symbol]

    def get(self, symbol: str) -> SymbolSpec | None:
        return self._specs.get(symbol)

    def all(self) -> dict[str, SymbolSpec]:
        return dict(self._specs)


def iter_ordered(sources: dict[str, Sequence[Bar]]) -> Iterator[tuple[str, Bar]]:
    """Time-ordered merge of several symbols' bars, ties broken by symbol name."""
    merged = [(b.ts, sym, b) for sym, bars in sources.items() for b in bars]
    merged.sort(key=lambda x: (x[0], x[1]))
    for _, sym, bar in merged:
        yield sym, bar
