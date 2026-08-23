"""Timeframe aggregation.

ADR-014: **all higher timeframes are derived from a single base timeframe by aggregation**,
in backtest and live alike, rather than being fetched independently from the broker.

Rationale. Fetching M5 and H4 separately from MT5 gives two series whose relationship is
only *approximately* guaranteed: brokers differ on how a partial H4 bar is reported, on
whether a bar with no ticks exists at all, and on the exact server-time boundary. A
multi-timeframe strategy comparing them then reasons about a state that never existed
simultaneously. Deriving H4 from the same M5 stream makes the relationship exact by
construction, and the cost -- fetching more base bars at warm-up -- is negligible.

The broker's own HTF bars are still fetched once at startup and compared, purely as a
diagnostic: a divergence means the broker's bar boundaries do not match ours (usually a
server-time or missing-bar issue) and is worth surfacing rather than absorbing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from atlas.core.enums import Timeframe
from atlas.core.errors import DataError
from atlas.core.market import Bar, floor_to_timeframe


class TimeframeAggregator:
    """Folds base-timeframe bars into one higher timeframe.

    A bucket is closed as soon as a base bar arrives whose **close time reaches the bucket
    boundary** -- not when a bar belonging to the next bucket turns up.

    That distinction is a correctness issue, not an optimisation. The M15 bar spanning
    05:00-05:15 is genuinely closed at 05:15, and a live system knows it at 05:15, because
    the M5 bar 05:10-05:15 has just closed. Waiting for the 05:15 M5 bar to arrive (which
    happens at 05:20) delays every higher-timeframe bar by one base bar. On an H4 read from
    M5 data that is five minutes of stale bias on every H4 close -- and, worse, it made the
    rolling live path disagree with the whole-history research path, because the latter
    computes closure from timestamps and has no such delay.

    A bucket that never completes (the market closed, or the data ran out) is never emitted,
    which is what keeps the right-hand edge of a backtest free of partial bars.
    """

    def __init__(self, symbol: str, base_tf: Timeframe, target_tf: Timeframe) -> None:
        if target_tf.seconds % base_tf.seconds != 0:
            raise DataError(f"{target_tf} is not an integer multiple of {base_tf}")
        if target_tf.seconds < base_tf.seconds:
            raise DataError(f"cannot aggregate {base_tf} up to the shorter {target_tf}")
        self.symbol = symbol
        self.base_tf = base_tf
        self.target_tf = target_tf
        self._bucket_ts: int | None = None
        self._o = self._h = self._l = self._c = 0.0
        self._vol = 0.0
        self._spread_sum = 0.0
        self._count = 0

    @property
    def forming(self) -> Bar | None:
        """The partially built HTF bar, marked ``complete=False``.

        Available to the trade manager (which legitimately needs current price context) and
        structurally unavailable to signal generation, which reads only ``BarSeries``.
        """
        if self._bucket_ts is None:
            return None
        return Bar(
            symbol=self.symbol, tf=self.target_tf, ts=self._bucket_ts,
            open=self._o, high=self._h, low=self._l, close=self._c, volume=self._vol,
            spread_points=self._spread_sum / self._count if self._count else 0.0,
            complete=False,
        )

    def push(self, bar: Bar) -> Bar | None:
        """Feed one base bar. Returns a *closed* HTF bar when one just completed."""
        if bar.tf is not self.base_tf:
            raise DataError(f"expected {self.base_tf} bars, got {bar.tf}")
        bucket = floor_to_timeframe(bar.ts, self.target_tf)
        if self._bucket_ts is None:
            self._start(bucket, bar)
            return self._emit_if_complete(bar)
        if bucket < self._bucket_ts:
            raise DataError(f"base bar {bar.ts} precedes the open bucket {self._bucket_ts}")
        if bucket > self._bucket_ts:
            # The previous bucket never reached its boundary (a gap, or a market close), so
            # it is discarded rather than emitted as a short bar.
            self._start(bucket, bar)
            return self._emit_if_complete(bar)
        self._update(bar)
        return self._emit_if_complete(bar)

    def _emit_if_complete(self, bar: Bar) -> Bar | None:
        """Close the bucket if this base bar's close reaches the bucket boundary."""
        if self._bucket_ts is None:
            return None
        boundary = self._bucket_ts + self.target_tf.seconds * 1000
        if bar.ts_close < boundary:
            return None
        out = self._close()
        self._bucket_ts = None
        return out

    def _start(self, bucket: int, bar: Bar) -> None:
        self._bucket_ts = bucket
        self._o, self._h, self._l, self._c = bar.open, bar.high, bar.low, bar.close
        self._vol = bar.volume
        self._spread_sum = bar.spread_points
        self._count = 1

    def _update(self, bar: Bar) -> None:
        self._h = max(self._h, bar.high)
        self._l = min(self._l, bar.low)
        self._c = bar.close
        self._vol += bar.volume
        self._spread_sum += bar.spread_points
        self._count += 1

    def _close(self) -> Bar:
        if self._bucket_ts is None:  # pragma: no cover - guarded by every caller
            raise DataError("cannot close a bucket that was never opened")
        return Bar(
            symbol=self.symbol, tf=self.target_tf, ts=self._bucket_ts,
            open=self._o, high=self._h, low=self._l, close=self._c, volume=self._vol,
            spread_points=self._spread_sum / self._count if self._count else 0.0,
            complete=True,
        )

    def flush(self) -> Bar | None:
        """Force-close the forming bar. Used only at the end of a historical replay -- never
        live, where an unfinished bar must stay unfinished."""
        if self._bucket_ts is None:
            return None
        out = self._close()
        self._bucket_ts = None
        return out


class MultiTimeframeAggregator:
    """One base stream fanned out to several target timeframes."""

    def __init__(self, symbol: str, base_tf: Timeframe, targets: Iterable[Timeframe]) -> None:
        self.symbol = symbol
        self.base_tf = base_tf
        self.aggregators: dict[Timeframe, TimeframeAggregator] = {
            tf: TimeframeAggregator(symbol, base_tf, tf) for tf in targets if tf is not base_tf
        }

    def push(self, bar: Bar) -> dict[Timeframe, Bar]:
        """Every timeframe that just closed a bar, **longest first, base timeframe last**.

        The ordering is load-bearing. When an M5 bar and an M15 bar close at the same instant,
        a consumer that processes the M5 bar first would evaluate its strategy against an M15
        frame that is one bar stale -- and would do so only on the bars where the higher
        timeframe happened to close, which is exactly when the higher-timeframe bias is most
        likely to have changed. Emitting the slowest timeframe first guarantees that by the
        time the trigger timeframe is handled, every other timeframe is current.

        This was a real defect: it made the rolling live path produce different trades from
        the whole-history research path, while every individual feature computed identically.
        """
        closed: dict[Timeframe, Bar] = {}
        for tf in sorted(self.aggregators, key=lambda t: t.seconds, reverse=True):
            out = self.aggregators[tf].push(bar)
            if out is not None:
                closed[tf] = out
        closed[self.base_tf] = bar
        return closed

    def forming(self) -> dict[Timeframe, Bar]:
        return {tf: b for tf, agg in self.aggregators.items() if (b := agg.forming) is not None}


def aggregate(bars: Iterable[Bar], base_tf: Timeframe, target_tf: Timeframe) -> Iterator[Bar]:
    """One-shot aggregation of a finished historical series.

    A bucket that reaches its boundary is emitted; one that does not (the data simply ran
    out mid-period) is dropped. Emitting a partial bucket would produce a bar whose high and
    low reflect only part of its period -- exactly the shape of a look-ahead artefact at the
    right-hand edge of a backtest.
    """
    it = iter(bars)
    first = next(it, None)
    if first is None:
        return
    agg = TimeframeAggregator(first.symbol, base_tf, target_tf)
    out = agg.push(first)
    if out is not None:
        yield out
    for b in it:
        out = agg.push(b)
        if out is not None:
            yield out


def compare_series(
    ours: Sequence[Bar], theirs: Sequence[Bar], tol: float = 1e-9
) -> list[str]:
    """Diagnostic: report where our aggregated bars differ from the broker's own.

    Returns human-readable difference descriptions. An empty list means the broker's bar
    boundaries agree with ours; a non-empty list is a server-time or missing-bar signal
    worth investigating before trusting multi-timeframe logic.
    """
    ours_by_ts = {b.ts: b for b in ours}
    diffs: list[str] = []
    for t in theirs:
        mine = ours_by_ts.get(t.ts)
        if mine is None:
            diffs.append(f"{t.ts}: broker has a bar we do not")
            continue
        for field in ("open", "high", "low", "close"):
            a, b = getattr(mine, field), getattr(t, field)
            if abs(a - b) > tol:
                diffs.append(f"{t.ts}: {field} ours={a} broker={b}")
    theirs_ts = {t.ts for t in theirs}
    diffs.extend(
        f"{ts}: we have a bar the broker does not" for ts in ours_by_ts if ts not in theirs_ts
    )
    return diffs
