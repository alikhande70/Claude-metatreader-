"""Market structure semantics on hand-constructed series where the answer is known."""

from __future__ import annotations

import numpy as np
import pytest

from atlas.core.enums import StructureEvent
from atlas.features.structure import (
    analyse_structure,
    detect_sweep,
    displacement,
    find_fvgs,
    find_liquidity_pools,
    find_swings,
    unmitigated_fvgs,
)


def series(closes, wick=0.6):
    c = np.asarray(closes, dtype=float)
    return c + wick, c - wick, c, np.arange(len(c)) * 300_000


def test_swing_detection_and_confirmation_lag():
    h, low, _c, ts = series([100, 101, 103, 102, 101, 104, 106, 105, 104, 108])
    sw = find_swings(h, low, ts, strength=2)
    highs = [s for s in sw if s.kind == "HIGH"]
    assert [s.index for s in highs] == [2, 6]
    assert all(s.confirmed_index == s.index + 2 for s in sw)
    assert all(s.lag == 2 for s in sw)


def test_swings_near_the_right_edge_are_not_reported():
    h, low, c, ts = series([*range(20), 30, 20])  # a spike at index 20
    sw = find_swings(h, low, ts, strength=3)
    assert all(s.index <= len(c) - 1 - 3 for s in sw), "unconfirmed swings stay invisible"


def test_choch_then_bos_sequencing():
    """A break against the prevailing break-state is a CHoCH; the next one with it is a BOS."""
    h, low, c, ts = series([100, 101, 103, 102, 101, 104, 106, 105, 104, 108, 110])
    st = analyse_structure(h, low, c, ts, strength=2)
    events = [(i, st.event[i]) for i in range(len(c)) if st.event[i] is not StructureEvent.NONE]
    kinds = [e for _, e in events]
    assert kinds[0] is StructureEvent.CHOCH_UP
    assert StructureEvent.BOS_UP in kinds[1:]


def test_a_swing_generates_at_most_one_break():
    """Without consuming the reference level, every subsequent bar would re-fire the same BOS."""
    h, low, c, ts = series([100, 101, 103, 102, 101, 104, 105, 106, 107, 108, 109, 110])
    st = analyse_structure(h, low, c, ts, strength=2)
    ups = [i for i in range(len(c)) if st.event[i] in
           (StructureEvent.BOS_UP, StructureEvent.CHOCH_UP)]
    assert len(ups) <= 3, f"too many break events for a single monotonic advance: {ups}"


def test_break_margin_suppresses_marginal_breaks():
    # wick=0 so the swing high equals the close and the overshoot is exactly 0.05
    h, low, c, ts = series([100, 101, 103, 102, 101, 103.05], wick=0.0)
    no_margin = analyse_structure(h, low, c, ts, strength=2)
    with_margin = analyse_structure(h, low, c, ts, strength=2,
                                    break_margin=np.full(len(c), 1.0))
    assert any(e is not StructureEvent.NONE for e in no_margin.event)
    assert all(e is StructureEvent.NONE for e in with_margin.event), (
        "a 0.05 overshoot must not count as a break when the margin is 1.0"
    )


def test_swing_trend_needs_two_confirmed_swings_of_each_kind():
    h, low, c, ts = series([100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 105, 107])
    st = analyse_structure(h, low, c, ts, strength=1)
    assert st.swing_trend[0] == "RANGE"
    assert "BULL" in st.swing_trend, "an HH/HL sequence must eventually register as bullish"


def test_choch_does_not_flip_swing_trend_immediately():
    """The documented specification choice: break_state is responsive, swing_trend is not."""
    closes = [100, 101, 103, 102, 101, 104, 106, 105, 104, 108, 110, 109, 107, 105, 103, 101]
    h, low, c, ts = series(closes)
    st = analyse_structure(h, low, c, ts, strength=2)
    i = next(i for i in range(len(c)) if st.event[i] is StructureEvent.CHOCH_DOWN)
    assert st.break_state[i] == "BEAR"
    assert st.swing_trend[i] == "BULL", "swing_trend must wait for new confirmed swings"


def test_dealing_range_is_never_degenerate():
    """Regression: using only the two most recent opposite swings collapsed the range to
    near-zero in chop, which made position_in_range diverge."""
    rng = np.random.default_rng(1)
    c = 100 + np.cumsum(rng.normal(0, 0.1, 800))
    h, low, cc, ts = c + 0.05, c - 0.05, c, np.arange(len(c)) * 300_000
    st = analyse_structure(h, low, cc, ts, strength=3, min_range=np.full(len(c), 0.5))
    sizes = [st.at(i).range_size for i in range(400, len(c))]
    assert min(sizes) >= 0.5 - 1e-9, "the min_range post-condition must hold at every bar"
    positions = [abs(st.at(i).position_in_range(cc[i])) for i in range(400, len(c))]
    assert max(positions) < 50, "position_in_range must not explode"


def test_premium_discount():
    h, low, c, ts = series([100, 110, 100, 110, 100, 110, 100])
    st = analyse_structure(h, low, c, ts, strength=1)
    snap = st.last()
    assert snap.is_discount(snap.equilibrium - 1)
    assert snap.is_premium(snap.equilibrium + 1)
    assert snap.position_in_range(snap.range_low) == pytest.approx(0.0)
    assert snap.position_in_range(snap.range_high) == pytest.approx(1.0)


def test_fvg_detection_and_direction():
    # bar0 high=101, bar1 gaps up, bar2 low=104 -> bullish gap [101, 104]
    h = np.array([101.0, 106.0, 108.0])
    low = np.array([99.0, 102.0, 104.0])
    g = find_fvgs(h, low, np.arange(3) * 1000)
    assert len(g) == 1 and g[0].bullish
    assert (g[0].bottom, g[0].top) == (101.0, 104.0)
    assert g[0].index == 2, "an FVG is knowable at the third bar, with no lag"

    h2 = np.array([108.0, 104.0, 101.0])
    low2 = np.array([104.0, 100.0, 99.0])
    g2 = find_fvgs(h2, low2, np.arange(3) * 1000)
    assert len(g2) == 1 and not g2[0].bullish
    assert (g2[0].bottom, g2[0].top) == (101.0, 104.0)


def test_fvg_min_height_filters_noise():
    h = np.array([101.0, 106.0, 108.0])
    low = np.array([99.0, 102.0, 101.5])  # gap of only 0.5
    assert find_fvgs(h, low, np.arange(3) * 1000, min_height=np.full(3, 2.0)) == []
    assert len(find_fvgs(h, low, np.arange(3) * 1000, min_height=np.full(3, 0.1))) == 1


def test_fvg_mitigation_removes_the_gap():
    h = np.array([101.0, 106.0, 108.0, 107.0, 106.0, 105.0])
    low = np.array([99.0, 102.0, 104.0, 103.0, 102.0, 100.0])  # bar 5 trades below 101
    gaps = find_fvgs(h, low, np.arange(6) * 1000)
    assert len(unmitigated_fvgs(gaps, h, low, upto=3)) == 1
    assert unmitigated_fvgs(gaps, h, low, upto=5) == [], "traded through the far edge -> gone"


def test_fvg_expiry():
    h = np.array([101.0, 106.0, 108.0] + [107.5] * 50)
    low = np.array([99.0, 102.0, 104.0] + [105.0] * 50)
    gaps = find_fvgs(h, low, np.arange(len(h)) * 1000)
    assert len(unmitigated_fvgs(gaps, h, low, upto=20, max_age=30)) == 1
    assert unmitigated_fvgs(gaps, h, low, upto=40, max_age=30) == []


def test_liquidity_pools_cluster_equal_highs():
    h, low, _c, ts = series([100, 110, 100, 110.05, 100, 109.98, 100])
    sw = find_swings(h, low, ts, strength=1)
    pools = find_liquidity_pools(sw, tolerance=0.5)
    highs = [p for p in pools if p.kind == "HIGH"]
    assert highs and highs[0].touches >= 3
    # series() adds a 0.6 wick, so a swing high at close 110.0 has price 110.6
    assert highs[0].price == pytest.approx(110.61, abs=0.05)
    assert find_liquidity_pools(sw, tolerance=0.001) == [] or all(
        p.touches >= 2 for p in find_liquidity_pools(sw, tolerance=0.001)
    )


def test_sweep_requires_penetration_then_reclaim():
    from atlas.features.structure import LiquidityPool

    pool = LiquidityPool(price=110.0, kind="HIGH", touches=2, last_index=0)
    h = np.array([109.0, 111.0, 110.5])
    low = np.array([108.0, 109.0, 108.5])
    c = np.array([108.5, 110.8, 109.0])  # spiked above 110, closed back below
    assert detect_sweep(h, low, c, 2, [pool], penetration=0.5) is pool
    c_no_reclaim = np.array([108.5, 110.8, 110.9])  # still above the pool
    assert detect_sweep(h, low, c_no_reclaim, 2, [pool], penetration=0.5) is None
    assert detect_sweep(h, low, c, 2, [pool], penetration=5.0) is None  # not deep enough


def test_sweep_ignores_pools_confirmed_later():
    from atlas.features.structure import LiquidityPool

    future = LiquidityPool(price=110.0, kind="HIGH", touches=2, last_index=99)
    h = np.array([109.0, 111.0, 110.5])
    low = np.array([108.0, 109.0, 108.5])
    c = np.array([108.5, 110.8, 109.0])
    assert detect_sweep(h, low, c, 2, [future], penetration=0.5) is None


def test_displacement_is_in_atr_units():
    c = np.arange(20.0)
    atr = np.full(20, 2.0)
    d = displacement(c, atr, bars=3)
    assert d[-1] == pytest.approx(1.5)  # 3 units of move / 2.0 ATR
    assert np.isnan(d[:3]).all()
