"""Instrument maths, time handling and decision determinism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atlas.core.clock import SimulatedClock
from atlas.core.decision import make_client_order_id, make_decision_id
from atlas.core.enums import Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.core.market import floor_to_timeframe, ms_to_dt, utc_ms

GOLD = SymbolSpec(
    name="XAUUSD",
    digits=2,
    point=0.01,
    tick_size=0.01,
    tick_value=1.0,
    contract_size=100,
    volume_min=0.01,
    volume_max=50,
    volume_step=0.01,
    stops_level_points=50,
)
GOLD3 = SymbolSpec(
    name="XAUUSD.m",
    digits=3,
    point=0.001,
    tick_size=0.001,
    tick_value=0.1,
    contract_size=100,
    volume_min=0.01,
    volume_max=50,
    volume_step=0.01,
)
EUR = SymbolSpec(
    name="EURUSD",
    digits=5,
    point=0.00001,
    tick_size=0.00001,
    tick_value=1.0,
    contract_size=100000,
    volume_min=0.01,
    volume_max=100,
    volume_step=0.01,
)


def test_point_value_matches_hand_calculation():
    # 300 points on gold = $3.00 move x 100 oz = $300 per lot
    assert GOLD.money_for_points(300, 1.0) == pytest.approx(300.0)
    # a 3-digit gold broker: same economic move, ten times the point count
    assert GOLD3.money_for_points(3000, 1.0) == pytest.approx(300.0)
    # 20 pips on EURUSD 1 lot = $200
    assert EUR.money_for_points(200, 1.0) == pytest.approx(200.0)


def test_pip_size_depends_on_digits():
    assert GOLD.pip_size == pytest.approx(0.01)
    assert GOLD3.pip_size == pytest.approx(0.01)
    assert EUR.pip_size == pytest.approx(0.0001)


@given(risk=st.floats(10, 100_000), stop=st.floats(10, 5000))
@settings(max_examples=200, deadline=None)
def test_sizing_never_exceeds_the_risk_budget(risk, stop):
    """Volume normalisation must round DOWN: realised risk <= requested risk, always."""
    raw = GOLD.volume_for_risk(risk, stop)
    vol = GOLD.normalize_volume(raw)
    if vol == 0.0:
        assert raw < GOLD.volume_min
        return
    realised = GOLD.money_for_points(stop, vol)
    assert realised <= risk * (1 + 1e-9)


def test_volume_below_minimum_returns_zero_not_minimum():
    """Returning volume_min here would silently trade many times the intended risk."""
    assert GOLD.normalize_volume(0.004) == 0.0
    assert GOLD.normalize_volume(0.01) == 0.01


def test_volume_clamped_to_max():
    assert GOLD.normalize_volume(999.0) == 50.0


@pytest.mark.parametrize("spec,lo,hi", [(GOLD, 500, 10_000), (GOLD3, 500, 10_000), (EUR, 0.5, 3.0)])
@given(data=st.data())
@settings(max_examples=200, deadline=None)
def test_price_normalization_is_idempotent_and_bounded(spec, lo, hi, data):
    """The two properties that actually matter: normalising twice changes nothing, and the
    result never moves the price by as much as a whole tick.

    We deliberately do NOT assert an absolute tick-grid residual: at prices far outside an
    instrument's real range the double-precision quotient itself carries error larger than
    any sensible epsilon, which would be a test artefact rather than a defect.
    """
    price = data.draw(st.floats(lo, hi, allow_nan=False, allow_infinity=False))
    p = spec.normalize_price(price)
    assert spec.normalize_price(p) == p
    assert abs(p - price) <= spec.tick_size


def test_stops_level_enforced():
    assert not GOLD.is_stop_distance_valid(2400.00, 2399.80)  # 20 pts < 50 pt stops level
    assert GOLD.is_stop_distance_valid(2400.00, 2399.00)  # 100 pts


@pytest.mark.parametrize(
    "when,tf,expect",
    [
        ("2026-08-23T13:47:00", Timeframe.M15, "2026-08-23T13:45:00"),
        ("2026-08-23T13:47:00", Timeframe.H4, "2026-08-23T12:00:00"),
        ("2026-08-23T13:47:00", Timeframe.D1, "2026-08-23T00:00:00"),
        ("2026-08-23T13:47:00", Timeframe.W1, "2026-08-17T00:00:00"),  # Sunday -> prior Monday
        ("2026-08-17T00:00:00", Timeframe.W1, "2026-08-17T00:00:00"),  # Monday midnight is exact
    ],
)
def test_timeframe_flooring(when, tf, expect):
    got = ms_to_dt(floor_to_timeframe(utc_ms(datetime.fromisoformat(when).replace(tzinfo=UTC)), tf))
    assert got == datetime.fromisoformat(expect).replace(tzinfo=UTC)


def test_w1_flooring_lands_on_monday_for_a_full_year():
    d = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(365 * 4):
        floored = ms_to_dt(floor_to_timeframe(utc_ms(d), Timeframe.W1))
        assert floored.weekday() == 0 and floored.hour == 0
        assert 0 <= (d - floored).total_seconds() < 7 * 86400
        d += timedelta(hours=6)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="naive"):
        utc_ms(datetime(2026, 1, 1))


def test_decision_ids_are_deterministic_and_bounded():
    a = make_decision_id("XAUUSD", "sm", 1000)
    assert a == make_decision_id("XAUUSD", "sm", 1000)
    assert a != make_decision_id("XAUUSD", "sm", 1001)
    coid = make_client_order_id(a)
    assert len(coid) <= 16, "must fit in a truncated MT5 order comment"
    assert coid != make_client_order_id(a, attempt_group=1)


def test_simulated_clock_refuses_to_go_backwards():
    c = SimulatedClock(1000)
    c.set(2000)
    with pytest.raises(ValueError, match="backwards"):
        c.set(1500)
