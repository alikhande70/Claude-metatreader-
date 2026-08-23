"""The look-ahead property tests.

The whole system computes features over a full history in one vectorised pass and then
indexes into the result. That is only legitimate if every value at bar ``i`` depends on bars
``0..i`` and nothing later. These tests verify it the only way that actually proves it:
**recompute on a truncated series and demand identical values**.

If a feature ever reads the future -- an indicator seeded from the whole array, a window size
derived from ``len(series)``, a threshold taken from the final bar -- the truncated
recomputation diverges and these tests fail. Two such leaks were caught this way during
development (a length-dependent percentile window, and a liquidity tolerance taken from the
last bar's ATR), which is the point.
"""

from __future__ import annotations

import numpy as np
import pytest

from atlas.core.enums import Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.data.series import BarSeries
from atlas.data.synthetic import SyntheticConfig, generate
from atlas.features import indicators as ind
from atlas.features.frame import FeatureConfig, FeatureFrame
from atlas.features.structure import analyse_structure, find_fvgs, find_swings

GOLD = SymbolSpec(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
                  contract_size=100, volume_min=0.01, volume_max=50, volume_step=0.01,
                  stops_level_points=50)

BARS = generate(SyntheticConfig(bars=1600, seed=21))
SERIES = BarSeries.from_bars(BARS)


@pytest.mark.parametrize(
    "fn",
    [
        lambda s: ind.sma(s.close, 20),
        lambda s: ind.ema(s.close, 21),
        lambda s: ind.rma(s.close, 14),
        lambda s: ind.atr(s.high, s.low, s.close, 14),
        lambda s: ind.rsi(s.close, 14),
        lambda s: ind.adx(s.high, s.low, s.close, 14)[0],
        lambda s: ind.adx(s.high, s.low, s.close, 14)[1],
        lambda s: ind.efficiency_ratio(s.close, 20),
        lambda s: ind.donchian(s.high, s.low, 20)[0],
        lambda s: ind.donchian(s.high, s.low, 20)[1],
        lambda s: ind.rolling_percentile(ind.atr(s.high, s.low, s.close, 14), 200),
        lambda s: ind.realized_vol(s.close, 20),
        lambda s: ind.body_ratio(s.open, s.high, s.low, s.close),
        lambda s: ind.slope(s.close, 10),
    ],
    ids=["sma", "ema", "rma", "atr", "rsi", "adx", "plus_di", "er", "donch_hi", "donch_lo",
         "atr_pct", "realized_vol", "body_ratio", "slope"],
)
def test_indicator_value_never_changes_when_future_bars_arrive(fn):
    """The defining property of a causal indicator."""
    full = fn(SERIES)
    for cut in (700, 1000, 1300):
        truncated = fn(SERIES.head(cut))
        a, b = full[:cut], truncated
        both_nan = np.isnan(a) & np.isnan(b)
        assert np.allclose(a[~both_nan], b[~both_nan], rtol=0, atol=1e-12, equal_nan=True), (
            f"value at bars 0..{cut} changed once later bars were appended"
        )


def test_swings_are_only_reported_once_confirmed():
    """A swing at bar k with strength N must not be visible before bar k + N."""
    n = 3
    for cut in (400, 800, 1200):
        s = SERIES.head(cut)
        sw = find_swings(s.high, s.low, s.ts, strength=n)
        assert all(x.confirmed_index <= cut - 1 for x in sw)
        assert all(x.confirmed_index == x.index + n for x in sw)
        # every swing visible at `cut` must also be visible in the full series, unchanged
        full = {(x.index, x.kind): x.price for x in find_swings(SERIES.high, SERIES.low, SERIES.ts, n)}
        for x in sw:
            assert full[(x.index, x.kind)] == x.price


def test_structure_state_is_stable_under_truncation():
    atr = ind.atr(SERIES.high, SERIES.low, SERIES.close, 14)
    margin = 0.1 * np.nan_to_num(atr)
    minr = 2.0 * np.nan_to_num(atr)
    full = analyse_structure(SERIES.high, SERIES.low, SERIES.close, SERIES.ts,
                             strength=3, break_margin=margin, min_range=minr)
    for cut in (600, 900, 1400):
        s = SERIES.head(cut)
        a = ind.atr(s.high, s.low, s.close, 14)
        part = analyse_structure(s.high, s.low, s.close, s.ts, strength=3,
                                 break_margin=0.1 * np.nan_to_num(a),
                                 min_range=2.0 * np.nan_to_num(a))
        assert part.swing_trend[: cut] == full.swing_trend[:cut]
        assert part.break_state[: cut] == full.break_state[:cut]
        assert part.event[: cut] == full.event[:cut]
        assert np.allclose(part.range_high[:cut], full.range_high[:cut], equal_nan=True)
        assert np.allclose(part.range_low[:cut], full.range_low[:cut], equal_nan=True)


def test_fvgs_are_knowable_at_the_third_bar_and_stable():
    atr = ind.atr(SERIES.high, SERIES.low, SERIES.close, 14)
    full = find_fvgs(SERIES.high, SERIES.low, SERIES.ts, min_height=0.3 * np.nan_to_num(atr))
    for cut in (500, 1100):
        s = SERIES.head(cut)
        a = ind.atr(s.high, s.low, s.close, 14)
        part = find_fvgs(s.high, s.low, s.ts, min_height=0.3 * np.nan_to_num(a))
        assert all(g.index < cut for g in part)
        assert [(g.index, g.top, g.bottom, g.bullish) for g in part] == [
            (g.index, g.top, g.bottom, g.bullish) for g in full if g.index < cut
        ]


def test_full_feature_snapshot_is_identical_to_truncated_recomputation():
    """The end-to-end guarantee: the feature dict a strategy sees at bar i is the same
    whether or not the run continued past bar i."""
    cfg = FeatureConfig()
    full = FeatureFrame.compute(SERIES, GOLD, cfg)
    for cut in (700, 1000, 1350, 1599):
        part = FeatureFrame.compute(SERIES.head(cut + 1), GOLD, cfg)
        a, b = full.values(cut), part.values(-1)
        assert set(a) == set(b)
        for k in a:
            assert a[k] == pytest.approx(b[k], rel=0, abs=1e-9), (
                f"feature {k!r} at bar {cut} differs once the future is known: {a[k]} vs {b[k]}"
            )


def test_liquidity_pools_and_sweeps_are_stable_under_truncation():
    """Regression: pool clustering once used the FINAL bar's ATR as its tolerance, which
    leaked future volatility into every historical pool."""
    cfg = FeatureConfig()
    full = FeatureFrame.compute(SERIES, GOLD, cfg)
    for cut in (800, 1200):
        part = FeatureFrame.compute(SERIES.head(cut + 1), GOLD, cfg)
        fp = [(p.price, p.kind, p.touches) for p in full.pools_at(cut)]
        pp = [(p.price, p.kind, p.touches) for p in part.pools_at(-1)]
        assert fp == pp
        sf, sp = full.sweep_at(cut), part.sweep_at(-1)
        assert (sf is None) == (sp is None)
        if sf is not None and sp is not None:
            assert sf.price == pytest.approx(sp.price)


def test_active_fvgs_match_under_truncation():
    cfg = FeatureConfig()
    full = FeatureFrame.compute(SERIES, GOLD, cfg)
    for cut in (900, 1300):
        part = FeatureFrame.compute(SERIES.head(cut + 1), GOLD, cfg)
        assert [(g.index, g.top) for g in full.active_fvgs(cut)] == [
            (g.index, g.top) for g in part.active_fvgs(-1)
        ]


def test_donchian_excludes_the_current_bar_so_a_breakout_can_exist():
    """A channel that includes the breaking bar can never be broken."""
    s = SERIES.head(300)
    hi_excl, _ = ind.donchian(s.high, s.low, 20, exclude_current=True)
    hi_incl, _ = ind.donchian(s.high, s.low, 20, exclude_current=False)
    assert np.any(s.high[25:] > hi_excl[25:]), "no breakout is ever possible with this channel"
    assert not np.any(s.high[25:] > hi_incl[25:] + 1e-12)


def test_feature_frame_reports_warmup_honestly():
    cfg = FeatureConfig()
    short = FeatureFrame.compute(SERIES.head(30), GOLD, cfg)
    assert not short.ready(), "must not claim readiness inside the warm-up region"
    full = FeatureFrame.compute(SERIES, GOLD, cfg)
    assert full.ready()
    assert not full.ready(5)
    assert not full.ready(9999)


def test_htf_alignment_never_exposes_an_unclosed_bar():
    """ADR-014/015: HTF series contain only closed bars, so `last()` is inherently shift-1."""
    from atlas.data.aggregator import MultiTimeframeAggregator

    agg = MultiTimeframeAggregator("XAUUSD", Timeframe.M5, [Timeframe.H1])
    h1 = BarSeries("XAUUSD", Timeframe.H1)
    for bar in BARS[:200]:
        for tf, closed in agg.push(bar).items():
            if tf is Timeframe.H1:
                h1.append(closed)
        if len(h1):
            forming = agg.forming().get(Timeframe.H1)
            assert forming is not None
            assert h1.last().ts < forming.ts, "the last stored H1 bar must precede the forming one"
            assert h1.last().ts_close <= bar.ts_close
