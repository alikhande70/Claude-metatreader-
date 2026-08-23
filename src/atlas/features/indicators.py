"""Vectorised technical indicators.

Conventions that hold for every function here:

* Input arrays are **chronological** (index 0 = oldest). This is the opposite of MQL5's
  series indexing; the conversion happens once, at the MT5 boundary, and never inside the
  feature layer. Chronological indexing makes rolling-window numpy code far less error-prone.
* Output arrays have the **same length as the input**, with ``NaN`` in the warm-up region.
  Returning a shorter array would silently misalign every downstream index.
* Every value at index ``i`` is computed from ``data[0:i+1]`` only. That is the property the
  look-ahead test verifies by recomputation on truncated inputs.
* Smoothing labelled "Wilder" uses ``alpha = 1/n`` (RMA), matching MetaTrader's ATR, ADX and
  RSI. Using a standard EMA (``alpha = 2/(n+1)``) here would produce values that differ from
  the chart the user is looking at, which destroys trust for no benefit.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def _empty_like(x: np.ndarray) -> np.ndarray:
    return np.full(x.shape, np.nan, dtype=np.float64)


def sma(values: np.ndarray, period: int) -> np.ndarray:
    v = np.asarray(values, dtype=np.float64)
    out = _empty_like(v)
    if period <= 0 or len(v) < period:
        return out
    cs = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1 :] = (cs[period:] - cs[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential MA seeded with the SMA of the first ``period`` values (MetaTrader's seed)."""
    v = np.asarray(values, dtype=np.float64)
    out = _empty_like(v)
    if period <= 0 or len(v) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    acc = float(np.mean(v[:period]))
    out[period - 1] = acc
    for i in range(period, len(v)):
        acc = alpha * v[i] + (1 - alpha) * acc
        out[i] = acc
    return out


def rma(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (alpha = 1/period), seeded with the first SMA."""
    v = np.asarray(values, dtype=np.float64)
    out = _empty_like(v)
    if period <= 0 or len(v) < period:
        return out
    acc = float(np.mean(v[:period]))
    out[period - 1] = acc
    for i in range(period, len(v)):
        acc = (acc * (period - 1) + v[i]) / period
        out[i] = acc
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """TR with the first bar's TR set to its own range (no prior close exists)."""
    h, low_, c = (np.asarray(x, dtype=np.float64) for x in (high, low, close))
    tr = np.empty(len(h), dtype=np.float64)
    if len(h) == 0:
        return tr
    tr[0] = h[0] - low_[0]
    if len(h) > 1:
        prev = c[:-1]
        tr[1:] = np.maximum.reduce(
            [h[1:] - low_[1:], np.abs(h[1:] - prev), np.abs(low_[1:] - prev)]
        )
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), period)


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI.

    Edge cases are handled explicitly rather than left to produce inf/NaN: an unbroken run of
    up bars gives 100, an unbroken run of down bars gives 0, and a perfectly flat stretch
    gives 50. Leaving these as inf propagates NaN into every downstream gate and turns a
    quiet market into a silent outage.
    """
    c = np.asarray(close, dtype=np.float64)
    out = _empty_like(c)
    if len(c) <= period:
        return out
    delta = np.diff(c)  # length n-1, delta[k] is the change into bar k+1
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_g = rma(gains, period)
    avg_l = rma(losses, period)
    vals = np.full(len(delta), np.nan, dtype=np.float64)
    ok = ~np.isnan(avg_g)
    both_zero = ok & (avg_g == 0) & (avg_l == 0)
    no_loss = ok & (avg_l == 0) & (avg_g > 0)
    normal = ok & (avg_l > 0)
    vals[both_zero] = 50.0
    vals[no_loss] = 100.0
    rs = avg_g[normal] / avg_l[normal]
    vals[normal] = 100.0 - 100.0 / (1.0 + rs)
    out[1:] = vals
    return out


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wilder's ADX. Returns ``(adx, plus_di, minus_di)``.

    ADX measures trend *strength* without direction; the DI pair carries direction. Both are
    used: ADX gates the regime classifier, the DI spread contributes evidence.
    """
    h, low_, c = (np.asarray(x, dtype=np.float64) for x in (high, low, close))
    n = len(h)
    nan3 = (_empty_like(h), _empty_like(h), _empty_like(h))
    if n < period * 2 + 1:
        return nan3

    up = np.diff(h, prepend=h[0])
    dn = -np.diff(low_, prepend=low_[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_dm[0] = minus_dm[0] = 0.0

    tr_s = rma(true_range(h, low_, c), period)
    pdm_s = rma(plus_dm, period)
    mdm_s = rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * np.divide(pdm_s, tr_s, out=_empty_like(h), where=tr_s > 0)
        mdi = 100.0 * np.divide(mdm_s, tr_s, out=_empty_like(h), where=tr_s > 0)
        denom = pdi + mdi
        dx = 100.0 * np.divide(np.abs(pdi - mdi), denom, out=_empty_like(h), where=denom > 0)

    adx_out = _empty_like(h)
    valid = np.flatnonzero(~np.isnan(dx))
    if len(valid) >= period:
        start = int(valid[0])
        smoothed = rma(dx[start:], period)
        adx_out[start:] = smoothed
    return adx_out, pdi, mdi


def efficiency_ratio(close: np.ndarray, period: int = 20) -> np.ndarray:
    """Kaufman efficiency ratio in [0, 1]: net move divided by total path length.

    Near 1 means a clean directional move; near 0 means chop covering no ground. It is the
    single cheapest trend-vs-range discriminator available and, unlike ADX, it is bounded and
    directly interpretable.
    """
    c = np.asarray(close, dtype=np.float64)
    out = _empty_like(c)
    if len(c) <= period:
        return out
    path = np.abs(np.diff(c, prepend=c[0]))
    cs = np.cumsum(np.insert(path, 0, 0.0))
    idx = np.arange(period, len(c))
    total = cs[idx + 1] - cs[idx + 1 - period]
    net = np.abs(c[idx] - c[idx - period])
    out[period:] = np.divide(net, total, out=np.zeros(len(idx)), where=total > 0)
    return out


def donchian(
    high: np.ndarray, low: np.ndarray, period: int = 20, *, exclude_current: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling channel. ``exclude_current`` shifts the window back one bar.

    Excluding the current bar is the correct default for a breakout rule: a channel that
    includes the breaking bar can never be broken, so the signal would never fire (or, worse,
    a naive implementation compares the bar's own high to a max that contains it).
    """
    h, low_ = np.asarray(high, dtype=np.float64), np.asarray(low, dtype=np.float64)
    n = len(h)
    hi, lo = _empty_like(h), _empty_like(h)
    off = 1 if exclude_current else 0
    for i in range(period - 1 + off, n):
        s = i - period + 1 - off
        e = i + 1 - off
        hi[i] = h[s:e].max()
        lo[i] = low_[s:e].min()
    return hi, lo


def rolling_percentile(values: np.ndarray, period: int) -> np.ndarray:
    """Percentile rank of each value within its own trailing window, in [0, 1].

    Used to make volatility comparable across regimes: "ATR is at its 90th percentile of the
    last 500 bars" is actionable, "ATR is 4.2" is not.

    NaNs in the window are excluded from both the count and the denominator. Comparisons
    against NaN are False in IEEE arithmetic, which is exactly the behaviour needed here, so
    the warm-up region does not need special-casing.
    """
    v = np.asarray(values, dtype=np.float64)
    out = _empty_like(v)
    if period < 2 or len(v) < period:
        return out
    win = sliding_window_view(v, period)  # shape (len(v) - period + 1, period)
    centre = v[period - 1 :]
    valid = (~np.isnan(win)).sum(axis=1)
    le = (win <= centre[:, None]).sum(axis=1)
    ok = (valid >= 2) & ~np.isnan(centre)
    res = np.full(len(centre), np.nan)
    np.divide((le - 1).astype(float), (valid - 1).astype(float), out=res, where=ok)
    out[period - 1 :] = np.where(ok, res, np.nan)
    return out


def realized_vol(
    close: np.ndarray, period: int = 20, bars_per_year: float = 105_120.0
) -> np.ndarray:
    """Annualised realised volatility from log returns."""
    c = np.asarray(close, dtype=np.float64)
    out = _empty_like(c)
    if len(c) <= period:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(c), prepend=np.log(c[0]))
    win = sliding_window_view(r, period)
    sd = win.std(axis=1, ddof=1) * np.sqrt(bars_per_year)
    # Window j covers r[j : j+period], i.e. bar index j + period - 1. The first emitted bar
    # is `period` (not period - 1) so that the window never includes the synthetic first
    # return created by `prepend`.
    out[period:] = sd[1:]
    return out


def body_ratio(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray
) -> np.ndarray:
    """Body size as a fraction of total range -- the objective form of "one-sided candle"."""
    o, h, low_, c = (np.asarray(x, dtype=np.float64) for x in (open_, high, low, close))
    rng = h - low_
    out = np.zeros(len(o), dtype=np.float64)
    np.divide(np.abs(c - o), rng, out=out, where=rng > 0)
    return out


def slope(values: np.ndarray, period: int) -> np.ndarray:
    """Least-squares slope over a trailing window, expressed per bar."""
    v = np.asarray(values, dtype=np.float64)
    out = _empty_like(v)
    if period < 2 or len(v) < period:
        return out
    x = np.arange(period, dtype=np.float64)
    x -= x.mean()
    denom = float((x**2).sum())
    win = sliding_window_view(v, period)
    centred = win - win.mean(axis=1, keepdims=True)
    vals = (centred * x).sum(axis=1) / denom
    vals[np.isnan(win).any(axis=1)] = np.nan
    out[period - 1 :] = vals
    return out
