"""Risk engine, sizing and kill-switch semantics."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from atlas.core.enums import HaltReason, Side
from atlas.core.market import utc_ms
from atlas.core.trading import AccountState
from atlas.risk.config import RiskConfig
from atlas.risk.engine import OpenExposure, RiskEngine
from atlas.risk.sizing import kelly_fraction, risk_of_ruin, size_position
from atlas.risk.state import RiskState

T0 = utc_ms(datetime(2026, 5, 12, 10, 0, tzinfo=UTC))
HOUR = 3_600_000


def acct(equity: float, balance: float | None = None, **kw) -> AccountState:
    return AccountState(
        balance=balance if balance is not None else equity, equity=equity,
        free_margin=kw.pop("free_margin", equity), leverage=kw.pop("leverage", 100),
        currency="USD", trade_allowed=kw.pop("trade_allowed", True), **kw,
    )


# --- sizing ---------------------------------------------------------------------------


def test_sizing_uses_equity_not_balance(gold):
    """With a floating loss, balance overstates what is available."""
    floating_loss = size_position(gold, equity=9000, risk_pct=1.0, stop_points=300)
    flat = size_position(gold, equity=10000, risk_pct=1.0, stop_points=300)
    assert floating_loss.volume < flat.volume


@given(equity=st.floats(500, 500_000), risk=st.floats(0.1, 2.0), stop=st.floats(50, 3000))
@settings(max_examples=300, deadline=None)
def test_realised_risk_never_exceeds_the_budget(gold, equity, risk, stop):
    r = size_position(gold, equity, risk, stop)
    assert r.risk_money <= r.intended_risk_money * (1 + 1e-9)
    if r.volume > 0:
        assert r.risk_pct <= risk * (1 + 1e-9)


def test_conviction_scales_down_never_up(gold):
    base = size_position(gold, 100_000, 1.0, 300)
    high = size_position(gold, 100_000, 1.0, 300, conviction=1.0, min_fraction=0.5)
    low = size_position(gold, 100_000, 1.0, 300, conviction=0.0, min_fraction=0.5)
    assert high.volume == base.volume, "full conviction is the configured risk, not more"
    assert low.volume == pytest.approx(base.volume * 0.5, rel=0.02)
    assert low.volume < high.volume


def test_below_minimum_lot_is_reported_not_rounded_up(gold):
    """The dangerous alternative -- trading volume_min anyway -- would silently multiply risk."""
    r = size_position(gold, equity=500, risk_pct=0.5, stop_points=2000)
    assert r.volume == 0.0 and not r.tradeable
    assert r.drift == pytest.approx(1.0)
    would_risk = gold.money_for_points(2000, gold.volume_min)
    assert would_risk > r.intended_risk_money * 3


def test_margin_prefers_broker_value_over_leverage_estimate(gold, eurusd):
    assert size_position(gold, 100_000, 1.0, 300).margin_required == pytest.approx(
        gold.margin_initial * size_position(gold, 100_000, 1.0, 300).volume
    )
    r = size_position(eurusd, 100_000, 1.0, 300, leverage=100)
    assert r.margin_required > 0


def test_kelly_and_ror_are_sane():
    assert kelly_fraction(0.42, 2.0) == pytest.approx(0.13, abs=0.001)
    assert kelly_fraction(0.3, 1.0) == 0.0, "a negative edge gives no Kelly fraction"
    assert risk_of_ruin(0.3, 1.0, 1.0) == 1.0, "a negative edge is certain ruin eventually"
    assert risk_of_ruin(0.5, 2.0, 0.5) < risk_of_ruin(0.5, 2.0, 3.0)


# --- configuration ---------------------------------------------------------------------


def test_incoherent_configs_are_rejected():
    with pytest.raises(ValueError, match="below the per-trade risk"):
        RiskConfig(risk_per_trade_pct=3.0, max_total_risk_pct=2.0)
    with pytest.raises(ValueError, match="below total_drawdown"):
        RiskConfig(daily_loss_limit_pct=10.0, total_drawdown_limit_pct=8.0)


def test_correlation_groups_tolerate_broker_suffixes():
    cfg = RiskConfig()
    assert cfg.group_of("XAUUSD.m") == cfg.group_of("XAUUSD") == "USD_RISK"
    assert cfg.group_of("EURUSD_i") == "USD_MAJOR"
    assert cfg.group_of("USDCHF").startswith("SINGLE:")


def test_consecutive_losses_to_breach_is_reported():
    assert RiskConfig(risk_per_trade_pct=1.0,
                      daily_loss_limit_pct=5.0).consecutive_losses_to_daily_breach() == 5.0


# --- pre-trade gates ---------------------------------------------------------------------


def test_approval_path(gold):
    eng = RiskEngine(RiskConfig())
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000), open_exposure=[], conviction=0.8, now_ms=T0)
    assert a.approved and a.volume > 0
    assert all(g.passed for g in a.gates)
    assert any("losses would reach today's halt threshold" in n for n in a.notes)


def test_symbol_and_total_position_limits(gold):
    eng = RiskEngine(RiskConfig(max_positions=2, max_positions_per_symbol=1))
    ex = [OpenExposure(symbol="XAUUSD", side=Side.BUY, risk_money=10.0)]
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000), open_exposure=ex, now_ms=T0)
    assert not a.approved and a.reason_code == "SYMBOL_POSITION_LIMIT"

    ex2 = [OpenExposure(symbol="A", side=Side.BUY, risk_money=1.0),
           OpenExposure(symbol="B", side=Side.BUY, risk_money=1.0)]
    b = eng.evaluate(symbol="C", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000), open_exposure=ex2, now_ms=T0)
    assert not b.approved and b.reason_code == "POSITION_LIMIT"


def test_aggregate_risk_cap_counts_money_not_positions(gold):
    eng = RiskEngine(RiskConfig(max_total_risk_pct=2.0, risk_per_trade_pct=1.0,
                                max_group_risk_pct=2.0))
    ex = [OpenExposure(symbol="AAA", side=Side.BUY, risk_money=180.0)]
    a = eng.evaluate(symbol="BBB", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000), open_exposure=ex, now_ms=T0)
    assert not a.approved and a.reason_code == "TOTAL_RISK_LIMIT"
    g = next(g for g in a.gates if g.name == "total_open_risk")
    assert g.value is not None and g.value > 2.0


def test_correlated_instruments_count_as_one_exposure(gold):
    """Long gold and long silver is one bet, not two."""
    eng = RiskEngine(RiskConfig(max_total_risk_pct=5.0, max_group_risk_pct=1.0,
                                risk_per_trade_pct=0.8))
    ex = [OpenExposure(symbol="XAGUSD", side=Side.BUY, risk_money=80.0)]
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000), open_exposure=ex, now_ms=T0)
    assert not a.approved and a.reason_code == "GROUP_RISK_LIMIT"

    uncorrelated = [OpenExposure(symbol="USDCHF", side=Side.BUY, risk_money=80.0)]
    b = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000), open_exposure=uncorrelated, now_ms=T0)
    assert b.approved


def test_margin_headroom_is_enforced(gold):
    eng = RiskEngine(RiskConfig(min_free_margin_pct=50.0))
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000, free_margin=1_000), open_exposure=[], now_ms=T0)
    assert not a.approved and a.reason_code == "INSUFFICIENT_MARGIN"


def test_size_drift_rejects_a_too_coarse_lot_step(gold):
    """A tiny account on gold cannot express 0.5% risk in 0.01-lot steps."""
    eng = RiskEngine(RiskConfig(max_size_drift=0.2, risk_per_trade_pct=0.5))
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=1500,
                     account=acct(700), open_exposure=[], now_ms=T0)
    assert not a.approved and a.reason_code in {"SIZE_DRIFT", "BELOW_MIN_LOT"}


def test_broker_disabled_trading_is_respected(gold):
    eng = RiskEngine(RiskConfig())
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(10_000, trade_allowed=False), open_exposure=[], now_ms=T0)
    assert not a.approved and a.reason_code == "BROKER_TRADING_DISABLED"


# --- kill switch --------------------------------------------------------------------------


def test_daily_limit_trips_on_floating_loss_with_no_closed_trade(gold):
    """Prop daily limits are breached on equity, not on realised P/L."""
    eng = RiskEngine(RiskConfig(daily_loss_limit_pct=3.0))
    eng.observe(acct(10_000), T0)
    assert eng.observe(acct(9_800, balance=10_000), T0 + 60_000) is None
    r = eng.observe(acct(9_650, balance=10_000), T0 + 120_000)
    assert r is HaltReason.DAILY_LOSS_LIMIT
    assert eng.state.trades_today == 0, "no trade closed, yet the limit is enforced"
    a = eng.evaluate(symbol="XAUUSD", spec=gold, side=Side.BUY, stop_points=400,
                     account=acct(9_650), open_exposure=[], now_ms=T0)
    assert not a.approved and a.reason_code == "HALTED"


def test_trailing_drawdown_is_stricter_than_static():
    trailing = RiskEngine(RiskConfig(trailing_drawdown=True, total_drawdown_limit_pct=8.0,
                                     daily_loss_limit_pct=7.0))
    static = RiskEngine(RiskConfig(trailing_drawdown=False, total_drawdown_limit_pct=8.0,
                                   daily_loss_limit_pct=7.0))
    for eng in (trailing, static):
        eng.observe(acct(10_000), T0)
        eng.observe(acct(12_000), T0 + HOUR)  # a good run raises the high-water mark
    # 11,000 is 8.3% below the 12,000 HWM but 10% ABOVE the initial balance.
    assert trailing.observe(acct(11_000), T0 + 2 * HOUR) is HaltReason.TOTAL_DRAWDOWN_LIMIT
    assert static.observe(acct(11_000), T0 + 2 * HOUR) is None


def test_consecutive_losses_trip_and_a_win_resets_the_count():
    eng = RiskEngine(RiskConfig(max_consecutive_losses=3))
    eng.observe(acct(10_000), T0)
    assert eng.on_trade_closed(-50, T0) is None
    assert eng.on_trade_closed(-50, T0) is None
    assert eng.state.consecutive_losses == 2
    eng.on_trade_closed(+80, T0)
    assert eng.state.consecutive_losses == 0
    for _ in range(3):
        r = eng.on_trade_closed(-50, T0)
    assert r is HaltReason.CONSECUTIVE_LOSSES


def test_daily_halt_clears_at_the_reset_boundary_but_drawdown_halt_does_not():
    cfg = RiskConfig(daily_loss_limit_pct=3.0, total_drawdown_limit_pct=8.0,
                     daily_reset_tz="UTC", daily_reset_hour=0)
    eng = RiskEngine(cfg)
    eng.observe(acct(10_000), T0)
    eng.observe(acct(9_600, balance=10_000), T0 + 60_000)
    assert eng.state.halt_reason is HaltReason.DAILY_LOSS_LIMIT
    eng.observe(acct(9_600, balance=10_000), T0 + 24 * HOUR)  # next trading day
    assert not eng.state.halted, "a DAILY limit must clear at the daily reset"

    eng2 = RiskEngine(cfg)
    eng2.observe(acct(10_000), T0)
    eng2.observe(acct(9_100, balance=10_000), T0 + 60_000)
    assert eng2.state.halt_reason is HaltReason.TOTAL_DRAWDOWN_LIMIT
    eng2.observe(acct(9_100, balance=10_000), T0 + 24 * HOUR)
    assert eng2.state.halted, "a total-drawdown halt must survive the daily reset"


def test_a_breach_of_both_limits_halts_with_the_non_clearing_reason():
    """Safety regression. A single loss large enough to breach both limits must halt with
    TOTAL_DRAWDOWN (which needs an operator), not DAILY_LOSS (which clears overnight).
    Checking the daily limit first would let the system re-arm with the total limit still
    breached."""
    cfg = RiskConfig(daily_loss_limit_pct=3.0, total_drawdown_limit_pct=8.0)
    eng = RiskEngine(cfg)
    eng.observe(acct(10_000), T0)
    r = eng.observe(acct(9_100, balance=10_000), T0 + 1000)  # -9%: breaches both
    assert r is HaltReason.TOTAL_DRAWDOWN_LIMIT
    eng.observe(acct(9_100, balance=10_000), T0 + 48 * HOUR)
    assert eng.state.halted, "must not re-arm across the daily reset"


def test_a_serious_halt_is_not_downgraded_by_a_lesser_one():
    st = RiskState()
    st.trip(HaltReason.TOTAL_DRAWDOWN_LIMIT, "big", T0)
    st.trip(HaltReason.DAILY_LOSS_LIMIT, "small", T0)
    assert st.halt_reason is HaltReason.TOTAL_DRAWDOWN_LIMIT


def test_health_halts_clear_when_the_condition_clears():
    eng = RiskEngine(RiskConfig())
    assert eng.report_health(venue_ok=False, data_fresh=True, now_ms=T0) is HaltReason.VENUE_UNAVAILABLE
    assert eng.state.halted
    assert eng.report_health(venue_ok=True, data_fresh=True, now_ms=T0 + 1000) is None
    assert not eng.state.halted


def test_health_recovery_does_not_clear_a_risk_halt():
    eng = RiskEngine(RiskConfig())
    eng.observe(acct(10_000), T0)
    eng.observe(acct(9_100, balance=10_000), T0 + 1000)
    assert eng.state.halted
    eng.report_health(venue_ok=True, data_fresh=True, now_ms=T0 + 2000)
    assert eng.state.halted, "a venue health check must not clear a drawdown halt"


def test_resume_requires_an_explicit_call():
    eng = RiskEngine(RiskConfig())
    eng.halt(HaltReason.MANUAL, "operator stopped trading", T0)
    assert eng.state.halted
    for _ in range(5):
        eng.observe(acct(10_000), T0 + HOUR)
    assert eng.state.halted, "time alone must not re-arm a manual halt"
    assert eng.resume(T0 + 2 * HOUR)
    assert not eng.state.halted


# --- persistence -----------------------------------------------------------------------


def test_halt_survives_a_restart(tmp_path):
    path = tmp_path / "risk_state.json"
    eng = RiskEngine(RiskConfig())
    eng.observe(acct(10_000), T0)
    eng.observe(acct(9_100, balance=10_000), T0 + 1000)
    eng.state.save(path)

    restarted = RiskEngine(RiskConfig(), RiskState.load(path))
    assert restarted.state.halted
    assert restarted.state.halt_reason is HaltReason.TOTAL_DRAWDOWN_LIMIT
    assert restarted.state.equity_hwm == 10_000


def test_corrupt_state_file_fails_safe(tmp_path):
    """Unreadable state must halt, not silently start fresh with limits disarmed."""
    path = tmp_path / "risk_state.json"
    path.write_text("{ this is not json")
    st = RiskState.load(path)
    assert st.halted and st.halt_reason is HaltReason.MANUAL
    assert "unreadable" in st.halt_detail


def test_state_save_is_atomic(tmp_path):
    path = tmp_path / "nested" / "risk_state.json"
    st = RiskState(initial_balance=1000)
    st.save(path)
    assert path.exists() and not path.with_suffix(".json.tmp").exists()
    assert RiskState.load(path).initial_balance == 1000


def test_day_key_respects_a_non_utc_reset_timezone():
    """A firm resetting at 17:00 New York is not resetting at midnight UTC."""
    ny_evening = utc_ms(datetime(2026, 5, 12, 20, 30, tzinfo=UTC))  # 16:30 EDT
    ny_later = utc_ms(datetime(2026, 5, 12, 21, 30, tzinfo=UTC))  # 17:30 EDT
    a = RiskState.day_key_for(ny_evening, "America/New_York", 17)
    b = RiskState.day_key_for(ny_later, "America/New_York", 17)
    assert a != b, "the reset boundary must fall between these two instants"
    assert RiskState.day_key_for(ny_evening, "UTC", 0) == RiskState.day_key_for(ny_later, "UTC", 0)
