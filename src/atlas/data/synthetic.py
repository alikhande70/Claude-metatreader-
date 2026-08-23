"""Synthetic market generator.

**Scope and honesty.** This produces data with realistic *structure* -- volatility
clustering, regime switching, session-dependent activity, weekend gaps and a spread that
widens when liquidity thins. That makes it a genuine test bed for the *machinery*: the
aggregator, the feature pipeline, the execution simulator, the risk engine and the
end-to-end run loop.

It is **not** evidence about any strategy's edge. A strategy tuned on synthetic data has
been tuned on this generator's assumptions, not on a market. Any performance number derived
from it is labelled as such throughout ATLAS, and ``atlas validate`` refuses to mark a
configuration approved when its data source is synthetic.

Model
-----
Regime-switching drift with a GARCH(1,1)-style variance process, sampled on a sub-minute
grid so that each bar's high and low come from an actual simulated path rather than from a
fabricated range around the close.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from atlas.core.enums import Regime, Timeframe
from atlas.core.market import Bar, ms_to_dt


@dataclass(slots=True)
class SyntheticConfig:
    symbol: str = "XAUUSD"
    start_ms: int = 1_735_689_600_000  # 2025-01-01T00:00Z
    bars: int = 20_000
    tf: Timeframe = Timeframe.M5
    start_price: float = 2600.0
    point: float = 0.01

    #: Annualised volatility floor, as a fraction. Gold sits around 0.15-0.20.
    base_vol_annual: float = 0.16
    #: GARCH(1,1) parameters: persistence (beta) and shock sensitivity (alpha).
    garch_alpha: float = 0.08
    garch_beta: float = 0.90

    #: Mean regime duration in bars, and per-regime annualised drift.
    regime_mean_bars: int = 900
    trend_drift_annual: float = 0.35
    range_reversion: float = 0.05

    #: Cost model of the *generated feed* (the simulator applies its own on top).
    spread_points_base: float = 22.0
    spread_points_rollover: float = 180.0
    spread_vol_sensitivity: float = 1.5

    #: Session activity multipliers keyed by UTC hour.
    session_multipliers: dict[int, float] = field(default_factory=dict)

    seed: int = 7
    include_weekends: bool = False
    substeps: int = 12  # intrabar path resolution


_DEFAULT_HOURLY_ACTIVITY = {
    0: 0.55, 1: 0.50, 2: 0.50, 3: 0.45, 4: 0.45, 5: 0.55, 6: 0.75,
    7: 1.20, 8: 1.45, 9: 1.30, 10: 1.10, 11: 1.00, 12: 1.35, 13: 1.60,
    14: 1.50, 15: 1.30, 16: 1.05, 17: 0.90, 18: 0.75, 19: 0.65,
    20: 0.60, 21: 0.45, 22: 0.40, 23: 0.45,
}


def _activity(hour: int, cfg: SyntheticConfig) -> float:
    return cfg.session_multipliers.get(hour, _DEFAULT_HOURLY_ACTIVITY.get(hour, 1.0))


def generate(cfg: SyntheticConfig | None = None) -> list[Bar]:
    """Generate a bar series. Deterministic for a given ``seed``."""
    cfg = cfg or SyntheticConfig()
    rng = np.random.default_rng(cfg.seed)

    bar_seconds = cfg.tf.seconds
    bars_per_year = 365.25 * 24 * 3600 / bar_seconds
    base_var = (cfg.base_vol_annual**2) / bars_per_year

    regimes = (Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGE)
    regime = Regime.RANGE
    regime_left = int(rng.exponential(cfg.regime_mean_bars))

    price = cfg.start_price
    var = base_var
    anchor = price  # range-regime mean-reversion anchor

    out: list[Bar] = []
    ts = cfg.start_ms
    step_ms = bar_seconds * 1000
    made = 0
    guard = 0
    max_guard = cfg.bars * 6 + 10_000

    while made < cfg.bars and guard < max_guard:
        guard += 1
        dt = ms_to_dt(ts)
        wd, hour = dt.weekday(), dt.hour
        closed = (not cfg.include_weekends) and (
            wd == 5 or (wd == 4 and hour >= 21) or (wd == 6 and hour < 21)
        )
        if closed:
            ts += step_ms
            continue

        if regime_left <= 0:
            regime = regimes[int(rng.integers(0, 3))]
            regime_left = max(60, int(rng.exponential(cfg.regime_mean_bars)))
            anchor = price
        regime_left -= 1

        act = _activity(hour, cfg)
        step_var = var * act
        sigma = float(np.sqrt(max(step_var, 1e-14)))

        if regime is Regime.TREND_UP:
            mu = cfg.trend_drift_annual / bars_per_year
        elif regime is Regime.TREND_DOWN:
            mu = -cfg.trend_drift_annual / bars_per_year
        else:
            mu = cfg.range_reversion * (anchor - price) / max(price, 1e-9) / 20.0

        # Weekend gap: the first bar back applies an extra shock.
        gap = 0.0
        if out and (ts - out[-1].ts) > 4 * step_ms:
            gap = float(rng.normal(0.0, sigma * 6.0))

        n = max(2, cfg.substeps)
        shocks = rng.normal(mu / n + gap / n, sigma / np.sqrt(n), n)
        path = price * np.exp(np.cumsum(shocks))
        o, c = price, float(path[-1])
        h, low = float(max(o, path.max())), float(min(o, path.min()))

        realised = float(np.sum(shocks**2))
        var = (
            base_var * (1 - cfg.garch_alpha - cfg.garch_beta)
            + cfg.garch_alpha * realised
            + cfg.garch_beta * var
        )
        var = float(np.clip(var, base_var * 0.15, base_var * 30.0))
        price = c

        vol_ratio = np.sqrt(step_var / base_var)
        spread = cfg.spread_points_base * (
            1.0 + cfg.spread_vol_sensitivity * max(0.0, vol_ratio - 1.0)
        )
        spread /= max(0.35, act)
        if hour >= 21 or hour < 1:  # rollover window: liquidity collapses
            spread = max(spread, cfg.spread_points_rollover * float(rng.uniform(0.6, 1.4)))
        spread = float(max(cfg.spread_points_base * 0.6, spread))

        digits = round(-float(np.log10(cfg.point)))
        out.append(
            Bar(
                symbol=cfg.symbol, tf=cfg.tf, ts=ts,
                open=round(o, digits), high=round(h, digits),
                low=round(low, digits), close=round(c, digits),
                volume=float(max(1, int(rng.poisson(120 * act)))),
                spread_points=round(spread, 1), complete=True,
            )
        )
        made += 1
        ts += step_ms

    return out


def generate_quotes_from_bar(bar: Bar, point: float, n: int = 4) -> list[tuple[int, float, float]]:
    """Expand one bar into a plausible intrabar quote path ``(ts, bid, ask)``.

    Order matters and is deliberately conservative for a trader: the adverse extreme is
    visited **before** the favourable one (low-then-high for a bullish bar). When both a stop
    and a target sit inside the same bar, the simulator therefore resolves the stop first --
    the pessimistic assumption, and the only defensible one without tick data.
    """
    half = bar.spread_points * point / 2.0
    if bar.is_bullish:
        mids = [bar.open, bar.low, bar.high, bar.close]
    else:
        mids = [bar.open, bar.high, bar.low, bar.close]
    span = max(1, len(mids) - 1)
    dur = bar.tf.seconds * 1000
    return [
        (bar.ts + int(i * dur / span), round(m - half, 8), round(m + half, 8))
        for i, m in enumerate(mids[:n])
    ]
