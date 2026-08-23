"""Shared fixtures.

``build_features`` is the workhorse: it turns a synthetic bar stream into the exact
``MultiTimeframeFeatures`` object a strategy sees at a chosen bar, going through the real
aggregator so that timeframe alignment is exercised rather than faked.
"""

from __future__ import annotations

import pytest

from atlas.core.enums import Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, Quote
from atlas.data.aggregator import MultiTimeframeAggregator
from atlas.data.calendar import ServerClockMapping, TradingCalendar
from atlas.data.series import BarSeries
from atlas.data.synthetic import SyntheticConfig, generate
from atlas.features.frame import FeatureConfig, FeatureFrame, MultiTimeframeFeatures


@pytest.fixture(scope="session")
def gold() -> SymbolSpec:
    return SymbolSpec(
        name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
        contract_size=100, volume_min=0.01, volume_max=50.0, volume_step=0.01,
        stops_level_points=50, freeze_level_points=0, currency_base="XAU",
        currency_profit="USD", currency_margin="USD", margin_initial=2000.0,
        swap_long=-4.5, swap_short=1.2,
    )


@pytest.fixture(scope="session")
def eurusd() -> SymbolSpec:
    return SymbolSpec(
        name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
        contract_size=100000, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        stops_level_points=0, currency_base="EUR", currency_profit="USD",
        currency_margin="EUR", margin_initial=1000.0,
    )


@pytest.fixture(scope="session")
def m5_bars() -> list[Bar]:
    return generate(SyntheticConfig(bars=12_000, seed=42))


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar(
        server=ServerClockMapping(offset_seconds=3 * 3600, measured_at_ms=1)
    )


def build_series(
    bars: list[Bar], timeframes: list[Timeframe], base_tf: Timeframe = Timeframe.M5
) -> dict[Timeframe, BarSeries]:
    """Aggregate a base bar stream into per-timeframe closed-bar series."""
    symbol = bars[0].symbol
    agg = MultiTimeframeAggregator(symbol, base_tf, timeframes)
    out = {tf: BarSeries(symbol, tf, capacity=2048) for tf in dict.fromkeys([base_tf, *timeframes])}
    for b in bars:
        for tf, closed in agg.push(b).items():
            out[tf].append(closed)
    return out


def build_features(
    bars: list[Bar], spec: SymbolSpec, timeframes: list[Timeframe],
    cfg: FeatureConfig | None = None, base_tf: Timeframe = Timeframe.M5,
) -> MultiTimeframeFeatures:
    cfg = cfg or FeatureConfig()
    series = build_series(bars, timeframes, base_tf)
    return MultiTimeframeFeatures(
        symbol=bars[0].symbol,
        frames={tf: FeatureFrame.compute(s, spec, cfg) for tf, s in series.items() if len(s) > 5},
    )


@pytest.fixture(scope="session")
def gold_features(m5_bars, gold) -> MultiTimeframeFeatures:
    """Features over the whole synthetic history, computed once.

    Tests then take zero-copy views at whichever bar they care about, which is both far
    faster and a closer match to how the backtest engine actually works.
    """
    return build_features(m5_bars, gold, [Timeframe.H1, Timeframe.M15, Timeframe.M5])


def quote_at(bar: Bar, spec: SymbolSpec, spread_points: float | None = None) -> Quote:
    sp = bar.spread_points if spread_points is None else spread_points
    half = sp * spec.point / 2.0
    return Quote(symbol=bar.symbol, ts=bar.ts_close,
                 bid=spec.normalize_price(bar.close - half),
                 ask=spec.normalize_price(bar.close + half))
