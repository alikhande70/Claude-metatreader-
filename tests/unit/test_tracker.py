"""Position tracking: the bookkeeping the venue does not do for us."""

from __future__ import annotations

import pytest

from atlas.core.enums import Side
from atlas.core.trading import Position
from atlas.runtime.tracker import PositionTracker, TrackedPosition


def tracked(**kw) -> TrackedPosition:
    base = dict(ticket=1, symbol="XAUUSD", side=Side.BUY, strategy="SM1", decision_id="d1",
                client_order_id="A1", entry_price=2600.0, initial_stop=2590.0,
                initial_target=2620.0, opened_ts=0, volume=0.10)
    base.update(kw)
    return TrackedPosition(**base)


def venue_position(**kw) -> Position:
    base = dict(ticket=1, symbol="XAUUSD", side=Side.BUY, volume=0.10, open_price=2600.0,
                open_time=0, stop_loss=2590.0, take_profit=2620.0, current_price=2605.0,
                magic=1)
    base.update(kw)
    return Position(**base)


def test_open_risk_uses_the_live_stop_not_the_entry_stop(gold):
    """Regression for a bug where the docstring promised the live stop and the code used the
    entry stop, so a position moved to breakeven still consumed its full share of the
    aggregate risk cap and blocked trades the account could afford."""
    tp = tracked()
    tp.observe_venue(venue_position(current_price=2605.0))
    at_entry = tp.risk_money(gold, 2605.0)
    assert at_entry == pytest.approx(gold.money_for_points(1500, 0.10))

    tp.observe_venue(venue_position(stop_loss=2600.0, current_price=2605.0))
    at_breakeven = tp.risk_money(gold, 2605.0)
    assert at_breakeven < at_entry
    assert at_breakeven == pytest.approx(gold.money_for_points(500, 0.10))

    tp.observe_venue(venue_position(stop_loss=2610.0, current_price=2612.0))
    assert tp.risk_money(gold, 2612.0) == pytest.approx(gold.money_for_points(200, 0.10))


def test_an_unprotected_position_is_not_treated_as_zero_risk(gold):
    """Falling back to the entry stop is the safe direction to be wrong in."""
    tp = tracked()
    tp.observe_venue(venue_position(stop_loss=None, current_price=2605.0))
    assert tp.current_stop is None
    assert tp.risk_money(gold, 2605.0) == pytest.approx(gold.money_for_points(1500, 0.10))


def test_risk_never_goes_negative_when_price_is_beyond_the_stop(gold):
    tp = tracked(side=Side.BUY)
    tp.observe_venue(venue_position(stop_loss=2610.0, current_price=2605.0))
    assert tp.risk_money(gold, 2605.0) == 0.0


def test_volume_follows_the_venue_after_a_partial_close(gold):
    tp = tracked()
    tp.observe_venue(venue_position(volume=0.05))
    assert tp.volume == pytest.approx(0.05)
    assert tp.risk_money(gold, 2605.0) == pytest.approx(gold.money_for_points(1500, 0.05))


def test_r_multiple_is_measured_against_the_entry_stop():
    """R is fixed at entry. If it moved with the stop, a trailed winner would report a
    smaller R the better it did."""
    tp = tracked()
    assert tp.r_multiple(2610.0) == pytest.approx(1.0)
    tp.observe_venue(venue_position(stop_loss=2605.0))
    assert tp.r_multiple(2610.0) == pytest.approx(1.0), "R must not change when the stop moves"


def test_mae_and_mfe_track_the_extremes():
    tp = tracked()
    for price in (2603.0, 2596.0, 2612.0, 2601.0):
        tp.observe_price(price)
    assert tp.best_price == 2612.0
    assert tp.worst_price == 2596.0

    short = tracked(side=Side.SELL, entry_price=2600.0, initial_stop=2610.0)
    for price in (2597.0, 2604.0, 2590.0):
        short.observe_price(price)
    assert short.best_price == 2590.0, "for a short, 'best' is the lowest price"
    assert short.worst_price == 2604.0


def test_adopted_positions_are_flagged_and_carry_the_live_stop():
    tracker = PositionTracker()
    adopted = tracker.adopt(venue_position(ticket=9, stop_loss=2598.0),
                            strategy="SM1", reason="restart")
    assert adopted.stop_reconstructed is True
    assert adopted.current_stop == pytest.approx(2598.0)
    assert adopted.initial_stop == pytest.approx(2598.0), (
        "with no record, the live stop is the best available estimate of the entry stop"
    )
    assert adopted.notes == ["restart"]


def test_tracker_indexes_by_ticket_and_client_id():
    tracker = PositionTracker()
    tracker.add(tracked(ticket=5, client_order_id="AX"))
    assert tracker.get(5) is not None
    assert tracker.by_client_id("AX") is not None
    assert tracker.by_symbol("XAUUSD")
    assert len(tracker) == 1

    tracker.remove(5)
    assert tracker.get(5) is None
    assert tracker.by_client_id("AX") is None
    assert tracker.remove(999) is None


def test_position_view_hides_money_from_the_strategy():
    """A strategy that can see its own P/L will eventually be written to behave differently
    when losing. Risk-of-ruin behaviour belongs to the risk engine."""
    from atlas.strategy.base import PositionView

    tp = tracked()
    tp.observe_venue(venue_position(current_price=2612.0))
    view = tp.to_view(venue_position(current_price=2612.0))
    assert isinstance(view, PositionView)
    assert not hasattr(view, "profit")
    assert view.r_multiple_open == pytest.approx(1.2)
    assert view.stop_reconstructed is False
