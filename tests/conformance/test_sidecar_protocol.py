"""Conformance battery for the Python sidecar -- the OTHER terminal-side implementation.

The sidecar could not previously be executed anywhere outside Windows, so all of it sat in
the "requires a real environment" column, including the two thirds that have nothing to do
with MetaTrader: framing, handshake, dispatch, payload shapes, retcode propagation and
idempotency lookup.

Here it is run for real. `tests/conformance/fake_mt5.py` is installed into `sys.modules` as
``MetaTrader5``, backed by the same ``SimulatedVenue`` matching engine the EA-side battery
uses, and the sidecar's own ``Sidecar.run`` loop drives it in a thread against a real
``BridgeVenue`` over a real TCP socket:

    BridgeVenue  <-  JSON over TCP  <-  atlas_mt5_sidecar.Sidecar  ->  fake MetaTrader5
                                                                          -> matching engine

WHAT THIS ESTABLISHES
    The sidecar's protocol half is correct, and the file executes rather than merely parses.

WHAT IT DOES NOT ESTABLISH
    That the real ``MetaTrader5`` package behaves the way the fake does. The fake's attribute
    names and call shapes are transcribed from documentation, and a transcription can be
    wrong. The sidecar therefore stays in the third column of docs/STATUS.md until it has run
    against a terminal -- what has moved is the *size* of what is unverified, not the fact.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import socket as socket_module
import sys
import threading
import time
from typing import Any

import pytest
from tests.conformance import fake_mt5
from tests.conformance.test_bridge_protocol import TOKEN, free_port, make_sim, q

from atlas.core.clock import FrozenClock
from atlas.core.enums import OrderStatus, OrderType, Side
from atlas.core.errors import VenueError
from atlas.core.trading import OrderRequest
from atlas.execution.router import OrderRouter, RetryPolicy
from atlas.venues.mt5.bridge import BridgeVenue

MIN = 60_000
MAGIC = 20260823
SIDECAR_PATH = "sidecar/atlas_mt5_sidecar.py"


def load_sidecar():
    """Import the sidecar with the fake package standing in for MetaTrader5.

    The module is imported from its path rather than installed, because that is how it ships:
    a single file copied next to a terminal.
    """
    sys.modules["MetaTrader5"] = fake_mt5
    spec = importlib.util.spec_from_file_location("atlas_mt5_sidecar_under_test", SIDECAR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SidecarRunner:
    """Drives the sidecar's real run loop on a background thread."""

    def __init__(self, module: Any, port: int, symbols: str = "XAUUSD"):
        self.module = module
        args = argparse.Namespace(
            host="127.0.0.1", port=port, token=TOKEN, magic=MAGIC, symbols=symbols,
            timeframes="M5", poll_ms=20, log_level="CRITICAL",
        )
        self.sidecar = module.Sidecar(args)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.sidecar.start_terminal()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # A trimmed copy of Sidecar.run's body: same connect, same read_frames, same handle,
        # same publish. Only the unbounded `while True` becomes stoppable, because a daemon
        # thread that never exits leaks between tests.
        import time as _time

        while not self._stop.is_set():
            if self.sidecar.sock is None:
                try:
                    self.sidecar.connect()
                except OSError:
                    _time.sleep(0.05)
                    continue
            for msg in self.sidecar.read_frames():
                self.sidecar.handle(msg)
            if self.sidecar.sock is None:
                continue
            with contextlib.suppress(Exception):
                self.sidecar.publish()
            _time.sleep(self.sidecar.poll_ms / 1000.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self.sidecar.close()


class Market:
    """Owns the simulated market clock.

    The clock is anchored to wall time on purpose. The sidecar infers the broker's server
    offset from ``tick.time - time.time()``, because the ``MetaTrader5`` package exposes no
    server-time call -- so a market whose timestamps start at zero would make it infer an
    offset of minus fifty-five years. Anchoring here is what lets the offset inference, and
    every timestamp conversion built on it, be exercised rather than bypassed.
    """

    def __init__(self, sim, gold, base_ms: int) -> None:
        self.sim = sim
        self.gold = gold
        self.ts = base_ms

    def tick(self, mid: float = 2400.0, *, step_ms: int = 1000) -> None:
        self.ts += step_ms
        self.sim.on_quote(q(self.gold, self.ts, mid))


@contextlib.asynccontextmanager
async def sidecar_harness(gold, *, symbols: str = "XAUUSD"):
    sim = make_sim(gold)
    await sim.connect()
    fake_mt5.install(sim, asyncio.get_running_loop())
    fake_mt5.set_magic(MAGIC)

    # Quotes must exist before the sidecar starts: `start_terminal` is where it measures the
    # server offset, and it measures it from the most recent tick.
    market = Market(sim, gold, int(time.time()) * 1000)
    for _ in range(3):
        market.tick()

    module = load_sidecar()
    port = free_port()
    ticks: list = []
    bars: list = []
    venue = BridgeVenue(host="127.0.0.1", port=port, token=TOKEN,
                        request_timeout_ms=3000, clock=FrozenClock(0),
                        on_tick=ticks.append, on_bar=bars.append)
    await venue.start()
    runner = SidecarRunner(module, port, symbols=symbols)
    runner.start()
    assert runner.sidecar.offset == fake_mt5._state.server_offset_seconds, (
        "the sidecar inferred a different server offset than the terminal reports; "
        "every timestamp it converts would be wrong by that difference"
    )
    try:
        yield venue, market, runner, ticks, bars
    finally:
        await asyncio.get_running_loop().run_in_executor(None, runner.stop)
        await venue.disconnect()
        sys.modules.pop("MetaTrader5", None)


# --- the file must actually run, not merely parse -----------------------------------


def test_sidecar_module_executes(gold):
    """CI previously only checked that the sidecar parsed. Parsing catches a syntax error
    and nothing else -- a NameError at import would have shipped."""
    sys.modules["MetaTrader5"] = fake_mt5
    try:
        module = load_sidecar()
        assert module.PROTOCOL_VERSION == 1
        assert callable(module.Sidecar.run)
    finally:
        sys.modules.pop("MetaTrader5", None)


# --- handshake -----------------------------------------------------------------------


async def test_sidecar_handshake_is_accepted(gold):
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        health = await venue.connect(wait_seconds=5)
        assert health.connected
        assert venue.capabilities.extra["impl"] == "python-sidecar"
        assert venue.capabilities.client_id_lookup, "retry safety depends on this"


async def test_sidecar_with_the_wrong_token_is_refused(gold):
    """The sidecar reads its token from the CLI; a mismatch must be rejected at the door,
    not after the first order."""
    sim = make_sim(gold)
    await sim.connect()
    fake_mt5.install(sim, asyncio.get_running_loop())
    Market(sim, gold, int(time.time()) * 1000).tick()
    module = load_sidecar()
    port = free_port()
    venue = BridgeVenue(host="127.0.0.1", port=port, token="the-real-token",
                        request_timeout_ms=1500, clock=FrozenClock(0))
    await venue.start()
    args = argparse.Namespace(host="127.0.0.1", port=port, token="wrong", magic=MAGIC,
                              symbols="XAUUSD", timeframes="M5", poll_ms=20,
                              log_level="CRITICAL")
    sc = module.Sidecar(args)
    sc.start_terminal()
    try:
        sc.connect()
        await asyncio.sleep(0.3)
        frames = await asyncio.get_running_loop().run_in_executor(None, sc.read_frames)
        assert any(f.get("t") == "bye" for f in frames), frames
        assert any("auth" in str(f.get("reason", "")) for f in frames if f.get("t") == "bye")
    finally:
        sc.close()
        await venue.disconnect()
        sys.modules.pop("MetaTrader5", None)


# --- state reads ---------------------------------------------------------------------


async def test_sidecar_specs_round_trip(gold):
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        specs = await venue.symbol_specs([gold.name])
        got = specs[gold.name]
        assert got.digits == gold.digits
        assert got.point == pytest.approx(gold.point)
        assert got.tick_value == pytest.approx(gold.tick_value)
        assert got.contract_size == pytest.approx(gold.contract_size)
        assert got.volume_step == pytest.approx(gold.volume_step)
        assert got.stops_level_points == gold.stops_level_points
        # The sidecar converts MT5's Sunday-based weekday to ATLAS's Monday-based one.
        # A round trip that lands back on the original value is the only way to know the
        # conversion is not off by a day in both directions.
        assert got.swap_rollover_3days == gold.swap_rollover_3days


async def test_sidecar_quote_and_account(gold):
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        quote = await venue.quote(gold.name)
        assert quote.bid < quote.ask
        assert quote.bid == pytest.approx(2400.0 - 0.10, abs=0.02)
        account = await venue.account()
        assert account.balance == pytest.approx(25_000.0)
        assert account.currency == "USD"


async def test_sidecar_bars_start_at_the_closed_bar(gold):
    """`copy_rates_from_pos(..., start=1, ...)` skips the forming bar. If that ever became
    0 the engine would receive a bar that is still changing -- the classic look-ahead."""
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        offset = fake_mt5._state.server_offset_seconds
        rows = [
            (300 * i + offset, 2400.0 + i, 2401.0 + i, 2399.0 + i, 2400.5 + i, 100 + i, 20)
            for i in range(5)
        ]
        fake_mt5.set_bars(gold.name, fake_mt5.TIMEFRAME_M5, rows)
        bars = await venue.bars(gold.name, __import__(
            "atlas.core.enums", fromlist=["Timeframe"]).Timeframe.M5, 3)
        assert len(bars) == 3
        # The newest returned bar must be the one BEFORE the forming bar (index 4).
        assert bars[-1].close == pytest.approx(2403.5)
        assert bars[-1].ts == 300 * 3 * 1000, "server seconds must be converted to UTC ms"


# --- orders ---------------------------------------------------------------------------


def market_order(gold, client_id: str = "sc-order-0001", volume: float = 0.10) -> OrderRequest:
    return OrderRequest(
        client_order_id=client_id, decision_id="d-1", symbol=gold.name, side=Side.BUY,
        order_type=OrderType.MARKET, volume=volume, stop_loss=2380.0, take_profit=2440.0,
        magic=MAGIC, comment=client_id,
    )


async def test_sidecar_market_order_fills_and_reports(gold):
    async with sidecar_harness(gold) as (venue, market, _, _, _):
        await venue.connect(wait_seconds=5)
        result = await venue.submit(market_order(gold))
        assert result.accepted, result.message
        # The fill lands on the next quote, exactly as it does everywhere else in ATLAS.
        market.tick()
        positions = await venue.positions()
        assert len(positions) == 1
        assert positions[0].volume == pytest.approx(0.10)
        assert positions[0].magic == MAGIC


async def test_sidecar_rejects_a_stop_inside_the_stops_level(gold):
    """A stop inside the stops level must be refused, never silently dropped.

    Both real terminal implementations pre-validate and answer with a protocol *error*
    (`ok:false`, `INVALID_STOPS`, retcode 10016) rather than an accepted reply carrying a
    rejection retcode -- which is the shape docs/PROTOCOL.md gives as its own example. On the
    ATLAS side that surfaces as `VenueError`, so this asserts the exception rather than an
    unaccepted result. `test_a_rejected_order_is_never_retried` covers what the engine
    actually sees.
    """
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        request = market_order(gold).model_copy(update={"stop_loss": 2399.95})
        with pytest.raises(VenueError) as excinfo:
            await venue.submit(request)
        assert excinfo.value.retcode == 10016
        assert "stops level" in str(excinfo.value).lower()


async def test_sidecar_rounds_volume_down_never_up(gold):
    """Rounding up exceeds the risk budget that produced the number."""
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        result = await venue.submit(market_order(gold, volume=0.1749))
        assert result.accepted, result.message
        sent = [r for r in fake_mt5._state.sent if r.get("symbol") == gold.name]
        assert sent, "the sidecar never reached the terminal"
        assert sent[-1]["volume"] == pytest.approx(0.17)


async def test_sidecar_refuses_a_volume_below_the_minimum(gold):
    """Below the minimum is zero, not the minimum: silently trading the minimum is a
    position nobody sized."""
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        with pytest.raises(VenueError) as excinfo:
            await venue.submit(market_order(gold, volume=0.004))
        assert excinfo.value.retcode == 10014


async def test_a_rejected_order_is_never_retried(gold):
    """The property that actually matters live, and that nothing covered before.

    A stop inside the stops level is an ordinary event -- price moves between the decision
    and the send. Both terminal implementations report it as a protocol error, so the router
    sees an exception rather than a rejection retcode. It must turn that into ONE clean
    refusal: retrying a deterministic rejection resends an order the broker has already
    refused, three times, for nothing.
    """
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        router = OrderRouter(venue, FrozenClock(0), policy=RetryPolicy(max_attempts=3))
        request = market_order(gold, client_id="sc-reject-001").model_copy(
            update={"stop_loss": 2399.95}
        )
        outcome = await router.submit(request)
        assert not outcome.accepted
        assert outcome.status is OrderStatus.REJECTED
        assert outcome.attempts == 1, "a venue rejection must not be retried"
        assert "10016" in str(outcome.error) or "stops" in str(outcome.error).lower()
        assert request.client_order_id not in router.in_flight


async def test_sidecar_find_by_comment_is_the_idempotency_lookup(gold):
    """Retry safety rests entirely on this answering 'here it is' or 'definitely not'."""
    async with sidecar_harness(gold) as (venue, market, _, _, _):
        await venue.connect(wait_seconds=5)
        assert await venue.find_by_client_id("sc-order-0001") is None
        await venue.submit(market_order(gold))
        market.tick()
        found = await venue.find_by_client_id("sc-order-0001")
        assert found is not None
        assert found.volume == pytest.approx(0.10)


async def test_sidecar_modify_and_close_a_position(gold):
    async with sidecar_harness(gold) as (venue, market, _, _, _):
        await venue.connect(wait_seconds=5)
        await venue.submit(market_order(gold))
        market.tick()
        ticket = (await venue.positions())[0].ticket

        modified = await venue.modify_position(ticket, stop_loss=2385.0, take_profit=2450.0)
        assert modified.accepted, modified.message
        after = (await venue.positions())[0]
        assert after.stop_loss == pytest.approx(2385.0)

        closed = await venue.close_position(ticket)
        assert closed.accepted, closed.message
        market.tick()
        assert await venue.positions() == []


async def test_sidecar_refuses_to_touch_a_foreign_magic(gold):
    """A manual trade in the same terminal must be safe from the bridge."""
    async with sidecar_harness(gold) as (venue, market, _, _, _):
        await venue.connect(wait_seconds=5)
        foreign = market_order(gold, client_id="not-ours-0001").model_copy(
            update={"magic": 999_999}
        )
        await venue.submit(foreign)
        market.tick()
        # It exists in the terminal, but the sidecar must not report or modify it.
        assert await venue.positions() == []
        ticket = market.sim.open_sim_positions()[0].ticket
        with pytest.raises(VenueError, match="different magic"):
            await venue.modify_position(ticket, stop_loss=2390.0)


async def test_sidecar_pending_order_round_trip(gold):
    async with sidecar_harness(gold) as (venue, _market, _, _, _):
        await venue.connect(wait_seconds=5)
        request = OrderRequest(
            client_order_id="sc-pend-0001", decision_id="d-2", symbol=gold.name,
            side=Side.BUY, order_type=OrderType.LIMIT, volume=0.10, price=2380.0,
            stop_loss=2360.0, take_profit=2420.0, magic=MAGIC, comment="sc-pend-0001",
        )
        result = await venue.submit(request)
        assert result.accepted, result.message
        # Resting, not filled -- and the retcode alone cannot say which. Real MT5 answers a
        # placed pending order with 10009 DONE, the same code a market fill gets.
        assert result.status is OrderStatus.WORKING, (
            "an accepted pending order must never be reported as FILLED: the engine would "
            "believe it holds a position, at a fill price of 0.0"
        )
        assert result.fill_price == 0.0 and result.filled_volume == 0.0
        pending = await venue.pending_orders()
        assert len(pending) == 1
        assert pending[0].price == pytest.approx(2380.0)

        cancelled = await venue.cancel_order(pending[0].ticket)
        assert cancelled.accepted
        assert await venue.pending_orders() == []


async def test_sidecar_unknown_op_is_an_error_not_a_crash(gold):
    """A newer ATLAS asking for something an older sidecar lacks must get a clean error and
    a session that survives it."""
    async with sidecar_harness(gold) as (venue, _market, runner, _, _):
        await venue.connect(wait_seconds=5)
        assert runner.sidecar.dispatch  # the dispatch table is what is under test
        with pytest.raises(Exception) as excinfo:
            runner.sidecar.dispatch("no_such_op", {})
        assert "UNKNOWN_OP" in str(excinfo.value) or "does not implement" in str(excinfo.value)
        # The session must still work afterwards.
        assert (await venue.account()).balance == pytest.approx(25_000.0)


async def test_sidecar_history_deals_pair_into_round_trips(gold):
    """The closing deal of a long is a SELL. Getting that inversion wrong silently flips
    the side of every closed trade in the journal."""
    async with sidecar_harness(gold) as (venue, market, _, _, _):
        await venue.connect(wait_seconds=5)
        await venue.submit(market_order(gold))
        market.tick()
        ticket = (await venue.positions())[0].ticket
        await venue.close_position(ticket)
        market.tick(2410.0)

        trades = await venue.closed_trades(0)
        assert len(trades) == 1
        assert str(trades[0].side) == "BUY", "the closing SELL deal must not flip the side"
        assert trades[0].entry_price == pytest.approx(2400.10, abs=0.2)


# --- streaming ------------------------------------------------------------------------


async def test_sidecar_streams_ticks(gold):
    async with sidecar_harness(gold) as (venue, market, _, ticks, _):
        await venue.connect(wait_seconds=5)
        market.tick(2405.0)
        for _ in range(60):
            if ticks:
                break
            await asyncio.sleep(0.05)
        assert ticks, "no tick reached the engine"
        assert ticks[-1].symbol == gold.name
        assert ticks[-1].bid < ticks[-1].ask


async def test_sidecar_emits_a_bar_only_after_it_closes(gold):
    """A bar must be published when the NEXT bucket opens, never while it is forming."""
    async with sidecar_harness(gold) as (venue, _market, _, _, bars):
        await venue.connect(wait_seconds=5)
        offset = fake_mt5._state.server_offset_seconds
        rows = [
            (300 * i + offset, 2400.0 + i, 2401.0 + i, 2399.0 + i, 2400.5 + i, 100 + i, 20)
            for i in range(3)
        ]
        fake_mt5.set_bars(gold.name, fake_mt5.TIMEFRAME_M5, rows)
        await asyncio.sleep(0.2)
        assert not bars, "a bar was published before any new bucket opened"

        # A new forming bar appears: the one before it has now closed.
        rows.append((300 * 3 + offset, 2403.0, 2404.0, 2402.0, 2403.5, 103, 20))
        fake_mt5.set_bars(gold.name, fake_mt5.TIMEFRAME_M5, rows)
        for _ in range(80):
            if bars:
                break
            await asyncio.sleep(0.05)
        assert bars, "no closed bar reached the engine"
        published = bars[-1]
        assert published.ts == 300 * 2 * 1000, "the CLOSED bar must be published, not the new one"
        assert published.close == pytest.approx(2402.5)
        assert published.volume == 102, "the volume field must not collide with the version"


# --- framing --------------------------------------------------------------------------


def test_sidecar_refuses_to_send_an_oversized_frame(gold):
    """The frame cap exists so a malformed payload cannot wedge the peer's parser."""
    sys.modules["MetaTrader5"] = fake_mt5
    try:
        module = load_sidecar()
        args = argparse.Namespace(host="127.0.0.1", port=1, token="", magic=MAGIC,
                                  symbols="XAUUSD", timeframes="M5", poll_ms=20,
                                  log_level="CRITICAL")
        sc = module.Sidecar(args)
        left, right = socket_module.socketpair()
        sc.sock = left
        sc.send({"t": "junk", "payload": "x" * (module.MAX_FRAME_BYTES + 10)})
        right.settimeout(0.1)
        with pytest.raises((TimeoutError, BlockingIOError, OSError)):
            right.recv(1)
        left.close()
        right.close()
    finally:
        sys.modules.pop("MetaTrader5", None)
