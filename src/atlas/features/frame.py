"""Feature computation for one (symbol, timeframe), and multi-timeframe assembly.

Why the whole series is computed at once
----------------------------------------
Every indicator here is strictly causal: the value at bar ``i`` depends only on bars
``0..i``. That property lets us compute the entire history in one vectorised pass and then
*index* into it, instead of recomputing a rolling window on every bar. In a 20,000-bar
backtest that is the difference between seconds and minutes, and it costs nothing in
correctness -- which ``tests/unit/test_no_lookahead.py`` verifies by recomputing on
truncated inputs and demanding identical values.

Multi-timeframe alignment is correct **by construction** rather than by careful indexing:
because higher-timeframe series are appended only when a bar closes (ADR-014/015), the last
element of an HTF series is always the last *closed* HTF bar. There is no "shift 1" to
remember and therefore no shift-1 bug to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from atlas.core.enums import StructureEvent, Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.data.series import BarSeries
from atlas.features import indicators as ind
from atlas.features.structure import (
    FairValueGap,
    LiquidityPool,
    StructureSeries,
    StructureView,
    analyse_structure,
    detect_sweep,
    displacement,
    find_fvgs,
    find_liquidity_pools,
    unmitigated_fvgs,
)


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    """Feature parameters.

    Deliberately small. Every entry here is a degree of freedom that a walk-forward
    optimisation can fit to noise; the rule of thumb is ~50 trades per parameter before a
    result means anything, so the parameter budget is a design constraint, not a detail.

    The threshold parameters are expressed as **multiples of ATR** rather than as absolute
    point values so that they transfer across instruments and across volatility regimes
    without re-fitting.
    """

    atr_period: int = 14
    adx_period: int = 14
    rsi_period: int = 14
    er_period: int = 20
    ema_fast: int = 21
    ema_slow: int = 55
    donchian_period: int = 20
    swing_strength: int = 3
    vol_window: int = 200

    #: A close must clear a level by max(2 x spread, break_margin_atr x ATR) to count.
    break_margin_atr: float = 0.10
    #: Gaps smaller than this fraction of ATR are noise, not imbalance.
    fvg_min_atr: float = 0.30
    #: Swings within this fraction of ATR form one liquidity pool.
    liquidity_tol_atr: float = 0.15
    #: How far beyond a pool price must trade to count as a sweep.
    sweep_penetration_atr: float = 0.10
    displacement_bars: int = 3
    fvg_max_age: int = 200
    #: How long a structural break or swing keeps influencing state, in bars (ADR-019).
    #: Must be comfortably smaller than the live feature window, or the live path and the
    #: research path compute different things.
    state_memory_bars: int = 300
    #: Minimum width of the dealing range, in ATR. A range narrower than this is not a range.
    #: Chosen structurally (a swing range spanning less than ~2 ATR is a single bar's noise,
    #: not a structure), not optimised. Sensitivity is smooth: sweeping 1.0-3.0 moves the
    #: median range from 2.8 to 4.1 ATR with no discontinuity, so nothing hinges on the exact
    #: value.
    min_range_atr: float = 2.0


@dataclass(slots=True)
class FeatureFrame:
    """Computed features for one symbol/timeframe, aligned to the source series."""

    symbol: str
    tf: Timeframe
    cfg: FeatureConfig
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    atr_pct: np.ndarray
    adx: np.ndarray
    plus_di: np.ndarray
    minus_di: np.ndarray
    rsi: np.ndarray
    er: np.ndarray
    ema_fast: np.ndarray
    ema_slow: np.ndarray
    donchian_high: np.ndarray
    donchian_low: np.ndarray
    body_ratio: np.ndarray
    displacement: np.ndarray
    realized_vol: np.ndarray
    structure: StructureSeries
    point: float = 1e-5
    fvgs: list[FairValueGap] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.close)

    @classmethod
    def compute(cls, series: BarSeries, spec: SymbolSpec, cfg: FeatureConfig) -> FeatureFrame:
        h, low_, c, o = series.high, series.low, series.close, series.open
        ts = series.ts
        a = ind.atr(h, low_, c, cfg.atr_period)
        adx_v, pdi, mdi = ind.adx(h, low_, c, cfg.adx_period)
        spread_price = series.spread * spec.point
        margin = np.maximum(2.0 * spread_price, cfg.break_margin_atr * np.nan_to_num(a))
        structure = analyse_structure(
            h, low_, c, ts, strength=cfg.swing_strength, break_margin=margin,
            min_range=cfg.min_range_atr * np.nan_to_num(a),
            state_memory_bars=cfg.state_memory_bars,
        )
        fvgs = find_fvgs(h, low_, ts, min_height=cfg.fvg_min_atr * np.nan_to_num(a))
        dh, dl = ind.donchian(h, low_, cfg.donchian_period)
        # NOTE: the percentile window is a FIXED config value. Deriving it from len(series)
        # would make the same bar's feature change as more history arrives -- a look-ahead
        # leak that only shows up under truncated recomputation.
        atr_pct = ind.rolling_percentile(a, cfg.vol_window)
        bars_per_year = 365.25 * 24 * 3600 / series.tf.seconds
        return cls(
            symbol=series.symbol, tf=series.tf, cfg=cfg, ts=ts, open=o, high=h, low=low_, close=c,
            atr=a,
            atr_pct=atr_pct,
            adx=adx_v, plus_di=pdi, minus_di=mdi,
            rsi=ind.rsi(c, cfg.rsi_period),
            er=ind.efficiency_ratio(c, cfg.er_period),
            ema_fast=ind.ema(c, cfg.ema_fast), ema_slow=ind.ema(c, cfg.ema_slow),
            donchian_high=dh, donchian_low=dl,
            body_ratio=ind.body_ratio(o, h, low_, c),
            displacement=displacement(c, a, cfg.displacement_bars),
            realized_vol=ind.realized_vol(c, cfg.er_period, bars_per_year),
            structure=structure, fvgs=fvgs, point=spec.point,
        )

    # -- access -----------------------------------------------------------------

    def ready(self, i: int = -1) -> bool:
        """True when the warm-up region is behind us at bar ``i``.

        Checked explicitly rather than relying on NaN propagation: a NaN silently failing
        every comparison would make the system stand aside without ever saying why.
        """
        if self.n == 0:
            return False
        i = i if i >= 0 else self.n + i
        if i < 0 or i >= self.n:
            return False
        return not (
            np.isnan(self.atr[i]) or np.isnan(self.adx[i]) or np.isnan(self.ema_slow[i])
            or np.isnan(self.er[i])
        )

    def pools_at(self, i: int = -1) -> list[LiquidityPool]:
        """Liquidity pools as knowable at bar ``i``.

        Computed on demand rather than cached for the whole series: the clustering tolerance
        is ``liquidity_tol_atr x ATR`` and using the *final* bar's ATR to cluster historical
        swings would leak future volatility backwards into every past bar. Cheap enough --
        it runs once per decision, over a few hundred swings.
        """
        i = i if i >= 0 else self.n + i
        atr_i = float(np.nan_to_num(self.atr[i]))
        swings = [s for s in self.structure.swings if s.confirmed_index <= i]
        return find_liquidity_pools(
            swings, tolerance=max(self.cfg.liquidity_tol_atr * atr_i, self.point)
        )

    def sweep_at(self, i: int = -1) -> LiquidityPool | None:
        i = i if i >= 0 else self.n + i
        pen = self.cfg.sweep_penetration_atr * float(np.nan_to_num(self.atr[i]))
        return detect_sweep(self.high, self.low, self.close, i, self.pools_at(i), pen)

    def active_fvgs(self, i: int = -1) -> list[FairValueGap]:
        i = i if i >= 0 else self.n + i
        return unmitigated_fvgs(self.fvgs, self.high, self.low, i, max_age=self.cfg.fvg_max_age)

    def index_at(self, ts_ms: int) -> int:
        """Index of the last bar **closed** at or before ``ts_ms``, or -1 if none.

        ``self.ts`` holds bar OPEN times, so a bar is closed at ``ts + period``. Getting this
        wrong by one bar is the classic multi-timeframe look-ahead bug; it is computed here
        once rather than at each call site.
        """
        period = self.tf.seconds * 1000
        idx = int(np.searchsorted(self.ts + period, ts_ms, side="right")) - 1
        return idx

    def view(self, i: int) -> FrameView:
        """A zero-copy view of this frame as it stood at bar ``i``."""
        return FrameView(self, i if i >= 0 else self.n + i)

    def view_at(self, ts_ms: int) -> FrameView | None:
        i = self.index_at(ts_ms)
        return None if i < 0 else FrameView(self, i)

    def values(self, i: int = -1) -> dict[str, float]:
        """Flat numeric snapshot for the decision record and the dashboard.

        Keys are prefixed with the timeframe so a multi-timeframe record stays unambiguous
        when its features are flattened into one dict.
        """
        i = i if i >= 0 else self.n + i
        p = f"{self.tf}"
        snap = self.structure.at(i)
        price = float(self.close[i])
        return {
            f"{p}.close": price,
            f"{p}.atr": _f(self.atr[i]),
            f"{p}.atr_pct": _f(self.atr_pct[i]),
            f"{p}.adx": _f(self.adx[i]),
            f"{p}.di_spread": _f(self.plus_di[i]) - _f(self.minus_di[i]),
            f"{p}.rsi": _f(self.rsi[i]),
            f"{p}.er": _f(self.er[i]),
            f"{p}.ema_fast": _f(self.ema_fast[i]),
            f"{p}.ema_slow": _f(self.ema_slow[i]),
            f"{p}.ema_gap_atr": _safe_div(
                _f(self.ema_fast[i]) - _f(self.ema_slow[i]), _f(self.atr[i])
            ),
            f"{p}.body_ratio": _f(self.body_ratio[i]),
            f"{p}.displacement": _f(self.displacement[i]),
            f"{p}.realized_vol": _f(self.realized_vol[i]),
            f"{p}.range_high": snap.range_high,
            f"{p}.range_low": snap.range_low,
            f"{p}.range_pos": snap.position_in_range(price),
            f"{p}.swing_trend": _trend_code(snap.swing_trend),
            f"{p}.break_state": _trend_code(snap.break_state),
            f"{p}.event": _event_code(snap.event),
        }


class FrameView:
    """A :class:`FeatureFrame` restricted to bars ``0..i``.

    Every array property returns a **numpy view** (``arr[:i+1]``), which is O(1) and shares
    memory with the parent. That is what makes it viable to compute features once over a
    20,000-bar history and then hand each of 20,000 evaluations its own past -- the
    alternative, recomputing a rolling window per bar, is quadratic and turns a 20-second
    backtest into a 20-minute one.

    The view exposes the same surface a strategy uses, so strategy code is written once and
    runs unchanged against a live frame (where "now" is the last bar) and a historical view.
    """

    __slots__ = ("_f", "_i")

    def __init__(self, frame: FeatureFrame, i: int) -> None:
        self._f = frame
        self._i = i

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<FrameView {self._f.symbol}/{self._f.tf} at bar {self._i}>"

    @property
    def index(self) -> int:
        return self._i

    @property
    def n(self) -> int:
        return self._i + 1

    @property
    def symbol(self) -> str:
        return self._f.symbol

    @property
    def tf(self) -> Timeframe:
        return self._f.tf

    @property
    def cfg(self) -> FeatureConfig:
        return self._f.cfg

    @property
    def point(self) -> float:
        return self._f.point

    @property
    def structure(self) -> StructureView:
        return StructureView(self._f.structure, self._i)

    def ready(self, i: int = -1) -> bool:
        return self._f.ready(self._i if i == -1 else i)

    def pools_at(self, i: int = -1) -> list[LiquidityPool]:
        return self._f.pools_at(self._i if i == -1 else i)

    def sweep_at(self, i: int = -1) -> LiquidityPool | None:
        return self._f.sweep_at(self._i if i == -1 else i)

    def active_fvgs(self, i: int = -1) -> list[FairValueGap]:
        return self._f.active_fvgs(self._i if i == -1 else i)

    def values(self, i: int = -1) -> dict[str, float]:
        return self._f.values(self._i if i == -1 else i)


def _view_array(name: str):
    def getter(self: FrameView) -> np.ndarray:
        return getattr(self._f, name)[: self._i + 1]

    getter.__name__ = name
    return property(getter)


for _name in (
    "ts", "open", "high", "low", "close", "atr", "atr_pct", "adx", "plus_di", "minus_di",
    "rsi", "er", "ema_fast", "ema_slow", "donchian_high", "donchian_low", "body_ratio",
    "displacement", "realized_vol",
):
    setattr(FrameView, _name, _view_array(_name))
del _name


#: Anything a strategy can read features from: a live frame, or a historical view of one.
FrameLike = FeatureFrame | FrameView


@dataclass(slots=True)
class MultiTimeframeFeatures:
    """Feature frames for one symbol across the strategy's fixed timeframe ladder.

    The ladder is fixed in configuration before testing. Evaluating extra timeframes and
    trading whichever agrees with the bias is not analysis, it is a search for confirmation,
    and it inflates results in exactly the way that does not survive live.
    """

    symbol: str
    frames: dict[Timeframe, FrameLike]

    def get(self, tf: Timeframe) -> FrameLike | None:
        return self.frames.get(tf)

    def require(self, tf: Timeframe) -> FrameLike:
        f = self.frames.get(tf)
        if f is None:
            raise KeyError(f"{self.symbol}: no feature frame for {tf}")
        return f

    def ready(self, timeframes: list[Timeframe] | None = None) -> bool:
        tfs = timeframes or list(self.frames)
        return all((f := self.frames.get(tf)) is not None and f.ready() for tf in tfs)

    def values(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for f in self.frames.values():
            out.update(f.values())
        return out

    def view_at(self, ts_ms: int) -> MultiTimeframeFeatures:
        """This symbol's features as they stood at ``ts_ms``.

        Each timeframe is positioned at its last bar **closed** by then, so multi-timeframe
        alignment is computed in exactly one place. Timeframes with no closed bar yet are
        omitted, which the strategy reports as ``NOT_READY`` rather than silently skipping.
        """
        out: dict[Timeframe, FrameLike] = {}
        for tf, f in self.frames.items():
            base = f._f if isinstance(f, FrameView) else f
            v = base.view_at(ts_ms)
            if v is not None:
                out[tf] = v
        return MultiTimeframeFeatures(self.symbol, out)


def _f(x: object) -> float:
    v = float(x)  # type: ignore[arg-type]
    return 0.0 if np.isnan(v) else v


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _trend_code(t: str) -> float:
    return {"BULL": 1.0, "BEAR": -1.0}.get(t, 0.0)


def _event_code(e: StructureEvent) -> float:
    return {
        StructureEvent.BOS_UP: 2.0, StructureEvent.CHOCH_UP: 1.0,
        StructureEvent.NONE: 0.0,
        StructureEvent.CHOCH_DOWN: -1.0, StructureEvent.BOS_DOWN: -2.0,
    }[e]
