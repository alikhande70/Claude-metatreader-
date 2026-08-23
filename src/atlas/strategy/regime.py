"""Regime classification.

A regime label is not a prediction; it is a statement about the *character* of recent price
action, and its job is to keep a strategy out of the conditions it was not designed for. SM1
is a continuation system and loses money in chop, so the honest thing is to detect chop and
stand aside rather than to add filters until the backtest looks better.

Three inputs, chosen because they measure different things:

* **ADX** -- trend strength via directional movement.
* **Efficiency ratio** -- how much ground price covered relative to the distance travelled.
  Bounded [0, 1] and directly interpretable, unlike ADX.
* **ATR percentile** -- where current volatility sits in its own recent distribution, which is
  what makes "too volatile to trade" comparable across instruments and eras.

They disagree often, and that disagreement is itself informative: requiring *either* ADX or
ER to clear its threshold (rather than both) keeps the filter from being so strict that the
sample collapses.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas.core.enums import Regime
from atlas.features.frame import FeatureFrame


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    adx_trend_min: float = 22.0
    er_trend_min: float = 0.35
    atr_pct_max: float = 0.95
    atr_pct_min: float = 0.05


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    regime: Regime
    adx: float
    er: float
    atr_pct: float
    detail: str

    @property
    def is_trending(self) -> bool:
        return self.regime in (Regime.TREND_UP, Regime.TREND_DOWN)

    @property
    def is_shock(self) -> bool:
        return self.regime is Regime.HIGH_VOL_SHOCK


def classify(frame: FeatureFrame, cfg: RegimeConfig, i: int = -1) -> RegimeAssessment:
    if not frame.ready(i):
        return RegimeAssessment(Regime.UNKNOWN, 0.0, 0.0, 0.0, "features not warmed up")
    i = i if i >= 0 else frame.n + i
    adx = float(frame.adx[i])
    er = float(frame.er[i])
    atr_pct = float(frame.atr_pct[i])
    di_spread = float(frame.plus_di[i]) - float(frame.minus_di[i])

    if atr_pct > cfg.atr_pct_max:
        return RegimeAssessment(
            Regime.HIGH_VOL_SHOCK, adx, er, atr_pct,
            f"ATR at the {atr_pct:.0%} percentile of its own recent range",
        )
    trending = adx >= cfg.adx_trend_min or er >= cfg.er_trend_min
    if not trending:
        return RegimeAssessment(
            Regime.RANGE, adx, er, atr_pct,
            f"ADX {adx:.1f} < {cfg.adx_trend_min} and ER {er:.2f} < {cfg.er_trend_min}",
        )
    direction = Regime.TREND_UP if di_spread >= 0 else Regime.TREND_DOWN
    return RegimeAssessment(
        direction, adx, er, atr_pct,
        f"ADX {adx:.1f}, ER {er:.2f}, DI spread {di_spread:+.1f}",
    )
