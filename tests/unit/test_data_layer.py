"""Bar series, aggregation, calendar/DST and the temporal contract."""

from __future__ import annotations

from datetime import UTC, datetime, time
from itertools import pairwise

import pytest

from atlas.core.enums import Timeframe
from atlas.core.errors import DataError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, ms_to_dt, utc_ms
from atlas.data.aggregator import MultiTimeframeAggregator, aggregate, compare_series
from atlas.data.calendar import (
    NewsCalendar,
    NewsEvent,
    ServerClockMapping,
    SessionDef,
    TradingCalendar,
    currencies_of,
)
from atlas.data.csv_io import gap_report, read_bars, write_bars
from atlas.data.series import BarSeries
from atlas.data.source import ReplayDataSource, UpdateKind
from atlas.data.synthetic import SyntheticConfig, generate, generate_quotes_from_bar

GOLD = SymbolSpec(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0,
                  contract_size=100, volume_min=0.01, volume_max=50, volume_step=0.01,
                  stops_level_points=50)


def mk(ts: int, o=100.0, h=101.0, low=99.0, c=100.5, tf=Timeframe.M5, sym="X", **kw) -> Bar:
    return Bar(symbol=sym, tf=tf, ts=ts, open=o, high=h, low=low, close=c, **kw)


# --- BarSeries ---------------------------------------------------------------------


def test_series_rejects_incomplete_out_of_order_and_foreign_bars():
    s = BarSeries("X", Timeframe.M5)
    s.append(mk(0))
    with pytest.raises(DataError, match="incomplete"):
        s.append(mk(300_000, complete=False))
    with pytest.raises(DataError, match="out-of-order"):
        s.append(mk(-300_000))
    with pytest.raises(DataError, match="does not belong"):
        s.append(mk(300_000, tf=Timeframe.M15))
    with pytest.raises(DataError, match="does not belong"):
        s.append(mk(300_000, sym="Y"))


def test_duplicate_timestamp_overwrites_instead_of_appending():
    """MT5 re-delivers the same closed bar on reconnect. That must be idempotent."""
    s = BarSeries("X", Timeframe.M5)
    s.append(mk(0, c=100.5))
    s.append(mk(0, c=777.0))
    assert len(s) == 1 and s.close[-1] == 777.0


def test_ring_buffer_keeps_the_newest_bars():
    s = BarSeries("X", Timeframe.M5, max_bars=3)
    for i in range(10):
        s.append(mk(i * 300_000, c=float(i)))
    assert len(s) == 3
    assert s.close.tolist() == [7.0, 8.0, 9.0]
    assert s.ts.tolist() == [7 * 300_000, 8 * 300_000, 9 * 300_000]


def test_head_and_tail_are_copies_not_views():
    s = BarSeries("X", Timeframe.M5)
    for i in range(10):
        s.append(mk(i * 300_000, c=float(i)))
    h = s.head(5)
    h._arrays["close"][0] = -1.0
    assert s.close[0] == 0.0, "mutating a slice must not corrupt the parent series"
    assert s.tail(3).close.tolist() == [7.0, 8.0, 9.0]


def test_index_of():
    s = BarSeries("X", Timeframe.M5)
    for i in range(5):
        s.append(mk(i * 300_000))
    assert s.index_of(2 * 300_000) == 2
    assert s.index_of(999) == -1


# --- Aggregation -------------------------------------------------------------------


def test_partial_bucket_is_never_emitted():
    """A 5-hour M5 series yields 4 closed H1 bars, not 5: the last hour is still forming."""
    bars = [mk(i * 300_000, o=100 + i, h=101 + i, low=99 + i, c=100.5 + i) for i in range(60)]
    h1 = list(aggregate(bars, Timeframe.M5, Timeframe.H1))
    assert len(h1) == 4
    assert all(b.complete for b in h1)


def test_aggregated_ohlc_matches_the_constituent_bars():
    bars = [mk(i * 300_000, o=100 + i, h=105 + i, low=95 + i, c=100.5 + i, volume=2.0)
            for i in range(24)]
    h1 = list(aggregate(bars, Timeframe.M5, Timeframe.H1))
    first = h1[0]
    src = bars[:12]
    assert first.open == src[0].open
    assert first.close == src[-1].close
    assert first.high == max(b.high for b in src)
    assert first.low == min(b.low for b in src)
    assert first.volume == pytest.approx(sum(b.volume for b in src))


def test_aggregator_rejects_downward_aggregation():
    """Every standard timeframe divides evenly into every larger one, so the reachable
    failure is a target *shorter* than the base -- which would silently fabricate detail."""
    from atlas.data.aggregator import TimeframeAggregator

    with pytest.raises(DataError, match="multiple"):
        TimeframeAggregator("X", Timeframe.H1, Timeframe.M5)
    TimeframeAggregator("X", Timeframe.M30, Timeframe.H4)  # 14400 % 1800 == 0, allowed


def test_aggregator_rejects_base_bars_of_the_wrong_timeframe():
    from atlas.data.aggregator import TimeframeAggregator

    agg = TimeframeAggregator("X", Timeframe.M5, Timeframe.H1)
    with pytest.raises(DataError, match="expected"):
        agg.push(mk(0, tf=Timeframe.M15))


def test_forming_bar_is_marked_incomplete():
    agg = MultiTimeframeAggregator("X", Timeframe.M5, [Timeframe.H1])
    for i in range(5):
        agg.push(mk(i * 300_000, c=float(i)))
    forming = agg.forming()[Timeframe.H1]
    assert forming.complete is False
    assert forming.close == 4.0


def test_compare_series_detects_divergence():
    ours = [mk(0, c=1.0, tf=Timeframe.H1), mk(3_600_000, c=2.0, tf=Timeframe.H1)]
    theirs = [mk(0, c=1.0, tf=Timeframe.H1), mk(3_600_000, c=2.5, tf=Timeframe.H1),
              mk(7_200_000, c=3.0, tf=Timeframe.H1)]
    diffs = compare_series(ours, theirs)
    assert any("close" in d for d in diffs)
    assert any("broker has a bar we do not" in d for d in diffs)


# --- Calendar and DST --------------------------------------------------------------


def _at(iso: str) -> int:
    return utc_ms(datetime.fromisoformat(iso).replace(tzinfo=UTC))


def test_london_open_shifts_with_dst_in_utc_terms():
    """The whole point of tz-aware sessions: 07:00 UTC is the London open in summer only."""
    cal = TradingCalendar()
    summer, winter = _at("2026-07-15T07:30"), _at("2026-01-15T07:30")
    assert "LONDON" in cal.active_sessions(summer), "07:30 UTC is 08:30 BST -- open"
    assert "LONDON" not in cal.active_sessions(winter), "07:30 UTC is 07:30 GMT -- not yet open"
    assert "LONDON" in cal.active_sessions(_at("2026-01-15T08:30"))


def test_new_york_and_london_dst_dates_differ():
    """In late March the US has changed clocks and Europe has not; the overlap shifts."""
    cal = TradingCalendar()
    t = _at("2026-03-12T12:30")  # US on EDT (since 8 Mar), UK still on GMT
    local_ny = ms_to_dt(t).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert local_ny.hour == 8
    assert "NEWYORK" in cal.active_sessions(t)


def test_session_wrapping_midnight():
    s = SessionDef(name="OVERNIGHT", tz="UTC", start=time(22, 0), end=time(3, 0))
    assert s.contains(ms_to_dt(_at("2026-08-19T23:00")))  # Wednesday night
    assert s.contains(ms_to_dt(_at("2026-08-20T02:00")))  # belongs to Wednesday's session
    assert not s.contains(ms_to_dt(_at("2026-08-20T05:00")))


def test_server_offset_is_measured_and_quantised():
    m = ServerClockMapping.measure(
        server_epoch_ms=_at("2026-07-01T15:00") + 4123,  # +4.1s of latency noise
        utc_epoch_ms=_at("2026-07-01T12:00"),
    )
    assert m.offset_hours == 3.0
    assert m.to_utc(m.to_server(12345)) == 12345


def test_server_offset_staleness():
    m = ServerClockMapping.measure(_at("2026-07-01T15:00"), _at("2026-07-01T12:00"))
    assert not m.is_stale(_at("2026-07-01T20:00"))
    assert m.is_stale(_at("2026-07-03T12:00")), "a stale offset must be re-measured (DST)"
    assert ServerClockMapping().is_stale(0), "an unmeasured mapping is always stale"


def test_rollover_follows_server_midnight_not_utc_midnight():
    """The window is +/-60 min around SERVER midnight, so two brokers an hour apart disagree
    about whether the same UTC instant is rollover. That is the behaviour we want."""
    utc3 = TradingCalendar(server=ServerClockMapping(offset_seconds=3 * 3600, measured_at_ms=1))
    utc2 = TradingCalendar(server=ServerClockMapping(offset_seconds=2 * 3600, measured_at_ms=1))
    t = _at("2026-07-15T20:30")  # 23:30 on a UTC+3 server, 22:30 on a UTC+2 server
    assert utc3.is_rollover(t)
    assert not utc2.is_rollover(t)
    assert utc2.is_rollover(_at("2026-07-15T21:30"))  # now 23:30 server for UTC+2
    assert not utc3.is_rollover(_at("2026-07-15T12:00"))  # 15:00 server, mid-session


def test_weekend_window():
    cal = TradingCalendar()
    assert cal.is_weekend(_at("2026-08-22T12:00"))  # Saturday
    assert cal.is_weekend(_at("2026-08-21T22:00"))  # Friday after close
    assert cal.is_weekend(_at("2026-08-23T15:00"))  # Sunday before open
    assert not cal.is_weekend(_at("2026-08-23T22:00"))  # Sunday after open
    assert not cal.is_weekend(_at("2026-08-20T12:00"))  # Thursday


def test_news_blackout_window_and_impact_filter():
    ev = NewsEvent(ts=_at("2026-09-04T12:30"), currency="USD", impact="HIGH", title="NFP")
    low = NewsEvent(ts=_at("2026-09-04T15:00"), currency="USD", impact="LOW", title="minor")
    cal = NewsCalendar([ev, low])
    assert cal.blackout(_at("2026-09-04T12:20"), ["XAU", "USD"]) is not None  # 10 min before
    assert cal.blackout(_at("2026-09-04T12:40"), ["USD"]) is not None  # 10 min after
    assert cal.blackout(_at("2026-09-04T13:30"), ["USD"]) is None  # an hour later
    assert cal.blackout(_at("2026-09-04T12:30"), ["EUR"]) is None  # wrong currency
    assert cal.blackout(_at("2026-09-04T15:00"), ["USD"]) is None  # LOW filtered out
    assert cal.blackout(_at("2026-09-04T15:00"), ["USD"], min_impact="LOW") is not None


def test_missing_news_file_is_unavailable_not_empty(tmp_path):
    """'No data' and 'no events' must be distinguishable, or the system trades through NFP."""
    cal = NewsCalendar.from_file(tmp_path / "nope.json")
    assert cal.available is False
    (tmp_path / "bad.json").write_text("{not json")
    assert NewsCalendar.from_file(tmp_path / "bad.json").available is False
    (tmp_path / "ok.json").write_text('[{"ts": 1, "currency": "USD", "impact": "HIGH"}]')
    good = NewsCalendar.from_file(tmp_path / "ok.json")
    assert good.available is True and len(good.events) == 1


@pytest.mark.parametrize("sym,expect", [
    ("XAUUSD", ("XAU", "USD")), ("XAUUSD.m", ("XAU", "USD")), ("GOLD", ("XAU", "USD")),
    ("EURUSD_i", ("EUR", "USD")), ("GBPJPY", ("GBP", "JPY")),
])
def test_currency_extraction_handles_broker_suffixes(sym, expect):
    assert currencies_of(sym) == expect


# --- Synthetic data and the temporal contract ---------------------------------------


def test_synthetic_generator_is_deterministic_and_well_formed():
    a = generate(SyntheticConfig(bars=500, seed=3))
    b = generate(SyntheticConfig(bars=500, seed=3))
    assert [x.close for x in a] == [x.close for x in b]
    assert generate(SyntheticConfig(bars=500, seed=4))[-1].close != a[-1].close
    assert all(x.low <= x.open <= x.high and x.low <= x.close <= x.high for x in a)
    assert all(x.ts < y.ts for x, y in pairwise(a))


def test_synthetic_excludes_the_closed_weekend():
    bars = generate(SyntheticConfig(bars=3000, seed=5))
    for b in bars:
        d = ms_to_dt(b.ts)
        assert d.weekday() != 5, "Saturday bars must not exist"
        assert not (d.weekday() == 6 and d.hour < 21), "Sunday pre-open bars must not exist"


def test_intrabar_quote_path_visits_the_adverse_extreme_first():
    """The pessimistic ordering the simulator relies on to resolve stop-before-target."""
    up = mk(0, o=100.0, h=105.0, low=99.0, c=104.0, spread_points=20)
    mids = [ (q[1] + q[2]) / 2 for q in generate_quotes_from_bar(up, 0.01) ]
    assert mids[1] == pytest.approx(99.0), "bullish bar must visit its low before its high"
    down = mk(0, o=104.0, h=105.0, low=99.0, c=100.0, spread_points=20)
    mids_d = [ (q[1] + q[2]) / 2 for q in generate_quotes_from_bar(down, 0.01) ]
    assert mids_d[1] == pytest.approx(105.0), "bearish bar must visit its high before its low"


async def test_replay_source_stamps_bar_updates_at_close_time():
    """ADR-015: a bar is knowable at T+P, so its update carries ts = T+P, not T."""
    bars = generate(SyntheticConfig(bars=300, seed=11, tf=Timeframe.M5))
    src = ReplayDataSource({"XAUUSD": bars}, {"XAUUSD": GOLD},
                           base_tf=Timeframe.M5, timeframes=[Timeframe.M15], warmup_bars=100)
    await src.warmup()
    seen = []
    async for u in src.stream():
        seen.append(u)
        if len(seen) > 60:
            break
    bar_updates = [u for u in seen if u.kind is UpdateKind.BAR and u.tf is Timeframe.M5]
    assert bar_updates
    for u in bar_updates:
        assert u.ts == u.bar.ts + Timeframe.M5.seconds * 1000
    assert all(a.ts <= b.ts for a, b in pairwise(seen)), "stream must be ordered"


async def test_replay_warmup_returns_only_closed_bars():
    bars = generate(SyntheticConfig(bars=600, seed=12, tf=Timeframe.M5))
    src = ReplayDataSource({"XAUUSD": bars}, {"XAUUSD": GOLD},
                           base_tf=Timeframe.M5, timeframes=[Timeframe.M15, Timeframe.H1],
                           warmup_bars=400)
    warm = await src.warmup()
    assert warm[("XAUUSD", Timeframe.M5)]
    assert len(warm[("XAUUSD", Timeframe.M15)]) < len(warm[("XAUUSD", Timeframe.M5)])
    for series in warm.values():
        assert all(series.bar(i).complete for i in range(len(series)))


def test_replay_rejects_insufficient_history():
    bars = generate(SyntheticConfig(bars=50, seed=1))
    with pytest.raises(DataError, match="warm-up"):
        ReplayDataSource({"XAUUSD": bars}, {"XAUUSD": GOLD},
                         base_tf=Timeframe.M5, timeframes=[], warmup_bars=100)


# --- CSV ---------------------------------------------------------------------------


def test_csv_roundtrip_and_server_time_conversion(tmp_path):
    bars = generate(SyntheticConfig(bars=200, seed=2))
    p = tmp_path / "x.csv"
    assert write_bars(p, bars) == 200
    same = read_bars(p, "XAUUSD", Timeframe.M5, server_offset_hours=0)
    assert [b.ts for b in same] == [b.ts for b in bars]
    shifted = read_bars(p, "XAUUSD", Timeframe.M5, server_offset_hours=3)
    assert shifted[0].ts == bars[0].ts - 3 * 3_600_000


def test_csv_rejects_impossible_ohlc(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("datetime,open,high,low,close\n2026-01-01 00:00:00,100,99,101,100\n")
    with pytest.raises(DataError, match="violate"):
        read_bars(p, "X", Timeframe.M5, server_offset_hours=0)


def test_csv_accepts_mt5_export_header(tmp_path):
    p = tmp_path / "mt5.csv"
    p.write_text(
        "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
        "2026.01.02\t00:05:00\t2600.10\t2601.00\t2599.80\t2600.50\t120\t0\t22\n"
        "2026.01.02\t00:10:00\t2600.50\t2602.00\t2600.00\t2601.75\t140\t0\t24\n"
    )
    bars = read_bars(p, "XAUUSD", Timeframe.M5, server_offset_hours=2)
    assert len(bars) == 2
    assert bars[0].spread_points == 22
    assert ms_to_dt(bars[0].ts) == datetime(2026, 1, 1, 22, 5, tzinfo=UTC)


def test_gap_report_ignores_weekends_but_flags_holes():
    good = generate(SyntheticConfig(bars=3000, seed=6))
    assert gap_report(good, Timeframe.M5) == []
    holed = good[:100] + good[400:500]
    assert gap_report(holed, Timeframe.M5)
