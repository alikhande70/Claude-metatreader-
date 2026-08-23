"""Position sizing from broker specification.

Two rules that are constantly got wrong, and are enforced here rather than left to callers:

* **Round down, never to nearest.** Rounding up silently pushes risk above the stated limit --
  a small error per trade and a systematic one across thousands.
* **Size from equity, not balance.** With positions open, balance overstates what is actually
  available, and every prop drawdown rule is computed on equity.

After rounding, the *realised* money at risk is recomputed from the final lot size. That, not
the intended figure, is what gets journalled and reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas.core.instrument import SymbolSpec


@dataclass(frozen=True, slots=True)
class SizingResult:
    volume: float
    risk_money: float  # realised, after lot rounding
    intended_risk_money: float
    risk_pct: float
    ideal_volume: float
    stop_points: float
    margin_required: float
    drift: float  # (intended - realised) / intended; positive means we are under-risked

    @property
    def tradeable(self) -> bool:
        return self.volume > 0


def size_position(
    spec: SymbolSpec,
    equity: float,
    risk_pct: float,
    stop_points: float,
    *,
    leverage: int = 100,
    conviction: float | None = None,
    min_fraction: float = 0.5,
) -> SizingResult:
    """Compute lot size for a given risk budget and stop distance.

    ``conviction`` in [0, 1] scales the risk between ``min_fraction`` and 1.0 of the base.
    It can only ever *reduce* size below the configured maximum.
    """
    if equity <= 0:
        raise ValueError("equity must be positive")
    if stop_points <= 0:
        raise ValueError("stop_points must be positive")

    scale = 1.0
    if conviction is not None:
        c = min(1.0, max(0.0, conviction))
        scale = min_fraction + (1.0 - min_fraction) * c
    intended = equity * (risk_pct / 100.0) * scale

    ideal = spec.volume_for_risk(intended, stop_points)
    volume = spec.normalize_volume(ideal)
    realised = spec.money_for_points(stop_points, volume) if volume > 0 else 0.0
    drift = 0.0 if intended <= 0 else (intended - realised) / intended

    margin = _margin_for(spec, volume, leverage)
    return SizingResult(
        volume=volume,
        risk_money=realised,
        intended_risk_money=intended,
        risk_pct=(realised / equity * 100.0) if equity > 0 else 0.0,
        ideal_volume=ideal,
        stop_points=stop_points,
        margin_required=margin,
        drift=drift,
    )


def _margin_for(spec: SymbolSpec, volume: float, leverage: int) -> float:
    """Margin estimate.

    Prefers the broker's own ``margin_initial`` per lot. The leverage fallback is an
    approximation (it ignores the margin currency conversion) and is only used when the
    broker did not report a value -- it is flagged as approximate wherever it is displayed.
    """
    if volume <= 0:
        return 0.0
    if spec.margin_initial > 0:
        return spec.margin_initial * volume
    if leverage <= 0:
        return 0.0
    notional_per_lot = spec.contract_size
    return notional_per_lot * volume / leverage


def kelly_fraction(win_rate: float, payoff: float) -> float:
    """Full Kelly. Reported for context only -- never used to size.

    Full Kelly maximises long-run growth but assumes the edge is known exactly, which it
    never is, and produces drawdowns no human tolerates. Quarter Kelly is the practical
    ceiling, and the useful diagnostic is whether the configured risk sits above it.
    """
    if payoff <= 0:
        return 0.0
    f = win_rate - (1.0 - win_rate) / payoff
    return max(0.0, f)


def risk_of_ruin(win_rate: float, payoff: float, risk_pct: float, ruin_pct: float = 50.0) -> float:
    """Monte-Carlo-free approximation of the probability of losing ``ruin_pct`` of equity.

    Uses the classic gambler's-ruin form on R-multiples. It is an approximation -- it assumes
    independent trades and a fixed payoff -- and is presented as an order of magnitude, not a
    precise probability.
    """
    if risk_pct <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    edge = win_rate * payoff - (1 - win_rate)
    if edge <= 0:
        return 1.0
    units = ruin_pct / risk_pct
    # Probability that a random walk with per-step mean `edge` and step size 1R ever falls
    # `units` R below its start.
    variance = win_rate * payoff**2 + (1 - win_rate) - edge**2
    if variance <= 0:
        return 0.0
    exponent = -2.0 * edge * units / variance
    import math

    return float(min(1.0, math.exp(exponent)))
