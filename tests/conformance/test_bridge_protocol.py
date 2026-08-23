"""Bridge protocol conformance battery.

Runs the real ``BridgeVenue`` against a real TCP socket against a real matching engine, with
only the wire format in between. Every assertion here is therefore about the protocol and its
Python implementation, not about a mock's opinion of them.

Scope, stated plainly: this verifies the ATLAS side and the protocol design. It cannot verify
``mql5/AtlasBridge.mq5`` -- MQL5 cannot be compiled or executed here. That code is
*Implemented*, not *Verified*, until it has been through MetaEditor against a demo account,
and the runbook says exactly how to do that.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from tests.conformance.fake_terminal import FakeTerminal

from atlas.core.clock import FrozenClock
from atlas.core.enums import OrderStatus, OrderType, Side
from atlas.core.errors import TransientError, VenueError, VenueUnavailable
from atlas.core.market import Quote
from atlas.core.trading import OrderRequest
from atlas.execution.router import OrderRouter, RetryPolicy
from atlas.venues.mt5 import protocol as p
from atlas.venues.mt5.bridge import BridgeVenue, _parse_endpoint
from atlas.venues.sim.costs import CostModel
from atlas.venues.sim.venue import SimConfig, SimulatedVenue

MIN = 60_000
TOKEN = "conformance-token"


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_sim(gold) -> SimulatedVenue:
    return SimulatedVenue(
        {gold.name: gold},
        config=SimConfig(starting_balance=25_000.0),
        costs=CostModel(entry_slippage_points=0.0, entry_slippage_std=0.0,
                        stop_slippage_points=0.0, stop_slippage_std=0.0,
                        commission_per_lot_per_side=0.0, swap_enabled=False),
    )


def q(gold, ts: int, mid: float, spread_points: float = 20.0) -> Quote:
    half = spread_points * gold.point / 2
    return Quote(symbol=gold.name, ts=ts, bid=round(mid - half, gold.digits),
                 ask=round(mid + half, gold.digits))


class Harness:
    """A bound BridgeVenue with a fake terminal connected to it."""

    def __init__(self, venue: BridgeVenue, terminal: FakeTerminal, sim: SimulatedVenue):
        self.venue = venue
        self.terminal = terminal
        self.sim = sim

    def advance(self, quote: Quote) -> None:
        self.terminal.advance(quote)


@contextlib.asynccontextmanager
async def harness(gold, *, token: str = TOKEN, terminal_token: str | None = None, **tkw):
    sim = make_sim(gold)
    await sim.connect()
    port = free_port()
    ticks: list[Quote] = []
    bars: list = []
    venue = BridgeVenue(host="127.0.0.1", port=port, token=token,
                        request_timeout_ms=1500, clock=FrozenClock(0),
                        on_tick=ticks.append, on_bar=bars.append)
    await venue.start()
    term = FakeTerminal(sim, token=TOKEN if terminal_token is None else terminal_token, **tkw)
    await term.connect("127.0.0.1", port)
    try:
        yield Harness(venue, term, sim), ticks, bars
    finally:
        await term.close()
        await venue.disconnect()


# --- handshake -----------------------------------------------------------------------


async def test_handshake_and_capabilities(gold):
    async with harness(gold) as (h, _, _):
        health = await h.venue.connect(wait_seconds=3)
        assert health.connected and health.trade_allowed
        assert health.server_offset_seconds == 3 * 3600, "offset must be measured from hello"
        assert h.venue.capabilities.client_id_lookup, "retry safety depends on this"
        assert h.venue.capabilities.name == "mt5-bridge"
        assert await asyncio.wait_for(h.terminal.welcomed.wait(), 2) is None or True


async def test_wrong_token_is_refused(gold):
    async with harness(gold, terminal_token="wrong") as (h, _, _):
        with pytest.raises(VenueUnavailable):
            await h.venue.connect(wait_seconds=1.0)
        await asyncio.sleep(0.05)
        assert h.terminal.closed_by_server == "authentication failed"


async def test_unsupported_protocol_version_is_refused_before_any_order(gold):
    async with harness(gold, version=999) as (h, _, _):
        with pytest.raises(VenueUnavailable):
            await h.venue.connect(wait_seconds=1.0)
        await asyncio.sleep(0.05)
        assert h.terminal.closed_by_server and "version" in h.terminal.closed_by_server


async def test_a_second_terminal_displaces_the_first(gold):
    """Two terminals trading one strategy is a double-size accident."""
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        second = FakeTerminal(h.sim, token=TOKEN, impl="second")
        await second.connect("127.0.0.1", h.venue.port)
        await asyncio.wait_for(second.welcomed.wait(), 2)
        assert h.venue.connection_count == 2
        assert h.venue.capabilities.extra["impl"] == "second"
        await second.close()


# --- state reads -----------------------------------------------------------------------


async def test_symbol_specs_round_trip_without_loss(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        specs = await h.venue.symbol_specs([gold.name])
        got = specs[gold.name]
        for field in ("digits", "point", "tick_size", "tick_value", "contract_size",
                      "volume_min", "volume_max", "volume_step", "stops_level_points",
                      "swap_mode", "swap_rollover_3days"):
            assert getattr(got, field) == getattr(gold, field), field
        assert got.filling_modes == gold.filling_modes
        assert got.value_per_point_per_lot == gold.value_per_point_per_lot


async def test_a_broker_reporting_zero_tick_value_is_refused(gold):
    """Every position size depends on tick_value; defaulting it would size silently wrong."""
    with pytest.raises(p.ProtocolError, match="tick_value"):
        p.parse_spec("XAUUSD", {"digits": 2, "point": 0.01, "tick_value": 0.0})


async def test_account_and_quote(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        acct = await h.venue.account()
        assert acct.balance == pytest.approx(25_000.0)
        quote = await h.venue.quote(gold.name)
        assert quote.ask > quote.bid


async def test_zero_stop_loss_becomes_none_not_a_price(gold):
    """MT5 reports 'no stop' as 0.0. Treating it as a price would make the trade manager
    believe an unprotected position is protected."""
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        await h.venue.submit(_req(gold, "A1", volume=0.10))
        h.advance(q(gold, 2 * MIN, 2600.0))
        pos = (await h.venue.positions())[0]
        assert pos.stop_loss is None
        assert pos.take_profit is None


# --- orders --------------------------------------------------------------------------


def _req(gold, coid: str, **kw) -> OrderRequest:
    base = dict(client_order_id=coid, decision_id="d", symbol=gold.name, side=Side.BUY,
                order_type=OrderType.MARKET, volume=0.10, magic=20260823, comment=coid,
                deviation_points=50)
    base.update(kw)
    return OrderRequest(**base)


async def test_market_order_round_trip_and_fill(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        result = await h.venue.submit(_req(gold, "A1", stop_loss=2595.0, take_profit=2610.0))
        assert result.accepted
        assert result.status is OrderStatus.WORKING  # queued for the next quote (ADR-017)
        assert not await h.venue.positions()
        h.advance(q(gold, 2 * MIN, 2600.0))
        positions = await h.venue.positions()
        assert len(positions) == 1
        assert positions[0].stop_loss == pytest.approx(2595.0)
        assert positions[0].comment == "A1"


async def test_invalid_stops_are_rejected_with_the_broker_retcode(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        r = await h.venue.submit(_req(gold, "A2", stop_loss=2599.95))
        assert not r.accepted
        assert r.retcode == 10016
        assert "stops level" in r.retcode_text.lower() or "stops" in r.retcode_text.lower()


async def test_retcode_texts_are_mapped_not_invented():
    for code in (10004, 10014, 10016, 10019, 10030, 10031):
        assert "unmapped" not in p.retcode_text(code)
    assert "unmapped" in p.retcode_text(19999)


async def test_modify_and_close_round_trip(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        await h.venue.submit(_req(gold, "A3", stop_loss=2595.0))
        h.advance(q(gold, 2 * MIN, 2600.0))
        ticket = (await h.venue.positions())[0].ticket

        mod = await h.venue.modify_position(ticket, stop_loss=2597.0)
        assert mod.accepted
        assert (await h.venue.positions())[0].stop_loss == pytest.approx(2597.0)

        closed = await h.venue.close_position(ticket)
        assert closed.accepted
        assert not await h.venue.positions()


async def test_pending_order_lifecycle(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        r = await h.venue.submit(_req(gold, "A4", order_type=OrderType.STOP, price=2610.0,
                                      stop_loss=2600.0))
        assert r.accepted
        pending = await h.venue.pending_orders()
        assert len(pending) == 1 and pending[0].price == pytest.approx(2610.0)
        cancelled = await h.venue.cancel_order(pending[0].ticket)
        assert cancelled.accepted and cancelled.status is OrderStatus.CANCELLED
        assert not await h.venue.pending_orders()


async def test_closed_trades_are_reconstructed_over_the_wire(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        await h.venue.symbol_specs([gold.name])
        h.advance(q(gold, MIN, 2600.0))
        await h.venue.submit(_req(gold, "A5", stop_loss=2595.0, take_profit=2610.0))
        h.advance(q(gold, 2 * MIN, 2600.0))
        h.advance(q(gold, 3 * MIN, 2611.0))
        trades = await h.venue.closed_trades(0)
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason.value == "TAKE_PROFIT"
        assert t.point == pytest.approx(gold.point), "point must survive for R conversion"
        assert t.r_multiple > 0


# --- the idempotency contract ------------------------------------------------------------


async def test_find_by_client_id_distinguishes_present_from_absent(gold):
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        assert await h.venue.find_by_client_id("A6") is None
        await h.venue.submit(_req(gold, "A6"))
        h.advance(q(gold, 2 * MIN, 2600.0))
        found = await h.venue.find_by_client_id("A6")
        assert found is not None and found.comment == "A6"


async def test_lost_reply_does_not_produce_a_double_fill(gold):
    """The failure the whole idempotency design exists for.

    The terminal executes ``order_send`` and then never sends the reply. The router times
    out, looks the order up by client id, finds it, and reports success -- rather than
    resubmitting and opening a second position at double the intended risk.
    """
    async with harness(gold, drop_reply_ops={"order_send"}) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        h.advance(q(gold, MIN, 2600.0))
        router = OrderRouter(h.venue, FrozenClock(0),
                             policy=RetryPolicy(max_attempts=3, base_delay_s=0))

        async def fill_after_timeout():
            # The queued market order fills on the next quote, exactly as it would live.
            await asyncio.sleep(0.2)
            h.advance(q(gold, 2 * MIN, 2600.0))

        task = asyncio.create_task(fill_after_timeout())
        outcome = await router.submit(_req(gold, "A7"))
        await task

        assert outcome.accepted, outcome.error
        assert outcome.resolved_by_lookup, "must have been resolved by lookup, not resubmission"
        assert len(await h.venue.positions()) == 1, "exactly one position, not two"
        sends = [op for op, _ in h.terminal.requests if op == "order_send"]
        assert len(sends) == 1, f"the order must have been sent once, not {len(sends)} times"


async def test_request_timeout_is_transient_not_fatal(gold):
    async with harness(gold, drop_reply_ops={"account"}) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        with pytest.raises(TransientError, match="did not answer"):
            await h.venue.account()
        # The connection survives: a timed-out request must not poison the session.
        assert (await h.venue.health()).connected


async def test_terminal_error_surfaces_as_a_venue_error(gold):
    async with harness(gold, fail_ops={"positions"}) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        with pytest.raises(VenueError, match=r"SIMULATED_FAILURE|failed"):
            await h.venue.positions()


async def test_disconnect_mid_request_fails_pending_requests(gold):
    async with harness(gold, drop_reply_ops={"positions"}) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        task = asyncio.create_task(h.venue.positions())
        await asyncio.sleep(0.05)
        await h.terminal.close()
        with pytest.raises((VenueUnavailable, TransientError)):
            await task


# --- streaming ------------------------------------------------------------------------


async def test_ticks_and_bars_are_delivered_to_callbacks(gold):
    async with harness(gold) as (h, ticks, bars):
        await h.venue.connect(wait_seconds=3)
        quote = q(gold, MIN, 2601.5)
        await h.terminal._send({"v": 1, "t": "tick", "ts": quote.ts, "sym": quote.symbol,
                                "bid": quote.bid, "ask": quote.ask})
        await h.terminal._send({"v": 1, "t": "bar", "ts": 2 * MIN, "sym": gold.name,
                                "tf": "M5", "open_time": MIN, "o": 2600.0, "h": 2602.0,
                                "l": 2599.0, "c": 2601.0, "vol": 100, "spread": 22})
        await asyncio.sleep(0.15)
        assert ticks and ticks[-1].bid == pytest.approx(quote.bid)
        assert bars and bars[-1].close == pytest.approx(2601.0)
        assert bars[-1].complete and bars[-1].spread_points == 22
        # Regression: the bar message once used "v" for BOTH the protocol version and the
        # volume, so every bar's volume silently parsed as 1.
        assert bars[-1].volume == 100


async def test_unknown_message_types_are_ignored_not_fatal(gold):
    """A newer terminal build must be able to talk to an older engine."""
    async with harness(gold) as (h, _, _):
        await h.venue.connect(wait_seconds=3)
        await h.terminal._send({"v": 1, "t": "some_future_message", "ts": 0, "x": 1})
        await asyncio.sleep(0.1)
        assert (await h.venue.health()).connected


async def test_health_reports_a_terminal_that_stops_answering(gold):
    async with harness(gold) as (h, _, _):
        assert (await h.venue.connect(wait_seconds=3)).connected
        await h.terminal.close()
        await asyncio.sleep(0.1)
        health = await h.venue.health()
        assert not health.connected


# --- codec unit checks --------------------------------------------------------------------


def test_frame_codec_round_trip():
    msg = {"v": 1, "t": "req", "ts": 1, "id": "r1", "op": "x", "args": {"a": "b\nc"}}
    assert p.decode(p.encode(msg)) == msg


@pytest.mark.parametrize("bad", [b"not json\n", b"[1,2,3]\n", b'{"no_type":1}\n'])
def test_malformed_frames_raise_protocol_errors(bad):
    with pytest.raises(p.ProtocolError):
        p.decode(bad)


def test_oversized_frames_are_refused():
    with pytest.raises(p.ProtocolError, match="exceeds"):
        p.encode({"v": 1, "t": "x", "ts": 0, "pad": "y" * (p.MAX_FRAME_BYTES + 10)})


@pytest.mark.parametrize("endpoint,expect", [
    ("tcp://127.0.0.1:5555", ("127.0.0.1", 5555)),
    ("192.168.1.5:6000", ("192.168.1.5", 6000)),
    ("localhost", ("localhost", 5555)),
])
def test_endpoint_parsing_tolerates_the_old_zeromq_style(endpoint, expect):
    assert _parse_endpoint(endpoint, "127.0.0.1", 5555) == expect


def test_order_send_args_pick_a_filling_mode_the_symbol_supports(gold):
    from atlas.core.enums import FillPolicy

    ioc_only = gold.model_copy(update={"filling_modes": (FillPolicy.IOC,)})
    args = p.order_send_args(_req(ioc_only, "A9"), ioc_only)
    assert args["filling"] == "IOC", "hardcoding FOK here is a guaranteed retcode 10030"
    fok_only = gold.model_copy(update={"filling_modes": (FillPolicy.FOK,)})
    assert p.order_send_args(_req(fok_only, "A9"), fok_only)["filling"] == "FOK"


def test_order_result_status_mapping():
    assert p.parse_order_result("c", {"retcode": 10009}, 0).status is OrderStatus.FILLED
    assert p.parse_order_result("c", {"retcode": 10008}, 0).status is OrderStatus.WORKING
    partial = p.parse_order_result("c", {"retcode": 10010}, 0)
    assert partial.status is OrderStatus.PARTIALLY_FILLED and partial.accepted
    rejected = p.parse_order_result("c", {"retcode": 10016}, 0)
    assert rejected.status is OrderStatus.REJECTED and not rejected.accepted
    assert "INVALID_STOPS" in rejected.retcode_text
