"""Live and paper runtime.

Assembles the same :class:`TradingEngine` a backtest uses (ADR-003) around a live market data
source and either the MT5 bridge or the simulator. Everything mode-specific lives here, at
the edge, and nothing below this file knows which mode it is in.

Startup is deliberately ordered so that the system cannot begin trading on a state it has not
verified:

1. Bind the bridge and wait for a terminal.
2. Read symbol specifications **from the broker** and compare them with the configured ones.
   A divergence is loud: contract size or stops level changing silently invalidates the risk
   maths on every open position.
3. Measure the server clock offset. The rollover window follows the broker's midnight, not
   UTC's.
4. Pull warm-up history and check it against the broker's own higher-timeframe bars.
5. Adopt any position already open under our magic, flagged as reconstructed.
6. Only then start the decision loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from atlas.bus.events import EventKind
from atlas.bus.journal import Journal
from atlas.core.clock import SystemClock
from atlas.core.enums import EngineState, RunMode, Timeframe
from atlas.core.errors import ConfigError, DataError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar, Quote
from atlas.data.calendar import (
    DEFAULT_SESSIONS,
    NewsCalendar,
    ServerClockMapping,
    TradingCalendar,
)
from atlas.data.series import BarSeries
from atlas.data.source import MarketDataSource, MarketUpdate, UpdateKind
from atlas.execution.venue import ExecutionVenue
from atlas.risk.engine import RiskEngine
from atlas.risk.state import RiskState
from atlas.runtime.engine import EngineConfig, TradingEngine
from atlas.runtime.features import IncrementalFeatureProvider
from atlas.strategy.registry import build
from atlas.venues.mt5.bridge import BridgeVenue


class LiveDataSource(MarketDataSource):
    """Turns pushed bridge callbacks into the same ordered update stream a replay produces.

    Ordering matters as much here as in replay: when several timeframes close at the same
    instant the trigger timeframe must be delivered **last**, so the strategy never evaluates
    against a stale higher-timeframe frame. The bridge emits per-timeframe bars, so this
    source buffers bars that share a timestamp and releases them longest-first.
    """

    def __init__(
        self,
        symbols: list[str],
        specs: dict[str, SymbolSpec],
        warmup: dict[tuple[str, Timeframe], BarSeries],
        *,
        queue_size: int = 10_000,
    ) -> None:
        self._symbols = tuple(symbols)
        self._specs = specs
        self._warmup = warmup
        self._queue: asyncio.Queue[MarketUpdate] = asyncio.Queue(maxsize=queue_size)
        self._pending_bars: dict[int, list[MarketUpdate]] = {}
        self.dropped = 0

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def spec(self, symbol: str) -> SymbolSpec:
        try:
            return self._specs[symbol]
        except KeyError as exc:
            raise DataError(f"no symbol spec for {symbol}") from exc

    async def warmup(self) -> dict[tuple[str, Timeframe], BarSeries]:
        return self._warmup

    def push_quote(self, quote: Quote) -> None:
        self._offer(MarketUpdate(kind=UpdateKind.QUOTE, symbol=quote.symbol, ts=quote.ts,
                                 quote=quote))

    def push_bar(self, bar: Bar) -> None:
        self._offer(MarketUpdate(kind=UpdateKind.BAR, symbol=bar.symbol, ts=bar.ts_close,
                                 bar=bar, tf=bar.tf))

    def _offer(self, update: MarketUpdate) -> None:
        try:
            self._queue.put_nowait(update)
        except asyncio.QueueFull:
            # Dropping the oldest quote is correct: quotes are a sampled view and a stale one
            # is worth less than a fresh one. Bars are never dropped -- losing a bar close
            # loses a decision.
            self.dropped += 1
            if update.kind is UpdateKind.BAR:
                raise
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(update)

    async def stream(self) -> AsyncIterator[MarketUpdate]:
        while True:
            update = await self._queue.get()
            if update.kind is not UpdateKind.BAR:
                yield update
                continue
            # Collect every bar closing at the same instant, then release longest-first.
            batch = [update]
            await asyncio.sleep(0)  # let sibling bar callbacks land
            while not self._queue.empty():
                nxt = self._queue.get_nowait()
                if nxt.kind is UpdateKind.BAR and nxt.ts == update.ts:
                    batch.append(nxt)
                else:
                    batch.append(nxt)
                    break
            bars = [u for u in batch if u.kind is UpdateKind.BAR and u.ts == update.ts]
            others = [u for u in batch if u not in bars]
            for u in sorted(bars, key=lambda x: x.tf.seconds if x.tf else 0, reverse=True):
                yield u
            for u in others:
                yield u


async def build_live_engine(cfg, *, paper: bool = False) -> tuple[TradingEngine, ExecutionVenue]:
    if not cfg.symbols:
        raise ConfigError("no symbols configured")
    symbols = [s.symbol for s in cfg.symbols]
    strategies = {s.symbol: build(s.strategy, s.parameters) for s in cfg.symbols}
    timeframes = {sym: st.timeframes for sym, st in strategies.items()}
    run_dir = Path(cfg.journal_dir) / ("paper" if paper else "live")
    clock = SystemClock()
    journal = Journal(run_dir, run_id=cfg.name, clock=clock, batch_size=1)

    source_holder: dict[str, LiveDataSource] = {}

    def on_tick(q: Quote) -> None:
        src = source_holder.get("src")
        if src is not None:
            src.push_quote(q)

    def on_bar(b: Bar) -> None:
        src = source_holder.get("src")
        if src is not None:
            src.push_bar(b)

    def on_txn(msg: dict) -> None:
        # Transactions are hints, not state (docs/PROTOCOL.md §3). They are journalled so the
        # timeline is complete, and the authoritative answer still comes from reconciliation.
        journal.append("venue.transaction", msg, stream=str(msg.get("sym", "system")))

    def on_log(level: str, message: str) -> None:
        journal.append(EventKind.VENUE_ERROR if level == "ERROR" else EventKind.HEARTBEAT,
                       {"source": "terminal", "level": level, "message": message})

    venue: ExecutionVenue
    if cfg.venue.kind == "sim" and not paper:
        raise ConfigError(
            "venue.kind is 'sim' with no market feed. Offline paper trading is just a "
            "backtest -- use 'atlas backtest'. For paper trading against live prices, point "
            "venue.kind at the bridge and pass --paper."
        )
    else:
        bridge = BridgeVenue(
            command_endpoint=cfg.venue.command_endpoint,
            token=cfg.venue.resolved_token(),
            request_timeout_ms=cfg.venue.request_timeout_ms,
            on_tick=on_tick, on_bar=on_bar, on_transaction=on_txn, on_log=on_log,
            clock=clock,
        )
        await bridge.connect(wait_seconds=120)
        if paper:
            # Real quotes, pretend money. The simulator prices against the broker's own
            # specification, so a paper session rehearses the live one rather than a
            # different system with the same name.
            from atlas.venues.paper import PaperVenue
            from atlas.venues.sim.venue import SimConfig, SimulatedVenue

            venue = PaperVenue(
                bridge,
                SimulatedVenue({}, config=SimConfig(
                    starting_balance=cfg.venue.starting_balance,
                    leverage=cfg.venue.leverage,
                )),
            )
            await venue.connect()
        else:
            venue = bridge
        specs = await venue.symbol_specs(symbols)
        missing = set(symbols) - set(specs)
        if missing:
            raise ConfigError(
                f"the broker does not offer {sorted(missing)}. Symbol names carry suffixes "
                f"(.m, _i, .pro) that differ between brokers -- run 'atlas bridge-check' to "
                f"see what this account actually has."
            )
        _compare_specs(cfg, specs, journal)

    health = await venue.health()
    offset = health.server_offset_seconds or 0
    server = ServerClockMapping(offset_seconds=offset, measured_at_ms=clock.now_ms(), samples=1)
    journal.append(EventKind.VENUE_CONNECTED, {
        "detail": health.detail, "server_offset_hours": server.offset_hours,
        "trade_allowed": health.trade_allowed, "paper": paper,
    })

    news = (NewsCalendar.from_file(cfg.news_file) if cfg.news_file
            else NewsCalendar((), available=False))
    calendar = TradingCalendar(DEFAULT_SESSIONS, server=server, news=news)

    warm = await _warmup_series(venue, symbols, timeframes, cfg)
    source = LiveDataSource(symbols, specs, warm)
    source_holder["src"] = source

    features = IncrementalFeatureProvider(specs, timeframes, cfg.feature_config(),
                                          window=cfg.feature_window)
    for (sym, tf), series in warm.items():
        features.seed(sym, tf, series)

    state_path = Path(cfg.state_dir) / f"{cfg.name}_risk.json"
    risk = RiskEngine(cfg.risk, RiskState.load(state_path, clock.now_ms()))
    if risk.state.halted:
        journal.append(EventKind.KILL_SWITCH, {
            "reason": risk.state.halt_reason, "detail": risk.state.halt_detail,
            "note": "restored from persisted state; a halt survives a restart by design",
        })

    engine = TradingEngine(
        source=source, venue=venue, features=features, strategies=strategies, risk=risk,
        calendar=calendar, journal=journal, clock=clock,
        config=EngineConfig(
            mode=RunMode.PAPER if paper else RunMode.LIVE, magic=cfg.magic,
            allowed_sessions=tuple(cfg.allowed_sessions), news_policy=cfg.news_policy,
            journal_no_setup=True,
        ),
    )
    engine.state_path = state_path  # type: ignore[attr-defined]
    return engine, venue


def _compare_specs(cfg, broker_specs: dict[str, SymbolSpec], journal: Journal) -> None:
    """Compare broker specs with the configured ones and journal every difference.

    Not fatal -- the broker is authoritative and its values are the ones used. But a silent
    difference means every backtest run against the configured spec sized positions
    differently from what will happen live, and that must be visible.
    """
    for entry in cfg.symbols:
        configured = entry.spec
        actual = broker_specs.get(entry.symbol)
        if configured is None or actual is None:
            continue
        diffs = {}
        for field in ("digits", "point", "tick_size", "tick_value", "contract_size",
                      "volume_min", "volume_step", "volume_max", "stops_level_points"):
            a, b = getattr(configured, field), getattr(actual, field)
            if a != b:
                diffs[field] = {"configured": a, "broker": b}
        if diffs:
            journal.append(EventKind.SPEC_CHANGED, {
                "symbol": entry.symbol, "differences": diffs,
                "detail": "the broker's values are authoritative and are being used; any "
                          "backtest run against the configured values sized differently",
            }, stream=entry.symbol)


async def _warmup_series(
    venue: ExecutionVenue, symbols: list[str], timeframes: dict[str, tuple[Timeframe, ...]],
    cfg,
) -> dict[tuple[str, Timeframe], BarSeries]:
    """Fetch base-timeframe history and aggregate every higher timeframe from it (ADR-014).

    The broker's own higher-timeframe bars are fetched too, but only to be **compared**. If
    they disagree with ours the bar boundaries differ -- usually a server-time problem -- and
    that is worth knowing before a multi-timeframe strategy starts reasoning about them.
    """
    from atlas.data.aggregator import MultiTimeframeAggregator, compare_series

    out: dict[tuple[str, Timeframe], BarSeries] = {}
    fetch = getattr(venue, "bars", None)
    for symbol in symbols:
        tfs = tuple(dict.fromkeys([cfg.base_timeframe, *timeframes[symbol]]))
        base_bars: list[Bar] = []
        if fetch is not None:
            base_bars = await fetch(symbol, cfg.base_timeframe, cfg.warmup_bars)
        if not base_bars:
            raise DataError(
                f"no warm-up history for {symbol} at {cfg.base_timeframe}. The terminal may "
                f"still be downloading it -- open the chart once and retry."
            )
        agg = MultiTimeframeAggregator(symbol, cfg.base_timeframe, tfs)
        series = {tf: BarSeries(symbol, tf, capacity=max(64, len(base_bars))) for tf in tfs}
        for bar in base_bars:
            for tf, closed in agg.push(bar).items():
                series[tf].append(closed)
        for tf, s in series.items():
            out[(symbol, tf)] = s
        if fetch is not None:
            for tf in tfs:
                if tf is cfg.base_timeframe:
                    continue
                theirs = await fetch(symbol, tf, min(200, len(series[tf])))
                if not theirs:
                    continue
                diffs = compare_series(series[tf].bars()[-len(theirs):], theirs)
                if diffs:
                    # Reported, not fatal: a handful of differences at the seam of the
                    # fetched window is normal; a systematic disagreement is not.
                    print(f"[warmup] {symbol} {tf}: {len(diffs)} bars differ from the "
                          f"broker's own; first: {diffs[0]}")
    return out


async def run_live(cfg, *, paper: bool = False) -> None:
    engine, venue = await build_live_engine(cfg, paper=paper)
    state_path = getattr(engine, "state_path", None)

    async def persist_risk_state() -> None:
        """Persist the kill switch continuously.

        A halt that only exists in memory is not a kill switch: the process that trips it is
        exactly the process most likely to die next.
        """
        while True:
            await asyncio.sleep(5)
            if state_path is not None:
                engine.risk.state.save(state_path)

    keeper = asyncio.create_task(persist_risk_state())
    try:
        await engine.run()
    except KeyboardInterrupt:
        engine.command_halt("interrupted by the operator")
    finally:
        keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keeper
        if state_path is not None:
            engine.risk.state.save(state_path)
        engine.journal.append(EventKind.ENGINE_STATE, {"state": EngineState.STOPPED})
        engine.journal.close()
        await venue.disconnect()
