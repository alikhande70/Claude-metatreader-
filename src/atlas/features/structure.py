"""Market structure: swings, trend state, BOS/CHoCH, ranges, liquidity, FVG.

The governing rule (ADR-004)
----------------------------
Almost every structural concept is *defined* using bars that come after the event and is
therefore **knowable later than it happened**. A swing high at bar ``k`` with strength ``N``
requires the following ``N`` bars to be lower, so it is knowable only at bar ``k + N``. Code
that treats it as knowable at ``k`` is reading the future, and the resulting backtest measures
clairvoyance rather than strategy.

Every structure produced here carries both indices: ``index`` (when it happened) and
``confirmed_index`` (when we were allowed to know). All downstream state machines are driven
by ``confirmed_index``.

Fixed specification choices
---------------------------
These are decisions, not defaults to be tuned. Changing one changes the strategy:

* **Breaks are close-based**, not wick-based, and must clear the level by
  ``margin = max(2 x spread, 0.1 x ATR)``. Without a margin, a one-point overshoot on a noisy
  print counts as a break and the backtest fills with untradeable signals.
* **Two trend notions are exposed separately.** ``swing_trend`` comes purely from the
  confirmed HH/HL sequence and is slow but robust. ``break_state`` responds immediately to
  BOS/CHoCH. Mixing them into one number would hide which one a rule actually depends on.
* **A CHoCH does not flip ``swing_trend``.** It sets ``break_state`` and marks the old trend
  as broken; ``swing_trend`` only changes when new confirmed swings establish a new sequence.
* **Structural state has explicitly bounded memory** (``state_memory_bars``). A break or a
  swing older than that no longer contributes. This is partly a modelling judgement -- a break
  of structure from two thousand bars ago does not describe the market now -- and partly a
  hard architectural requirement (ADR-019): the live engine recomputes features over a rolling
  window, so any feature whose value depends on unbounded history would differ between the
  research path and the live path, silently, in a way no test could pin down.
* **The dealing range is not simply the last two opposite swings.** In chop the most recent
  swing high and swing low can sit a few points apart, which collapses the range and makes
  premium/discount meaningless (or explosive, once you divide by it). The range is instead
  built by walking back through confirmed swings until it spans at least ``min_range``
  (default one ATR) or ``MAX_RANGE_SWINGS`` swings have been consumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from atlas.core.enums import StructureEvent

SwingKind = Literal["HIGH", "LOW"]
TrendState = Literal["BULL", "BEAR", "RANGE"]


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int  # bar at which the swing actually formed
    ts: int
    price: float
    kind: SwingKind
    confirmed_index: int  # first bar at which we were entitled to know about it

    @property
    def lag(self) -> int:
        return self.confirmed_index - self.index


def find_swings(
    high: np.ndarray, low: np.ndarray, ts: np.ndarray, strength: int = 3
) -> list[SwingPoint]:
    """Fractal swing points, in confirmation order.

    Only swings whose confirmation bar exists within the supplied data are returned, so the
    result is exactly the set a trader could have known about by the last bar.

    A bar that is simultaneously a swing high and a swing low (possible on a flat stretch with
    ties) is reported as both; the trend machine ignores such degenerate pairs because the
    strict inequality below makes them rare and harmless.
    """
    h = np.asarray(high, dtype=np.float64)
    low_ = np.asarray(low, dtype=np.float64)
    n = len(h)
    out: list[SwingPoint] = []
    if strength < 1 or n < 2 * strength + 1:
        return out
    span = 2 * strength + 1
    hw = sliding_window_view(h, span)  # window j is centred on bar j + strength
    lw = sliding_window_view(low_, span)
    centre_h = hw[:, strength]
    centre_l = lw[:, strength]
    is_high = (centre_h > hw[:, :strength].max(axis=1)) & (
        centre_h > hw[:, strength + 1 :].max(axis=1)
    )
    is_low = (centre_l < lw[:, :strength].min(axis=1)) & (
        centre_l < lw[:, strength + 1 :].min(axis=1)
    )
    for j in np.flatnonzero(is_high | is_low):
        k = int(j) + strength
        if is_high[j]:
            out.append(SwingPoint(k, int(ts[k]), float(h[k]), "HIGH", k + strength))
        if is_low[j]:
            out.append(SwingPoint(k, int(ts[k]), float(low_[k]), "LOW", k + strength))
    out.sort(key=lambda s: (s.confirmed_index, s.index))
    return out


@dataclass(frozen=True, slots=True)
class StructureSnapshot:
    """Structural state as known at one bar."""

    index: int
    swing_trend: TrendState
    break_state: TrendState
    event: StructureEvent
    last_high: SwingPoint | None
    last_low: SwingPoint | None
    prev_high: SwingPoint | None
    prev_low: SwingPoint | None
    range_high: float
    range_low: float

    @property
    def equilibrium(self) -> float:
        return (self.range_high + self.range_low) / 2.0

    @property
    def range_size(self) -> float:
        return max(0.0, self.range_high - self.range_low)

    def position_in_range(self, price: float) -> float:
        """Where ``price`` sits in the confirmed range: 0 at the low, 1 at the high.

        Values outside [0, 1] are meaningful and are not clipped -- price beyond the range
        boundary is exactly the situation a breakout or sweep rule cares about.
        """
        if self.range_size <= 0:
            return 0.5
        return (price - self.range_low) / self.range_size

    def is_discount(self, price: float) -> bool:
        return price < self.equilibrium

    def is_premium(self, price: float) -> bool:
        return price > self.equilibrium


class StructureSeries:
    """Per-bar structural state for an entire series, computed in one forward pass.

    Computing the whole history at once is what makes backtests fast, and it is safe only
    because the pass is strictly causal: the state written at bar ``i`` is derived from bars
    ``0..i`` and from swings whose ``confirmed_index <= i``. That property is asserted by
    ``tests/unit/test_no_lookahead.py``, which recomputes on truncated inputs and requires
    identical values.
    """

    __slots__ = (
        "_snapshots", "break_state", "event", "last_high_price", "last_low_price", "n",
        "range_high", "range_low", "swing_trend", "swings",
    )

    def __init__(self, n: int) -> None:
        self.n = n
        self.swings: list[SwingPoint] = []
        self.swing_trend: list[TrendState] = ["RANGE"] * n
        self.break_state: list[TrendState] = ["RANGE"] * n
        self.event: list[StructureEvent] = [StructureEvent.NONE] * n
        self.range_high = np.full(n, np.nan)
        self.range_low = np.full(n, np.nan)
        self.last_high_price = np.full(n, np.nan)
        self.last_low_price = np.full(n, np.nan)
        self._snapshots: list[StructureSnapshot | None] = [None] * n

    def at(self, i: int) -> StructureSnapshot:
        if i < 0:
            i += self.n
        snap = self._snapshots[i]
        if snap is None:  # pragma: no cover - defensive
            raise IndexError(f"no structure snapshot at bar {i}")
        return snap

    def last(self) -> StructureSnapshot:
        return self.at(self.n - 1)


#: How far back through confirmed swings the dealing range may reach before giving up on
#: reaching ``min_range``. Bounded so the range stays a *current* structure, not all history.
MAX_RANGE_SWINGS = 6


def _dealing_range(
    swings: list[SwingPoint], fallback_hi: float, fallback_lo: float, min_range: float
) -> tuple[float, float]:
    """Current dealing range: walk back through confirmed swings until the span is
    meaningful.

    Using only the two most recent opposite swings produces a degenerate range whenever they
    happen to sit at similar prices, which is common in chop -- and a near-zero range makes
    ``position_in_range`` diverge. Extending backwards until the span reaches ``min_range``
    keeps the concept stable without introducing an arbitrary bar lookback.

    The post-condition ``range_size >= min_range`` is **guaranteed**, not merely attempted:
    if walking back through ``MAX_RANGE_SWINGS`` swings still does not span far enough (a
    tight coil), the interval is widened symmetrically about its midpoint. Guaranteeing the
    invariant here means every consumer of ``position_in_range`` is bounded by construction
    and none of them needs its own defensive clamp.
    """
    if not swings:
        hi, lo = fallback_hi, fallback_lo
    else:
        hi = lo = swings[-1].price
        for k in range(len(swings) - 1, max(-1, len(swings) - 1 - MAX_RANGE_SWINGS), -1):
            p = swings[k].price
            hi = max(hi, p)
            lo = min(lo, p)
            if hi - lo >= min_range > 0:
                break
    if hi < lo:
        hi, lo = lo, hi
    if min_range > 0 and hi - lo < min_range:
        mid = (hi + lo) / 2.0
        hi, lo = mid + min_range / 2.0, mid - min_range / 2.0
    return hi, lo


def analyse_structure(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ts: np.ndarray,
    *,
    strength: int = 3,
    break_margin: np.ndarray | None = None,
    min_range: np.ndarray | None = None,
    state_memory_bars: int = 300,
) -> StructureSeries:
    """Walk the series once, maintaining swing lists and the BOS/CHoCH state machine.

    ``break_margin`` is a per-bar price distance a close must clear to count as a break;
    supply ``max(2 * spread, 0.1 * ATR)``. When omitted, breaks are exact (used only in tests).

    ``min_range`` is the per-bar minimum width of the dealing range; supply one ATR.

    ``state_memory_bars`` bounds how long a break or a swing keeps influencing the state.
    Beyond it, the state decays to ``RANGE``. This is required for the live rolling-window
    path to agree with the research whole-history path (ADR-019), and it is also the more
    defensible model: a structural break from two thousand bars ago is not describing now.
    """
    h = np.asarray(high, dtype=np.float64)
    low_ = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    n = len(c)
    res = StructureSeries(n)
    if n == 0:
        return res
    margin = np.zeros(n) if break_margin is None else np.nan_to_num(np.asarray(break_margin, float))
    min_rng = np.zeros(n) if min_range is None else np.nan_to_num(np.asarray(min_range, float))
    memory = max(1, int(state_memory_bars))

    swings = find_swings(h, low_, ts, strength)
    res.swings = swings
    by_confirm: dict[int, list[SwingPoint]] = {}
    for s in swings:
        by_confirm.setdefault(s.confirmed_index, []).append(s)

    highs: list[SwingPoint] = []
    lows: list[SwingPoint] = []
    ordered: list[SwingPoint] = []  # all confirmed swings, in confirmation order
    swing_trend: TrendState = "RANGE"
    # Bounded memory (ADR-019): the break that defines break_state is remembered for
    # `memory` bars and then forgotten, so the whole state machine is a pure function of a
    # bounded window of input.
    last_break_dir: TrendState = "RANGE"
    last_break_index = -10**9
    # Reference levels that a break is measured against. They are consumed on break so that
    # one swing cannot generate the same BOS on every subsequent bar.
    sh_ref: SwingPoint | None = None
    sl_ref: SwingPoint | None = None

    for i in range(n):
        horizon = i - memory
        for s in by_confirm.get(i, ()):
            ordered.append(s)
            if s.kind == "HIGH":
                highs.append(s)
                sh_ref = s
            else:
                lows.append(s)
                sl_ref = s
        # Drop anything that has aged out of the memory window.
        while highs and highs[0].confirmed_index < horizon:
            highs.pop(0)
        while lows and lows[0].confirmed_index < horizon:
            lows.pop(0)
        while ordered and ordered[0].confirmed_index < horizon:
            ordered.pop(0)
        if sh_ref is not None and sh_ref.confirmed_index < horizon:
            sh_ref = None
        if sl_ref is not None and sl_ref.confirmed_index < horizon:
            sl_ref = None

        if len(highs) >= 2 and len(lows) >= 2:
            hh = highs[-1].price > highs[-2].price
            hl = lows[-1].price > lows[-2].price
            lh = highs[-1].price < highs[-2].price
            ll = lows[-1].price < lows[-2].price
            if hh and hl:
                swing_trend = "BULL"
            elif lh and ll:
                swing_trend = "BEAR"
            else:
                swing_trend = "RANGE"
        else:
            swing_trend = "RANGE"

        break_state: TrendState = (
            last_break_dir if (i - last_break_index) <= memory else "RANGE"
        )

        event = StructureEvent.NONE
        m = float(margin[i])
        if sh_ref is not None and c[i] > sh_ref.price + m:
            # Breaking the reference high: continuation if we were already bullish,
            # otherwise a change of character.
            event = StructureEvent.BOS_UP if break_state == "BULL" else StructureEvent.CHOCH_UP
            break_state = last_break_dir = "BULL"
            last_break_index = i
            sh_ref = None
        elif sl_ref is not None and c[i] < sl_ref.price - m:
            event = StructureEvent.BOS_DOWN if break_state == "BEAR" else StructureEvent.CHOCH_DOWN
            break_state = last_break_dir = "BEAR"
            last_break_index = i
            sl_ref = None

        lh_p = highs[-1] if highs else None
        ll_p = lows[-1] if lows else None
        rh, rl = _dealing_range(
            ordered, float(h[: i + 1].max()), float(low_[: i + 1].min()), float(min_rng[i])
        )

        res.swing_trend[i] = swing_trend
        res.break_state[i] = break_state
        res.event[i] = event
        res.range_high[i] = rh
        res.range_low[i] = rl
        res.last_high_price[i] = lh_p.price if lh_p else np.nan
        res.last_low_price[i] = ll_p.price if ll_p else np.nan
        res._snapshots[i] = StructureSnapshot(
            index=i, swing_trend=swing_trend, break_state=break_state, event=event,
            last_high=lh_p, last_low=ll_p,
            prev_high=highs[-2] if len(highs) >= 2 else None,
            prev_low=lows[-2] if len(lows) >= 2 else None,
            range_high=rh, range_low=rl,
        )
    return res


# --- Fair value gaps ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FairValueGap:
    index: int  # index of the third bar -- the bar at which the gap becomes knowable
    ts: int
    top: float
    bottom: float
    bullish: bool

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def find_fvgs(
    high: np.ndarray,
    low: np.ndarray,
    ts: np.ndarray,
    *,
    min_height: np.ndarray | None = None,
) -> list[FairValueGap]:
    """Three-bar imbalances. Knowable at the close of the third bar -- no lag.

    ``min_height`` filters out gaps smaller than a per-bar threshold (supply ``0.3 * ATR``);
    without it, every second bar produces a one-tick "gap" and the concept becomes noise.
    """
    h = np.asarray(high, dtype=np.float64)
    low_ = np.asarray(low, dtype=np.float64)
    n = len(h)
    out: list[FairValueGap] = []
    if n < 3:
        return out
    k = np.arange(2, n)
    floor = np.zeros(n) if min_height is None else np.nan_to_num(np.asarray(min_height, float))
    bear = (low_[k - 2] > h[k]) & ((low_[k - 2] - h[k]) >= floor[k])
    bull = (h[k - 2] < low_[k]) & ((low_[k] - h[k - 2]) >= floor[k])
    for j in np.flatnonzero(bear | bull):
        idx = int(k[j])
        if bear[j]:
            out.append(FairValueGap(idx, int(ts[idx]), float(low_[idx - 2]), float(h[idx]), False))
        else:
            out.append(FairValueGap(idx, int(ts[idx]), float(low_[idx]), float(h[idx - 2]), True))
    return out


def unmitigated_fvgs(
    gaps: list[FairValueGap], high: np.ndarray, low: np.ndarray, upto: int, *, max_age: int = 200
) -> list[FairValueGap]:
    """Gaps not yet traded through, as of bar ``upto``.

    "Mitigated" means price traded through the far edge. A gap merely touched stays active.
    The distinction is fixed here so that two parts of the system cannot disagree about
    whether the same level is still in play.
    """
    h = np.asarray(high, dtype=np.float64)
    low_ = np.asarray(low, dtype=np.float64)
    alive: list[FairValueGap] = []
    for g in gaps:
        if g.index > upto or upto - g.index > max_age:
            continue
        after_h = h[g.index + 1 : upto + 1]
        after_l = low_[g.index + 1 : upto + 1]
        if len(after_h) == 0:
            alive.append(g)
            continue
        mitigated = (after_l.min() <= g.bottom) if g.bullish else (after_h.max() >= g.top)
        if not mitigated:
            alive.append(g)
    return alive


# --- Liquidity ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    price: float
    kind: SwingKind
    touches: int
    last_index: int


def find_liquidity_pools(
    swings: list[SwingPoint], tolerance: float, *, min_touches: int = 2
) -> list[LiquidityPool]:
    """Cluster confirmed swings that sit within ``tolerance`` of each other.

    Equal highs/lows are where resting stop orders accumulate. ``tolerance`` should be derived
    from spread and ATR (``max(3 * spread, 0.15 * ATR)``), never optimised -- it is the
    parameter most likely to be fitted to noise.
    """
    pools: list[LiquidityPool] = []
    for kind in ("HIGH", "LOW"):
        pts = sorted((s for s in swings if s.kind == kind), key=lambda s: s.price)
        i = 0
        while i < len(pts):
            j = i
            while j + 1 < len(pts) and pts[j + 1].price - pts[i].price <= tolerance:
                j += 1
            group = pts[i : j + 1]
            if len(group) >= min_touches:
                pools.append(
                    LiquidityPool(
                        price=float(np.mean([g.price for g in group])),
                        kind=kind,
                        touches=len(group),
                        last_index=max(g.confirmed_index for g in group),
                    )
                )
            i = j + 1
    return pools


def detect_sweep(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    index: int,
    pools: list[LiquidityPool],
    penetration: float,
    *,
    lookback: int = 3,
) -> LiquidityPool | None:
    """A pool taken and reclaimed: price traded beyond it within ``lookback`` bars and the
    current bar closed back inside.

    ``lookback`` is deliberately fixed at 3 rather than exposed as a tunable, because it is
    the single parameter in this concept most prone to being fitted to noise.
    """
    h = np.asarray(high, dtype=np.float64)
    low_ = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    lo_i = max(0, index - lookback + 1)
    win_h = h[lo_i : index + 1]
    win_l = low_[lo_i : index + 1]
    if len(win_h) == 0:
        return None
    for p in pools:
        if p.last_index > index:
            continue
        if p.kind == "HIGH" and win_h.max() > p.price + penetration and c[index] < p.price:
            return p
        if p.kind == "LOW" and win_l.min() < p.price - penetration and c[index] > p.price:
            return p
    return None


def displacement(close: np.ndarray, atr_values: np.ndarray, bars: int = 3) -> np.ndarray:
    """Net move over ``bars`` bars, expressed in ATR -- the objective form of "strong move"."""
    c = np.asarray(close, dtype=np.float64)
    a = np.asarray(atr_values, dtype=np.float64)
    out = np.full(len(c), np.nan)
    for i in range(bars, len(c)):
        if a[i] and not np.isnan(a[i]) and a[i] > 0:
            out[i] = (c[i] - c[i - bars]) / a[i]
    return out


class StructureView:
    """A read-only view of a :class:`StructureSeries` as it stood at bar ``i``.

    Zero-copy. Absolute bar indices remain valid because the underlying arrays are not
    sliced -- only the notion of "now" changes. This is what lets a backtest compute
    structure once over the whole history and still hand each evaluation a state that
    contains no future information.
    """

    __slots__ = ("_i", "_s")

    def __init__(self, series: StructureSeries, i: int) -> None:
        self._s = series
        self._i = i if i >= 0 else series.n + i

    @property
    def n(self) -> int:
        return self._i + 1

    @property
    def swing_trend(self) -> list[TrendState]:
        return self._s.swing_trend

    @property
    def break_state(self) -> list[TrendState]:
        return self._s.break_state

    @property
    def event(self) -> list[StructureEvent]:
        return self._s.event

    @property
    def swings(self) -> list[SwingPoint]:
        return [s for s in self._s.swings if s.confirmed_index <= self._i]

    def at(self, i: int) -> StructureSnapshot:
        if i < 0:
            i += self.n
        if i > self._i:
            raise IndexError(f"bar {i} is in the future of this view (now = {self._i})")
        return self._s.at(i)

    def last(self) -> StructureSnapshot:
        return self._s.at(self._i)
