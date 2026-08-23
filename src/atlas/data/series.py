"""Columnar bar storage.

Design note: a ``BarSeries`` holds **closed bars only**. The forming bar lives on the
``MarketDataSource``, not here. Making that a structural property of the container rather
than a convention is the cheapest available defence against look-ahead bias (ADR-004): a
feature function that only ever receives a ``BarSeries`` cannot accidentally read the
in-progress bar because it is not present in the object.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd

from atlas.core.enums import Timeframe
from atlas.core.errors import DataError
from atlas.core.market import Bar

_COLUMNS = ("ts", "open", "high", "low", "close", "volume", "spread")


class BarSeries:
    """Growable, numpy-backed OHLCV series with a bounded memory footprint.

    ``max_bars`` turns the series into a ring: older bars are dropped once the cap is hit.
    Live runs need this (a month of M1 data is 40k bars per symbol and never stops growing);
    backtests set it to ``None`` so the full history is retained for reporting.
    """

    __slots__ = ("_arrays", "_cap", "_n", "max_bars", "symbol", "tf")

    def __init__(
        self,
        symbol: str,
        tf: Timeframe,
        *,
        capacity: int = 1024,
        max_bars: int | None = None,
    ) -> None:
        self.symbol = symbol
        self.tf = tf
        self.max_bars = max_bars
        self._cap = max(16, capacity)
        self._n = 0
        self._arrays: dict[str, np.ndarray] = {
            "ts": np.zeros(self._cap, dtype=np.int64),
            **{c: np.zeros(self._cap, dtype=np.float64) for c in _COLUMNS[1:]},
        }

    # -- construction -----------------------------------------------------------

    @classmethod
    def from_bars(cls, bars: Sequence[Bar], *, max_bars: int | None = None) -> BarSeries:
        if not bars:
            raise DataError("cannot build a BarSeries from an empty sequence")
        s = cls(bars[0].symbol, bars[0].tf, capacity=max(16, len(bars)), max_bars=max_bars)
        s.extend(bars)
        return s

    # -- growth -----------------------------------------------------------------

    def _grow(self, needed: int) -> None:
        if needed <= self._cap:
            return
        new_cap = self._cap
        while new_cap < needed:
            new_cap *= 2
        for k, arr in self._arrays.items():
            grown = np.zeros(new_cap, dtype=arr.dtype)
            grown[: self._n] = arr[: self._n]
            self._arrays[k] = grown
        self._cap = new_cap

    def _trim(self) -> None:
        if self.max_bars is None or self._n <= self.max_bars:
            return
        drop = self._n - self.max_bars
        for arr in self._arrays.values():
            arr[: self.max_bars] = arr[drop : self._n]
        self._n = self.max_bars

    def append(self, bar: Bar) -> None:
        """Append one closed bar.

        Rejects incomplete bars, out-of-order timestamps and timeframe/symbol mismatches.
        These are loud failures on purpose: a silently misordered series produces indicator
        values that look plausible and are wrong.
        """
        if not bar.complete:
            raise DataError(f"refusing to store an incomplete bar at {bar.ts}")
        if bar.tf is not self.tf or bar.symbol != self.symbol:
            raise DataError(f"bar {bar.symbol}/{bar.tf} does not belong to {self.symbol}/{self.tf}")
        if self._n and bar.ts <= self._arrays["ts"][self._n - 1]:
            last = int(self._arrays["ts"][self._n - 1])
            if bar.ts == last:
                self._set(self._n - 1, bar)  # idempotent re-delivery of the same bar
                return
            raise DataError(f"out-of-order bar: {bar.ts} <= {last}")
        self._grow(self._n + 1)
        self._set(self._n, bar)
        self._n += 1
        self._trim()

    def _set(self, i: int, bar: Bar) -> None:
        a = self._arrays
        a["ts"][i] = bar.ts
        a["open"][i] = bar.open
        a["high"][i] = bar.high
        a["low"][i] = bar.low
        a["close"][i] = bar.close
        a["volume"][i] = bar.volume
        a["spread"][i] = bar.spread_points

    def extend(self, bars: Iterable[Bar]) -> None:
        for b in bars:
            self.append(b)

    # -- access -----------------------------------------------------------------

    def __len__(self) -> int:
        return self._n

    def __bool__(self) -> bool:
        return self._n > 0

    @property
    def ts(self) -> np.ndarray:
        return self._arrays["ts"][: self._n]

    @property
    def open(self) -> np.ndarray:
        return self._arrays["open"][: self._n]

    @property
    def high(self) -> np.ndarray:
        return self._arrays["high"][: self._n]

    @property
    def low(self) -> np.ndarray:
        return self._arrays["low"][: self._n]

    @property
    def close(self) -> np.ndarray:
        return self._arrays["close"][: self._n]

    @property
    def volume(self) -> np.ndarray:
        return self._arrays["volume"][: self._n]

    @property
    def spread(self) -> np.ndarray:
        return self._arrays["spread"][: self._n]

    def bar(self, i: int) -> Bar:
        """Bar at index ``i`` (negative indices count from the end)."""
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError(f"bar index {i} out of range for {self._n} bars")
        a = self._arrays
        return Bar(
            symbol=self.symbol, tf=self.tf, ts=int(a["ts"][i]), open=float(a["open"][i]),
            high=float(a["high"][i]), low=float(a["low"][i]), close=float(a["close"][i]),
            volume=float(a["volume"][i]), spread_points=float(a["spread"][i]), complete=True,
        )

    def last(self) -> Bar:
        return self.bar(self._n - 1)

    def tail(self, n: int) -> BarSeries:
        """A copy of the most recent ``n`` bars. Copy, not view: a view would let a caller
        mutate the parent's arrays."""
        n = min(n, self._n)
        out = BarSeries(self.symbol, self.tf, capacity=max(16, n))
        out._grow(n)
        for k, arr in self._arrays.items():
            out._arrays[k][:n] = arr[self._n - n : self._n]
        out._n = n
        return out

    def head(self, n: int) -> BarSeries:
        """The first ``n`` bars -- the prefix a strategy would have seen at that point.

        This is the primitive the no-look-ahead property test is built on.
        """
        n = max(0, min(n, self._n))
        out = BarSeries(self.symbol, self.tf, capacity=max(16, n))
        out._grow(max(16, n))
        for k, arr in self._arrays.items():
            out._arrays[k][:n] = arr[:n]
        out._n = n
        return out

    def bars(self) -> tuple[Bar, ...]:
        return tuple(self.bar(i) for i in range(self._n))

    def index_of(self, ts: int) -> int:
        """Index of the bar opening exactly at ``ts``, or -1."""
        idx = int(np.searchsorted(self.ts, ts))
        if idx < self._n and int(self.ts[idx]) == ts:
            return idx
        return -1

    def to_dataframe(self) -> pd.DataFrame:
        df = pd.DataFrame({c: self._arrays[c][: self._n] for c in _COLUMNS})
        df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("time")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        if not self._n:
            return f"<BarSeries {self.symbol}/{self.tf} empty>"
        return (
            f"<BarSeries {self.symbol}/{self.tf} n={self._n} "
            f"{int(self.ts[0])}..{int(self.ts[-1])} last_close={self.close[-1]:.5f}>"
        )
