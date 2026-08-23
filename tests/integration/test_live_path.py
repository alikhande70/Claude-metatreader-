"""The live path, end to end.

Exercises the code that only runs in a live session -- LiveDataSource ordering, the paper
venue's data/execution split, and the engine driving a real socket -- with the same engine
object a backtest uses.

What this does NOT cover is the MQL5 side; the peer here is the conformance fake terminal.
See docs/STATUS.md for that boundary.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from tests.conformance.fake_terminal import FakeTerminal
from tests.conformance.test_bridge_protocol import free_port, q

from atlas.core.clock import FrozenClock, SystemClock
from atlas.core.enums import Timeframe
from atlas.core.market import Bar, Quote
from atlas.runtime.live import LiveDataSource
from atlas.venues.mt5.bridge import BridgeVenue
from atlas.venues.paper import PaperVenue
from atlas.venues.sim.costs import CostModel
from atlas.venues.sim.venue import SimConfig, SimulatedVenue

MIN = 60_000


def bar(ts: int, tf: Timeframe, close: float = 2600.0) -> Bar:
    return Bar(symbol="XAUUSD", tf=tf, ts=ts, open=close, high=close + 1, low=close - 1,
               close=close, volume=10.0, spread_points=20.0)


# --- LiveDataSource ordering ---------------------------------------------------------


async def test_bars_closing_together_are_delivered_longest_first(gold):
    """The same ordering property the aggregator guarantees in replay must hold live.

    The bridge pushes each timeframe as its own message, so without this the trigger
    timeframe could be handled before the higher-timeframe bar that closed at the same
    instant -- and only on the bars where the HTF bias was most likely to have just changed.
    """
    src = LiveDataSource(["XAUUSD"], {"XAUUSD": gold}, {})
    ts = 3_600_000
    for tf in (Timeframe.M5, Timeframe.H1, Timeframe.M15):
        src.push_bar(bar(ts - tf.seconds * 1000, tf))

    seen = []
    stream = src.stream()
    for _ in range(3):
        seen.append(await asyncio.wait_for(anext(stream), 2))
    order = [u.tf for u in seen]
    assert order == [Timeframe.H1, Timeframe.M15, Timeframe.M5], order


async def test_quotes_are_dropped_under_pressure_but_bars_never_are(gold):
    """A quote is a sampled view and a stale one is worth less than a fresh one. A bar close
    is a decision point, and losing one loses a decision."""
    src = LiveDataSource(["XAUUSD"], {"XAUUSD": gold}, {}, queue_size=4)
    for i in range(20):
        src.push_quote(Quote(symbol="XAUUSD", ts=i, bid=2600.0, ask=2600.2))
    assert src.dropped > 0

    src2 = LiveDataSource(["XAUUSD"], {"XAUUSD": gold}, {}, queue_size=2)
    src2.push_bar(bar(0, Timeframe.M5))
    src2.push_bar(bar(MIN, Timeframe.M5))
    with pytest.raises(asyncio.QueueFull):
        src2.push_bar(bar(2 * MIN, Timeframe.M5))


async def test_warmup_and_spec_lookup(gold):
    from atlas.core.errors import DataError
    from atlas.data.series import BarSeries

    series = BarSeries.from_bars([bar(i * 300_000, Timeframe.M5) for i in range(5)])
    src = LiveDataSource(["XAUUSD"], {"XAUUSD": gold}, {("XAUUSD", Timeframe.M5): series})
    assert len(await src.warmup()) == 1
    assert src.spec("XAUUSD") is gold
    with pytest.raises(DataError, match="no symbol spec"):
        src.spec("EURUSD")


# --- Paper venue ----------------------------------------------------------------------


@contextlib.asynccontextmanager
async def paper_harness(gold):
    """A paper venue whose data side is a real socket to a fake terminal."""
    upstream = SimulatedVenue({gold.name: gold}, config=SimConfig(starting_balance=50_000.0),
                              costs=CostModel(commission_per_lot_per_side=0.0,
                                              entry_slippage_points=0.0,
                                              entry_slippage_std=0.0, swap_enabled=False))
    await upstream.connect()
    port = free_port()
    bridge = BridgeVenue(host="127.0.0.1", port=port, token="", request_timeout_ms=1500,
                         clock=FrozenClock(0))
    await bridge.start()
    terminal = FakeTerminal(upstream, token="")
    await terminal.connect("127.0.0.1", port)
    sim = SimulatedVenue({}, config=SimConfig(starting_balance=10_000.0),
                         costs=CostModel(commission_per_lot_per_side=0.0,
                                         entry_slippage_points=0.0, entry_slippage_std=0.0,
                                         swap_enabled=False))
    venue = PaperVenue(bridge, sim)
    try:
        yield venue, terminal, upstream
    finally:
        await terminal.close()
        await venue.disconnect()


async def test_paper_takes_specs_from_the_broker_and_money_from_the_simulator(gold):
    async with paper_harness(gold) as (venue, terminal, upstream):
        await venue.connect()
        terminal.advance(q(gold, MIN, 2600.0))

        specs = await venue.symbol_specs([gold.name])
        assert specs[gold.name].tick_value == gold.tick_value
        # The simulator must be pricing against the BROKER's spec, or a paper session sizes
        # differently from the live session it is meant to rehearse.
        assert venue.execution.specs[gold.name].contract_size == gold.contract_size

        account = await venue.account()
        assert account.balance == pytest.approx(10_000.0), "money is the simulator's"
        assert upstream.account_state().balance == pytest.approx(50_000.0)

        quote = await venue.quote(gold.name)
        assert quote.ask > quote.bid, "prices are the broker's"


async def test_paper_orders_never_reach_the_upstream_broker(gold):
    async with paper_harness(gold) as (venue, terminal, upstream):
        await venue.connect()
        await venue.symbol_specs([gold.name])
        terminal.advance(q(gold, MIN, 2600.0))
        venue.observe_quote(await venue.quote(gold.name))

        from atlas.core.enums import OrderType, Side
        from atlas.core.trading import OrderRequest

        result = await venue.submit(OrderRequest(
            client_order_id="P1", decision_id="d", symbol=gold.name, side=Side.BUY,
            order_type=OrderType.MARKET, volume=0.10, stop_loss=2595.0, magic=1,
        ))
        assert result.accepted
        terminal.advance(q(gold, 2 * MIN, 2600.0))
        venue.observe_quote(await venue.quote(gold.name))

        assert len(await venue.positions()) == 1, "the simulator holds the position"
        assert not await upstream.positions(), "nothing reached the upstream broker"
        assert await venue.find_by_client_id("P1") is not None


async def test_paper_health_follows_the_data_connection(gold):
    """Simulated execution always works; the thing that can fail is the market feed, so that
    is what health must report."""
    async with paper_harness(gold) as (venue, terminal, _):
        assert (await venue.connect()).connected
        await terminal.close()
        await asyncio.sleep(0.1)
        assert not (await venue.health()).connected


# --- offline paper is refused rather than silently useless -------------------------------


async def test_offline_sim_venue_is_refused_with_a_useful_message(tmp_path, gold):
    from atlas.config.settings import AtlasSettings, SymbolSettings, VenueSettings
    from atlas.core.errors import ConfigError
    from atlas.runtime.live import build_live_engine

    cfg = AtlasSettings(
        name="offline", journal_dir=str(tmp_path), state_dir=str(tmp_path),
        symbols=[SymbolSettings(symbol="XAUUSD", strategy="SM1", spec=gold,
                                parameters={"htf": "H1", "mtf": "M15", "ltf": "M5"})],
        venue=VenueSettings(kind="sim"),
    )
    with pytest.raises(ConfigError, match="just a backtest"):
        await build_live_engine(cfg, paper=False)


def test_system_clock_is_used_live_and_is_not_simulated():
    clock = SystemClock()
    assert not clock.is_simulated
    assert clock.now_ms() > 1_700_000_000_000
