"""Simulated venue economics and order-router idempotency."""

from __future__ import annotations

import pytest

from atlas.core.clock import FrozenClock
from atlas.core.decision import make_client_order_id
from atlas.core.enums import ExitReason, OrderStatus, OrderType, Side
from atlas.core.errors import TransientError
from atlas.core.market import Quote
from atlas.core.trading import OrderRequest
from atlas.execution.router import OrderRouter, RetryPolicy
from atlas.execution.venue import VenueCapabilities
from atlas.venues.sim.costs import CostModel, cost_ratio
from atlas.venues.sim.venue import SimConfig, SimulatedVenue

MIN = 60_000


def mk_venue(gold, **kw) -> SimulatedVenue:
    costs = kw.pop("costs", CostModel(entry_slippage_points=0.0, entry_slippage_std=0.0,
                                      stop_slippage_points=0.0, stop_slippage_std=0.0,
                                      commission_per_lot_per_side=0.0, swap_enabled=False))
    return SimulatedVenue({gold.name: gold}, config=SimConfig(**kw), costs=costs)


def q(gold, ts: int, mid: float, spread_points: float = 20.0) -> Quote:
    half = spread_points * gold.point / 2
    return Quote(symbol=gold.name, ts=ts, bid=round(mid - half, gold.digits),
                 ask=round(mid + half, gold.digits))


def req(gold, **kw) -> OrderRequest:
    base = dict(client_order_id="A1", decision_id="d1", symbol=gold.name, side=Side.BUY,
                order_type=OrderType.MARKET, volume=0.10, deviation_points=100)
    base.update(kw)
    return OrderRequest(**base)


# --- the temporal contract -------------------------------------------------------------


async def test_market_order_fills_on_the_NEXT_quote_not_the_current_one(gold):
    """ADR-015/017. Filling from the quote already on hand would be filling at the signal
    bar's close -- the classic backtest inflation."""
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    r = await v.submit(req(gold))
    assert r.accepted and r.status is OrderStatus.PENDING_NEW
    assert not await v.positions(), "no position may exist before the next quote"

    v.on_quote(q(gold, MIN, 2610.00))
    pos = await v.positions()
    assert len(pos) == 1
    assert pos[0].open_price == pytest.approx(2610.10), "filled at the NEXT quote's ask"


async def test_fill_price_includes_the_spread(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00, spread_points=40))
    await v.submit(req(gold, side=Side.BUY))
    v.on_quote(q(gold, MIN, 2600.00, spread_points=40))
    buy = (await v.positions())[0]
    assert buy.open_price == pytest.approx(2600.20)  # lifted the ask

    v2 = mk_venue(gold)
    await v2.connect()
    v2.on_quote(q(gold, 0, 2600.00, spread_points=40))
    await v2.submit(req(gold, side=Side.SELL, client_order_id="A2"))
    v2.on_quote(q(gold, MIN, 2600.00, spread_points=40))
    assert (await v2.positions())[0].open_price == pytest.approx(2599.80)  # hit the bid


# --- broker-rule fidelity ----------------------------------------------------------------


async def test_stops_inside_the_brokers_stops_level_are_rejected(gold):
    """Accepting a stop the broker would refuse manufactures trades that cannot exist."""
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    r = await v.submit(req(gold, stop_loss=2599.90))  # 10 pts, inside the 50-pt stops level
    assert not r.accepted and r.retcode == 10016
    assert "stops level" in r.retcode_text
    ok = await v.submit(req(gold, stop_loss=2598.00, client_order_id="A3"))
    assert ok.accepted


async def test_stop_on_the_wrong_side_is_rejected(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    r = await v.submit(req(gold, side=Side.BUY, stop_loss=2610.00))
    assert not r.accepted and "not below" in r.retcode_text


async def test_invalid_volume_is_rejected(gold):
    """Off-grid volumes are rejected, not silently rounded -- that is what MT5 does."""
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    assert not (await v.submit(req(gold, volume=0.001))).accepted
    off_grid = await v.submit(req(gold, volume=0.015, client_order_id="A9"))
    assert not off_grid.accepted and off_grid.retcode == 10014
    assert "lot step" in off_grid.retcode_text
    assert (await v.submit(req(gold, volume=0.02, client_order_id="AA"))).accepted


async def test_insufficient_margin_blocks_the_fill(gold):
    v = mk_venue(gold, starting_balance=1000.0)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, volume=5.0))
    v.on_quote(q(gold, MIN, 2600.00))
    assert not await v.positions()
    assert any("insufficient margin" in m for _, _, m in v.rejections)


# --- exits ---------------------------------------------------------------------------------


async def test_stop_is_resolved_before_target_within_one_bar(gold):
    """ADR-018: when a bar contains both levels, the trader loses."""
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, stop_loss=2595.00, take_profit=2610.00))
    v.on_quote(q(gold, MIN, 2600.00))
    assert await v.positions()
    # The intrabar path visits the adverse extreme first.
    v.on_quote(q(gold, 2 * MIN, 2594.00))
    v.on_quote(q(gold, 3 * MIN, 2612.00))
    assert not await v.positions()
    assert len(v.trades) == 1 and v.trades[0].exit_reason is ExitReason.STOP_LOSS


async def test_stop_fill_slippage_makes_a_loss_worse_than_1r(gold):
    v = SimulatedVenue(
        {gold.name: gold}, config=SimConfig(),
        costs=CostModel(entry_slippage_points=0.0, entry_slippage_std=0.0,
                        stop_slippage_points=30.0, stop_slippage_std=0.0,
                        commission_per_lot_per_side=0.0, swap_enabled=False),
    )
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, stop_loss=2595.00))
    v.on_quote(q(gold, MIN, 2600.00))
    v.on_quote(q(gold, 2 * MIN, 2594.00))
    t = v.trades[0]
    assert t.exit_price < 2595.00, "a stop must be able to fill worse than its level"
    assert t.r_multiple < -1.0, f"slipped stop should lose more than 1R, got {t.r_multiple:.2f}"


async def test_take_profit_fills_at_its_level(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, stop_loss=2595.00, take_profit=2610.00))
    v.on_quote(q(gold, MIN, 2600.00))
    v.on_quote(q(gold, 2 * MIN, 2611.00))
    t = v.trades[0]
    assert t.exit_reason is ExitReason.TAKE_PROFIT
    assert t.exit_price == pytest.approx(2610.00)
    assert t.r_multiple > 0


async def test_mae_and_mfe_are_tracked(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, stop_loss=2590.00, take_profit=2650.00))
    v.on_quote(q(gold, MIN, 2600.00))
    for mid in (2597.0, 2606.0, 2599.0):
        v.on_quote(q(gold, MIN * 2, mid))
    p = v.sim_position(1)
    assert p.mae_points < 0 and p.mfe_points > 0


# --- costs -------------------------------------------------------------------------------


async def test_commission_is_charged_on_both_sides(gold):
    v = SimulatedVenue(
        {gold.name: gold}, config=SimConfig(),
        costs=CostModel(entry_slippage_points=0.0, entry_slippage_std=0.0,
                        stop_slippage_points=0.0, stop_slippage_std=0.0,
                        commission_per_lot_per_side=7.0, swap_enabled=False),
    )
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, volume=1.0, take_profit=2610.00, stop_loss=2590.0))
    v.on_quote(q(gold, MIN, 2600.00))
    v.on_quote(q(gold, 2 * MIN, 2611.00))
    assert v.trades[0].commission == pytest.approx(-14.0)


async def test_swap_accrues_daily_and_triples_on_the_rollover_day(gold):
    spec = gold.model_copy(update={"swap_long": -5.0, "swap_mode": 1, "swap_rollover_3days": 2})
    v = SimulatedVenue({spec.name: spec}, config=SimConfig(),
                       costs=CostModel(entry_slippage_points=0.0, entry_slippage_std=0.0,
                                       commission_per_lot_per_side=0.0))
    await v.connect()
    # 2026-05-11 is a Monday.
    from datetime import UTC, datetime

    from atlas.core.market import utc_ms
    day0 = utc_ms(datetime(2026, 5, 11, 12, tzinfo=UTC))
    v.on_quote(q(spec, day0, 2600.00))
    await v.submit(req(spec, volume=1.0))
    v.on_quote(q(spec, day0 + 60_000, 2600.00))
    p = v.sim_position(1)
    assert p.swap == 0.0
    v.on_quote(q(spec, day0 + 86_400_000, 2600.00))  # Tuesday
    assert p.swap == pytest.approx(-5.0)
    v.on_quote(q(spec, day0 + 2 * 86_400_000, 2600.00))  # Wednesday = triple day
    assert p.swap == pytest.approx(-20.0), "Wednesday must add 3x the nightly swap"


def test_cost_ratio_shows_short_stops_are_unviable(gold):
    m = CostModel(commission_per_lot_per_side=3.5)
    tight = cost_ratio(gold, m, volume=0.2, stop_points=40, spread_points=25)
    wide = cost_ratio(gold, m, volume=0.2, stop_points=800, spread_points=25)
    assert tight["cost_ratio"] > 0.5, "a 40-pt gold stop pays most of its risk in costs"
    assert wide["cost_ratio"] < 0.06
    assert tight["total_cost"] == pytest.approx(wide["total_cost"]), "cost is fixed per trade"


# --- margin stop-out -----------------------------------------------------------------------


async def test_stop_out_closes_positions_when_margin_collapses(gold):
    v = mk_venue(gold, starting_balance=3000.0, stop_out_level_pct=50.0)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, volume=1.0))  # margin 2000 of a 3000 account
    v.on_quote(q(gold, MIN, 2600.00))
    assert len(await v.positions()) == 1
    v.on_quote(q(gold, 2 * MIN, 2580.00))  # -$2000 floating
    assert not await v.positions()
    assert v.trades[0].exit_reason is ExitReason.STOP_OUT


# --- pending orders ------------------------------------------------------------------------


async def test_stop_order_triggers_and_slips(gold):
    v = SimulatedVenue({gold.name: gold}, config=SimConfig(),
                       costs=CostModel(stop_slippage_points=10.0, stop_slippage_std=0.0,
                                       entry_slippage_points=0.0, entry_slippage_std=0.0,
                                       commission_per_lot_per_side=0.0, swap_enabled=False))
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    r = await v.submit(req(gold, order_type=OrderType.STOP, price=2610.00, stop_loss=2600.0))
    assert r.accepted and r.status is OrderStatus.WORKING
    assert len(await v.pending_orders()) == 1
    v.on_quote(q(gold, MIN, 2605.00))
    assert not await v.positions()
    v.on_quote(q(gold, 2 * MIN, 2612.00))
    pos = await v.positions()
    assert len(pos) == 1
    # 10 pts of modelled stop slippage, scaled by spread/reference = 20/25 = 0.8 -> 8 pts.
    assert pos[0].open_price == pytest.approx(2610.08), "stop orders slip adversely"
    assert pos[0].open_price > 2610.00


async def test_limit_order_does_not_slip(gold):
    v = SimulatedVenue({gold.name: gold}, config=SimConfig(),
                       costs=CostModel(stop_slippage_points=50.0, entry_slippage_points=0.0,
                                       entry_slippage_std=0.0, commission_per_lot_per_side=0.0,
                                       swap_enabled=False))
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, order_type=OrderType.LIMIT, price=2590.00, stop_loss=2580.0))
    v.on_quote(q(gold, MIN, 2589.00))
    assert (await v.positions())[0].open_price == pytest.approx(2590.00)


async def test_cancel_order(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    r = await v.submit(req(gold, order_type=OrderType.STOP, price=2610.00))
    c = await v.cancel_order(r.order_ticket)
    assert c.accepted and not await v.pending_orders()
    assert not (await v.cancel_order(99999)).accepted


# --- modification --------------------------------------------------------------------------


async def test_modify_rejects_a_stop_inside_the_stops_level(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, stop_loss=2595.00))
    v.on_quote(q(gold, MIN, 2600.00))
    bad = await v.modify_position(1, stop_loss=2599.95)
    assert not bad.accepted and bad.retcode == 10016
    good = await v.modify_position(1, stop_loss=2598.00)
    assert good.accepted and v.sim_position(1).stop_loss == pytest.approx(2598.00)


async def test_partial_close_leaves_a_smaller_position(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, volume=0.20))
    v.on_quote(q(gold, MIN, 2600.00))
    r = await v.close_position(1, volume=0.10)
    assert r.accepted
    assert v.sim_position(1).volume == pytest.approx(0.10)
    assert len(v.trades) == 1


# --- router idempotency -----------------------------------------------------------------------


class FlakyVenue(SimulatedVenue):
    """A venue that loses the response to the first N submissions *after* processing them.

    This reproduces the exact scenario the idempotency key exists for: the order landed, the
    caller does not know it.
    """

    def __init__(self, *a, fail_first: int = 1, land_anyway: bool = True, **kw):
        super().__init__(*a, **kw)
        self.fail_first = fail_first
        self.land_anyway = land_anyway
        self.submit_calls = 0

    async def submit(self, request):
        self.submit_calls += 1
        if self.submit_calls <= self.fail_first:
            if self.land_anyway:
                await super().submit(request)
                self._fill_queued_market(self._quotes[request.symbol])
            raise TransientError("connection reset before the response arrived")
        return await super().submit(request)


async def test_router_does_not_double_fill_after_a_lost_response(gold):
    """The core safety property: a timed-out order that actually landed must not be resent."""
    v = FlakyVenue({gold.name: gold}, config=SimConfig(),
                   costs=CostModel(commission_per_lot_per_side=0.0, entry_slippage_points=0.0,
                                   entry_slippage_std=0.0, swap_enabled=False),
                   fail_first=1, land_anyway=True)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    router = OrderRouter(v, FrozenClock(0), policy=RetryPolicy(max_attempts=3, base_delay_s=0))
    out = await router.submit(req(gold, client_order_id=make_client_order_id("d1")))
    assert out.accepted
    assert out.resolved_by_lookup, "the retry must have been resolved by lookup, not resubmission"
    assert len(await v.positions()) == 1, "exactly one position, not two"
    assert not router.in_flight


async def test_router_retries_when_nothing_landed(gold):
    v = FlakyVenue({gold.name: gold}, config=SimConfig(),
                   costs=CostModel(commission_per_lot_per_side=0.0, swap_enabled=False),
                   fail_first=1, land_anyway=False)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    router = OrderRouter(v, FrozenClock(0), policy=RetryPolicy(max_attempts=3, base_delay_s=0))
    out = await router.submit(req(gold))
    assert out.accepted and out.attempts == 2 and not out.resolved_by_lookup


async def test_router_refuses_to_retry_when_lookup_is_unavailable(gold):
    """Unfilled is a missed trade; double-filled is a loss. Those are not symmetric."""

    class NoLookupVenue(FlakyVenue):
        @property
        def capabilities(self) -> VenueCapabilities:
            return VenueCapabilities(client_id_lookup=False, name="no-lookup")

        async def find_by_client_id(self, client_order_id):
            return None

    v = NoLookupVenue({gold.name: gold}, config=SimConfig(), fail_first=1, land_anyway=True)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    router = OrderRouter(v, FrozenClock(0), policy=RetryPolicy(max_attempts=3, base_delay_s=0))
    out = await router.submit(req(gold))
    assert not out.accepted
    assert "no idempotency lookup" in out.error
    assert v.submit_calls == 1, "it must not have tried again"


async def test_router_does_not_retry_a_final_rejection(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    router = OrderRouter(v, FrozenClock(0), policy=RetryPolicy(max_attempts=5, base_delay_s=0))
    out = await router.submit(req(gold, stop_loss=2599.95))  # invalid stops, retcode 10016
    assert not out.accepted and out.attempts == 1
    assert "10016" in out.error


async def test_router_close_is_more_persistent_than_open(gold):
    """Failing to open is a missed trade; failing to close is unbounded risk."""
    calls = {"n": 0}

    class StubbornVenue(SimulatedVenue):
        async def close_position(self, ticket, *, volume=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError("busy")
            return await super().close_position(ticket, volume=volume)

    v = StubbornVenue({gold.name: gold}, config=SimConfig(),
                      costs=CostModel(commission_per_lot_per_side=0.0, swap_enabled=False))
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold))
    v.on_quote(q(gold, MIN, 2600.00))
    router = OrderRouter(v, FrozenClock(0), policy=RetryPolicy(max_attempts=3, base_delay_s=0))
    r = await router.close(1)
    assert r.accepted and calls["n"] == 3


async def test_find_by_client_id_distinguishes_open_from_never_existed(gold):
    v = mk_venue(gold)
    await v.connect()
    v.on_quote(q(gold, 0, 2600.00))
    await v.submit(req(gold, client_order_id="AX"))
    v.on_quote(q(gold, MIN, 2600.00))
    assert await v.find_by_client_id("AX") is not None
    assert await v.find_by_client_id("NOPE") is None
    await v.close_position(1)
    assert await v.find_by_client_id("AX") is None
    assert v.was_filled("AX"), "a closed order must still be known to have filled"
    assert not v.was_filled("NOPE")
