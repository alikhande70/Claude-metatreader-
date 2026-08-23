"""Feature providers.

The engine asks one question -- "what did the features look like at time T?" -- and two
implementations answer it:

* :class:`PrecomputedFeatureProvider` computes every timeframe once over a full history and
  returns zero-copy views. Used in backtests, where the history is known up front. Legitimate
  only because every feature is strictly causal, which
  ``tests/unit/test_no_lookahead.py`` proves by recomputation on truncated input.
* :class:`IncrementalFeatureProvider` maintains rolling ``BarSeries`` and recomputes over a
  bounded trailing window on each bar close. Used live, where the future does not exist yet.

They are interchangeable, and ``tests/integration/test_engine_equivalence.py`` asserts that a
backtest run through each produces byte-identical trades. That equivalence is what lets the
fast path be used for research without the results being a different system from the live one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from atlas.core.enums import Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar
from atlas.data.series import BarSeries
from atlas.features.frame import (
    FeatureConfig,
    FeatureFrame,
    FrameLike,
    MultiTimeframeFeatures,
)


class FeatureProvider(ABC):
    @abstractmethod
    def on_bar(self, symbol: str, tf: Timeframe, bar: Bar) -> None:
        """Record a newly closed bar."""

    @abstractmethod
    def features_at(self, symbol: str, ts_ms: int) -> MultiTimeframeFeatures:
        """Features as they stood at ``ts_ms``, containing no later information."""

    @abstractmethod
    def series(self, symbol: str, tf: Timeframe) -> BarSeries | None: ...


class PrecomputedFeatureProvider(FeatureProvider):
    """Whole-history computation, indexed by time. Backtest fast path."""

    def __init__(
        self,
        bars: dict[tuple[str, Timeframe], BarSeries],
        specs: dict[str, SymbolSpec],
        cfg: FeatureConfig,
    ) -> None:
        self.cfg = cfg
        self._series = dict(bars)
        self._by_symbol: dict[str, MultiTimeframeFeatures] = {}
        for symbol in {s for s, _ in bars}:
            frames: dict[Timeframe, FrameLike] = {
                tf: FeatureFrame.compute(series, specs[symbol], cfg)
                for (s, tf), series in bars.items()
                if s == symbol and len(series) > cfg.ema_slow
            }
            self._by_symbol[symbol] = MultiTimeframeFeatures(symbol, frames)

    def on_bar(self, symbol: str, tf: Timeframe, bar: Bar) -> None:
        return None  # history is already complete

    def features_at(self, symbol: str, ts_ms: int) -> MultiTimeframeFeatures:
        base = self._by_symbol.get(symbol)
        if base is None:
            return MultiTimeframeFeatures(symbol, {})
        return base.view_at(ts_ms)

    def series(self, symbol: str, tf: Timeframe) -> BarSeries | None:
        return self._series.get((symbol, tf))


class IncrementalFeatureProvider(FeatureProvider):
    """Rolling recomputation over a bounded window. Live path.

    ``window`` caps both memory and CPU: features are recomputed from the last ``window``
    bars on each close. It must comfortably exceed the longest indicator warm-up, and the
    provider refuses a window that does not -- a silently-too-short window produces features
    that are subtly wrong rather than obviously missing.
    """

    def __init__(
        self,
        specs: dict[str, SymbolSpec],
        timeframes: dict[str, tuple[Timeframe, ...]],
        cfg: FeatureConfig,
        *,
        window: int = 1500,
    ) -> None:
        # The window must cover three things, and the binding one is usually the third:
        #  1. the longest indicator warm-up,
        #  2. the structural state memory (ADR-019),
        #  3. enough bars for Wilder/EMA smoothing to forget its seed. Those recursions have
        #     infinite memory, so a window that merely covers the warm-up still starts from a
        #     different seed than the whole-history computation and produces slightly
        #     different values. 20x the longest smoothing period puts the residual at
        #     ~(1 - 1/n)^(20n) = e^-20, far below any decision threshold.
        smoothing_convergence = 20 * max(cfg.atr_period, cfg.ema_slow, cfg.adx_period)
        needed = max(
            cfg.vol_window + cfg.atr_period,
            cfg.state_memory_bars + 50,
            smoothing_convergence,
        )
        if window < needed:
            raise ValueError(
                f"feature window {window} is too short for this configuration; it must be at "
                f"least {needed} bars. Binding constraints: vol_window "
                f"{cfg.vol_window + cfg.atr_period}, state memory "
                f"{cfg.state_memory_bars + 50}, smoothing convergence "
                f"{smoothing_convergence}. A window below this makes the live rolling "
                f"computation disagree with the research whole-history computation."
            )
        self.cfg = cfg
        self.window = window
        self._specs = specs
        self._timeframes = timeframes
        self._series: dict[tuple[str, Timeframe], BarSeries] = {}
        self._frames: dict[str, dict[Timeframe, FeatureFrame]] = {}
        self._dirty: set[tuple[str, Timeframe]] = set()

    def seed(self, symbol: str, tf: Timeframe, series: BarSeries) -> None:
        """Install warm-up history for a (symbol, timeframe)."""
        ring = BarSeries(symbol, tf, capacity=max(64, self.window), max_bars=self.window)
        start = max(0, len(series) - self.window)
        for i in range(start, len(series)):
            ring.append(series.bar(i))
        self._series[(symbol, tf)] = ring
        self._dirty.add((symbol, tf))

    def on_bar(self, symbol: str, tf: Timeframe, bar: Bar) -> None:
        key = (symbol, tf)
        s = self._series.get(key)
        if s is None:
            s = self._series[key] = BarSeries(
                symbol, tf, capacity=max(64, self.window), max_bars=self.window
            )
        s.append(bar)
        self._dirty.add(key)

    def _refresh(self, symbol: str) -> None:
        frames = self._frames.setdefault(symbol, {})
        for tf in self._timeframes.get(symbol, ()):
            key = (symbol, tf)
            if key not in self._dirty:
                continue
            s = self._series.get(key)
            if s is None or len(s) <= self.cfg.ema_slow:
                continue
            frames[tf] = FeatureFrame.compute(s, self._specs[symbol], self.cfg)
            self._dirty.discard(key)

    def features_at(self, symbol: str, ts_ms: int) -> MultiTimeframeFeatures:
        self._refresh(symbol)
        frames = self._frames.get(symbol, {})
        # Position each frame at its last bar closed by ts_ms. In live operation that is
        # almost always the final bar, but being explicit keeps the semantics identical to
        # the precomputed provider rather than merely usually equal.
        out: dict[Timeframe, FrameLike] = {}
        for tf, f in frames.items():
            v = f.view_at(ts_ms)
            if v is not None:
                out[tf] = v
        return MultiTimeframeFeatures(symbol, out)

    def series(self, symbol: str, tf: Timeframe) -> BarSeries | None:
        return self._series.get((symbol, tf))
