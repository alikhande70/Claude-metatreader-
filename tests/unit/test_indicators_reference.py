"""Indicator values checked against published references and analytic edge cases.

Testing an indicator only against itself proves nothing. Where a canonical reference exists
(Wilder's own worked example for RSI) it is used; elsewhere the test uses inputs whose
correct answer can be derived by hand.
"""

from __future__ import annotations

import numpy as np
import pytest

from atlas.features import indicators as ind

# The 33-close series from Wilder's "New Concepts in Technical Trading Systems", used by
# virtually every RSI implementation as its reference.
WILDER = np.array([
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03,
    45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45,
    45.78, 45.35, 44.03, 44.18, 44.22, 44.57, 43.42, 42.66, 43.13,
])


def test_rsi_matches_wilders_published_values():
    r = ind.rsi(WILDER, 14)
    assert r[14] == pytest.approx(70.46, abs=0.02)
    assert r[15] == pytest.approx(66.25, abs=0.02)
    assert r[-1] == pytest.approx(37.77, abs=0.05)
    assert np.isnan(r[:14]).all(), "no RSI value may exist before the warm-up completes"


@pytest.mark.parametrize("series,expected", [
    (np.arange(1.0, 60.0), 100.0),          # unbroken advance
    (np.arange(59.0, 0.0, -1.0), 0.0),      # unbroken decline
    (np.full(60, 5.0), 50.0),               # perfectly flat
])
def test_rsi_edge_cases_are_finite(series, expected):
    """A flat market must not produce NaN/inf -- that would silently disable every gate."""
    v = ind.rsi(series, 14)[-1]
    assert np.isfinite(v) and v == pytest.approx(expected)


def test_sma_and_ema_against_hand_values():
    v = np.arange(1.0, 11.0)
    assert ind.sma(v, 5)[-1] == pytest.approx(8.0)  # mean(6..10)
    # EMA seeded with SMA(first 5) = 3.0, then alpha = 2/6
    e = ind.ema(v, 5)
    acc = 3.0
    for x in v[5:]:
        acc = (2 / 6) * x + (4 / 6) * acc
    assert e[-1] == pytest.approx(acc)


def test_rma_is_wilder_not_ema():
    v = np.arange(1.0, 30.0)
    r, e = ind.rma(v, 14), ind.ema(v, 14)
    assert r[-1] != pytest.approx(e[-1]), "Wilder smoothing must differ from a standard EMA"
    acc = float(v[:14].mean())
    for x in v[14:]:
        acc = (acc * 13 + x) / 14
    assert r[-1] == pytest.approx(acc)


def test_true_range_uses_the_previous_close():
    h = np.array([10.0, 12.0, 11.0])
    low = np.array([9.0, 11.5, 8.0])
    c = np.array([9.5, 11.8, 8.5])
    tr = ind.true_range(h, low, c)
    assert tr[0] == pytest.approx(1.0)  # first bar: own range
    assert tr[1] == pytest.approx(max(0.5, abs(12.0 - 9.5), abs(11.5 - 9.5)))
    assert tr[2] == pytest.approx(max(3.0, abs(11.0 - 11.8), abs(8.0 - 11.8)))


def test_atr_of_constant_range_equals_that_range():
    n = 100
    h = np.full(n, 101.0)
    low = np.full(n, 99.0)
    c = np.full(n, 100.0)
    assert ind.atr(h, low, c, 14)[-1] == pytest.approx(2.0)


def test_efficiency_ratio_bounds():
    ramp = np.arange(100.0)
    assert ind.efficiency_ratio(ramp, 20)[-1] == pytest.approx(1.0)
    zigzag = np.array([100.0 + (i % 2) for i in range(100)])
    assert ind.efficiency_ratio(zigzag, 20)[-1] == pytest.approx(0.0, abs=0.06)
    rng = np.random.default_rng(0)
    walk = 100 + np.cumsum(rng.normal(0, 1, 3000))
    er = ind.efficiency_ratio(walk, 20)
    assert np.nanmin(er) >= 0.0 and np.nanmax(er) <= 1.0


def test_adx_is_high_in_a_trend_and_low_in_a_range():
    n = 300
    trend = np.arange(n, dtype=float)
    h, low, c = trend + 1, trend - 1, trend
    a, pdi, mdi = ind.adx(h, low, c, 14)
    assert a[-1] > 40, "a perfect ramp must register as a strong trend"
    assert pdi[-1] > mdi[-1]

    rng = np.random.default_rng(3)
    chop = 100 + rng.normal(0, 0.3, n).cumsum() * 0.05
    h2, l2 = chop + 1, chop - 1
    a2, _, _ = ind.adx(h2, l2, chop, 14)
    assert a2[-1] < a[-1]


def test_adx_direction_flips_with_the_trend():
    n = 200
    down = np.arange(n, 0, -1, dtype=float)
    _, pdi, mdi = ind.adx(down + 1, down - 1, down, 14)
    assert mdi[-1] > pdi[-1]


def test_rolling_percentile_is_bounded_and_ranks_correctly():
    v = np.arange(100.0)
    p = ind.rolling_percentile(v, 20)
    assert p[-1] == pytest.approx(1.0), "a new maximum must rank at 1.0"
    assert np.nanmin(p) >= 0.0 and np.nanmax(p) <= 1.0
    w = np.concatenate([np.arange(50.0), np.full(1, -1.0)])
    assert ind.rolling_percentile(w, 20)[-1] == pytest.approx(0.0)


def test_donchian_window_length():
    h = np.arange(50.0)
    low = h - 5
    hi, lo = ind.donchian(h, low, 10, exclude_current=True)
    assert hi[20] == pytest.approx(h[10:20].max())
    assert lo[20] == pytest.approx(low[10:20].min())
    hi2, _ = ind.donchian(h, low, 10, exclude_current=False)
    assert hi2[20] == pytest.approx(h[11:21].max())


def test_body_ratio_bounds():
    o = np.array([10.0, 10.0, 10.0])
    h = np.array([12.0, 10.0, 11.0])
    low = np.array([8.0, 10.0, 10.0])
    c = np.array([12.0, 10.0, 10.5])
    br = ind.body_ratio(o, h, low, c)
    assert br[0] == pytest.approx(0.5)
    assert br[1] == 0.0, "a zero-range bar must give 0, not a division by zero"
    assert br[2] == pytest.approx(0.5)


def test_slope_of_a_line_is_its_gradient():
    v = 3.0 * np.arange(50.0) + 7.0
    assert ind.slope(v, 10)[-1] == pytest.approx(3.0)
    assert ind.slope(np.full(50, 5.0), 10)[-1] == pytest.approx(0.0)


def test_short_inputs_return_all_nan_rather_than_raising():
    tiny = np.arange(3.0)
    for fn in (lambda: ind.sma(tiny, 20), lambda: ind.ema(tiny, 20), lambda: ind.rma(tiny, 20),
               lambda: ind.rsi(tiny, 14), lambda: ind.efficiency_ratio(tiny, 20),
               lambda: ind.realized_vol(tiny, 20)):
        out = fn()
        assert len(out) == 3 and np.isnan(out).all()
    a, p, m = ind.adx(tiny, tiny, tiny, 14)
    assert np.isnan(a).all() and np.isnan(p).all() and np.isnan(m).all()
