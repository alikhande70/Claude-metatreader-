"""Execution cost model for the simulator.

The purpose of this module is to make the simulator **pessimistic and honest**. A simulator
that fills at the signal price makes every strategy look profitable; the useful one is the
one a real edge can still survive.

Defaults are set for a 2-digit XAUUSD ECN account and should be replaced with values measured
from the user's own fill history (``atlas analyse slippage``) as soon as live trades exist.
Until then they are assumptions, and they are labelled as such in every report.

Deliberate pessimism, stated explicitly:

* **Entry slippage is adverse-only by default.** Real slippage is roughly symmetric with an
  adverse skew, so this over-charges slightly. Set ``allow_positive_slippage=True`` to model
  it symmetrically; the asymmetric default is the safer starting assumption.
* **Stop-fill slippage is always adverse and larger than entry slippage.** Stops execute
  during exactly the fast one-way moves that produce gaps, so a stop that "should" lose 1R
  routinely loses more.
* **Slippage scales with spread.** Wide spread is the observable proxy for thin liquidity,
  so the same model produces small slippage in the London session and large slippage at
  rollover without a separate time-of-day rule.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from atlas.core.instrument import SymbolSpec


@dataclass(frozen=True, slots=True)
class CostModel:
    commission_per_lot_per_side: float = 3.5

    entry_slippage_points: float = 2.0
    entry_slippage_std: float = 1.5
    stop_slippage_points: float = 6.0
    stop_slippage_std: float = 4.0
    allow_positive_slippage: bool = False

    #: Slippage is multiplied by (spread / reference_spread) ** this exponent.
    slippage_spread_exponent: float = 1.0
    reference_spread_points: float = 25.0

    #: Probability a market order is rejected outright (requote / no liquidity).
    reject_probability: float = 0.0

    swap_enabled: bool = True
    #: Fallback triple-swap weekday if the symbol spec does not say. MT5's default is
    #: Wednesday, but it varies by broker AND by symbol, so the spec value wins.
    triple_swap_weekday: int = 2

    def commission(self, volume: float) -> float:
        """Round-turn commission for ``volume`` lots, as a negative number."""
        return -abs(self.commission_per_lot_per_side) * volume * 2.0

    def commission_one_side(self, volume: float) -> float:
        return -abs(self.commission_per_lot_per_side) * volume

    def _scale(self, spread_points: float) -> float:
        if self.reference_spread_points <= 0:
            return 1.0
        ratio = max(0.1, spread_points / self.reference_spread_points)
        return float(ratio**self.slippage_spread_exponent)

    def entry_slippage(self, rng: np.random.Generator, spread_points: float) -> float:
        """Adverse entry slippage in points (>= 0 unless positive slippage is enabled)."""
        s = rng.normal(self.entry_slippage_points, self.entry_slippage_std) * self._scale(
            spread_points
        )
        return float(s) if self.allow_positive_slippage else float(max(0.0, s))

    def stop_slippage(self, rng: np.random.Generator, spread_points: float) -> float:
        """Adverse stop-fill slippage in points. Never favourable, by construction."""
        s = rng.normal(self.stop_slippage_points, self.stop_slippage_std) * self._scale(
            spread_points
        )
        return float(max(0.0, s))

    def rejected(self, rng: np.random.Generator) -> bool:
        return self.reject_probability > 0 and bool(rng.random() < self.reject_probability)

    def swap_charge(self, spec: SymbolSpec, volume: float, is_long: bool, weekday: int) -> float:
        """Overnight financing for one rollover, in account currency.

        MT5's ``swap_mode`` decides the unit of ``swap_long``/``swap_short``. Mode 0 is
        "points", mode 1 is "base currency", mode 2 is "interest %", and there are others.
        Only the common point mode is converted here; anything else is passed through as a
        per-lot currency amount, which is what most brokers actually configure.
        """
        if not self.swap_enabled:
            return 0.0
        raw = spec.swap_long if is_long else spec.swap_short
        if raw == 0.0:
            return 0.0
        multiplier = 3.0 if weekday == (spec.swap_rollover_3days % 7) else 1.0
        if spec.swap_mode == 0:  # points
            return spec.money_for_points(raw, volume) * multiplier
        return raw * volume * multiplier


def cost_ratio(
    spec: SymbolSpec,
    model: CostModel,
    *,
    volume: float,
    stop_points: float,
    spread_points: float,
) -> dict[str, float]:
    """Round-turn cost as a fraction of the money risked.

    This ratio is the fastest way to tell whether a strategy shape is viable at all:

    ======  ==============================================================
    < 5%    negligible -- typical of swing systems with wide stops
    5-15%   meaningful; the strategy needs a solid edge
    15-30%  heavy; most strategies in this band do not survive
    > 30%   cost, not the market, determines the outcome
    ======  ==============================================================

    Because cost is roughly fixed per trade while risk scales with the stop, doubling the
    stop distance roughly halves this ratio. That is why the standard fix for a
    cost-constrained system is a wider stop and lower frequency, not a better entry.
    """
    risk_money = spec.money_for_points(stop_points, volume)
    spread_cost = spec.money_for_points(spread_points, volume)
    commission = abs(model.commission(volume))
    slip = spec.money_for_points(
        model.entry_slippage_points + model.stop_slippage_points, volume
    )
    total = spread_cost + commission + slip
    return {
        "risk_money": risk_money,
        "spread_cost": spread_cost,
        "commission": commission,
        "slippage_cost": slip,
        "total_cost": total,
        "cost_ratio": (total / risk_money) if risk_money > 0 else float("inf"),
    }
