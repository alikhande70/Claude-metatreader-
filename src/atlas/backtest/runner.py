"""Backtest runner.

Wires the shared engine (ADR-003) to a replay data source, the simulated venue and the
precomputed feature provider. There is no backtest-specific decision logic anywhere -- the
strategy, risk engine, trade manager and order router are the same objects a live run uses.

The result carries the trades, the equity curve, the decision records and the engine's own
statistics, so the report can answer "why did it not trade more?" as readily as
"how much did it make?".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from atlas.analytics.metrics import PerformanceMetrics, compute_metrics
from atlas.bus.events import EventKind
from atlas.bus.journal import Journal
from atlas.core.clock import SimulatedClock
from atlas.core.enums import RunMode, Timeframe
from atlas.core.errors import ConfigError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Bar
from atlas.core.trading import Trade
from atlas.data.aggregator import MultiTimeframeAggregator
from atlas.data.calendar import TradingCalendar
from atlas.data.series import BarSeries
from atlas.data.source import ReplayDataSource
from atlas.features.frame import FeatureConfig
from atlas.risk.config import RiskConfig
from atlas.risk.engine import RiskEngine
from atlas.risk.state import RiskState
from atlas.runtime.engine import EngineConfig, EngineStats, TradingEngine
from atlas.runtime.features import (
    FeatureProvider,
    IncrementalFeatureProvider,
    PrecomputedFeatureProvider,
)
from atlas.strategy.base import Strategy
from atlas.venues.sim.costs import CostModel
from atlas.venues.sim.venue import SimConfig, SimulatedVenue


@dataclass(slots=True)
class BacktestConfig:
    base_tf: Timeframe = Timeframe.M5
    warmup_bars: int = 600
    starting_balance: float = 10_000.0
    leverage: int = 100
    seed: int = 1234
    allowed_sessions: tuple[str, ...] = ()
    news_policy: str = "warn"
    server_offset_seconds: int = 3 * 3600
    magic: int = 20260823
    journal_no_setup: bool = True
    #: Use the rolling provider instead of the precomputed one. Slower, and used by the
    #: equivalence test that proves the two produce identical results.
    incremental_features: bool = False
    feature_window: int = 1500
    #: Recorded on every result so a report can never present synthetic data as evidence.
    data_source_label: str = "unknown"
    synthetic: bool = False


@dataclass(slots=True)
class BacktestResult:
    trades: list[Trade]
    metrics: PerformanceMetrics
    equity_curve: list[tuple[int, float, float]]
    stats: EngineStats
    rejections: list[tuple[int, str, str]]
    config: BacktestConfig
    strategy_params: dict[str, dict] = field(default_factory=dict)
    journal_dir: Path | None = None
    synthetic: bool = False
    label: str = ""

    @property
    def approved_for_live(self) -> bool:
        """Never true for synthetic data, whatever the numbers say (ADR-006)."""
        return False if self.synthetic else self.metrics.trades > 0

    def summary(self) -> str:
        from atlas.analytics.metrics import summarise

        head = f"[{self.label or 'backtest'}] source={self.config.data_source_label}"
        if self.synthetic:
            head += "  ** SYNTHETIC DATA: machinery validation only, not evidence of an edge **"
        return head + "\n" + summarise(self.metrics)


def _build_series(
    bars: Mapping[str, Sequence[Bar]], base_tf: Timeframe,
    timeframes: dict[str, tuple[Timeframe, ...]],
) -> dict[tuple[str, Timeframe], BarSeries]:
    out: dict[tuple[str, Timeframe], BarSeries] = {}
    for symbol, bl in bars.items():
        tfs = tuple(dict.fromkeys([base_tf, *timeframes[symbol]]))
        agg = MultiTimeframeAggregator(symbol, base_tf, tfs)
        series = {tf: BarSeries(symbol, tf, capacity=max(64, len(bl))) for tf in tfs}
        for bar in bl:
            for tf, closed in agg.push(bar).items():
                series[tf].append(closed)
        for tf, s in series.items():
            out[(symbol, tf)] = s
    return out


async def run_backtest(
    *,
    bars: Mapping[str, Sequence[Bar]],
    specs: dict[str, SymbolSpec],
    strategies: dict[str, Strategy],
    journal_dir: Path | str,
    config: BacktestConfig | None = None,
    risk_config: RiskConfig | None = None,
    feature_config: FeatureConfig | None = None,
    costs: CostModel | None = None,
    label: str = "",
) -> BacktestResult:
    cfg = config or BacktestConfig()
    fcfg = feature_config or FeatureConfig()
    rcfg = risk_config or RiskConfig()

    missing = set(strategies) - set(bars)
    if missing:
        raise ConfigError(f"strategies configured for symbols with no data: {sorted(missing)}")
    for sym in bars:
        if sym not in specs:
            raise ConfigError(f"no symbol spec for {sym}")

    timeframes = {sym: strategies[sym].timeframes for sym in strategies}
    all_tfs = sorted(
        {tf for tfs in timeframes.values() for tf in tfs},
        key=lambda t: t.seconds,
    )

    source = ReplayDataSource(
        bars, specs, base_tf=cfg.base_tf, timeframes=all_tfs, warmup_bars=cfg.warmup_bars
    )
    venue = SimulatedVenue(
        specs,
        config=SimConfig(starting_balance=cfg.starting_balance, leverage=cfg.leverage,
                         seed=cfg.seed),
        costs=costs or CostModel(),
    )

    warm_series = _build_series(
        {s: b[: cfg.warmup_bars] for s, b in bars.items()}, cfg.base_tf, timeframes
    )
    features: FeatureProvider
    if cfg.incremental_features:
        inc = IncrementalFeatureProvider(specs, timeframes, fcfg, window=cfg.feature_window)
        for (sym, tf), series in warm_series.items():
            inc.seed(sym, tf, series)
        features = inc
    else:
        full_series = _build_series(bars, cfg.base_tf, timeframes)
        features = PrecomputedFeatureProvider(full_series, specs, fcfg)

    from atlas.data.calendar import ServerClockMapping

    calendar = TradingCalendar(
        server=ServerClockMapping(offset_seconds=cfg.server_offset_seconds, measured_at_ms=1)
    )
    clock = SimulatedClock(bars[next(iter(bars))][0].ts)
    journal = Journal(journal_dir, run_id=label or "backtest", clock=clock, batch_size=2000)
    risk = RiskEngine(rcfg, RiskState())

    engine = TradingEngine(
        source=source, venue=venue, features=features, strategies=strategies, risk=risk,
        calendar=calendar, journal=journal, clock=clock,
        config=EngineConfig(
            mode=RunMode.BACKTEST, magic=cfg.magic,
            allowed_sessions=cfg.allowed_sessions, news_policy=cfg.news_policy,
            journal_no_setup=cfg.journal_no_setup,
        ),
    )
    try:
        stats = await engine.run()
    finally:
        journal.close()

    metrics = compute_metrics(
        venue.trades, starting_equity=cfg.starting_balance,
        equity_curve=venue.equity_curve,
    )
    return BacktestResult(
        trades=list(venue.trades), metrics=metrics, equity_curve=list(venue.equity_curve),
        stats=stats, rejections=list(venue.rejections), config=cfg,
        strategy_params={s: st.parameters() for s, st in strategies.items()},
        journal_dir=Path(journal_dir), synthetic=cfg.synthetic, label=label,
    )


def load_decisions(journal_dir: Path | str) -> list[dict]:
    """Read decision records back out of a run's journal."""
    j = Journal(journal_dir, mirror_sqlite=True)
    try:
        out: list[dict] = []
        seq = 0
        while True:
            batch = j.read(seq, kinds=[EventKind.DECISION], limit=5000)
            if not batch:
                break
            out.extend(e.payload["record"] for e in batch if "record" in e.payload)
            seq = batch[-1].seq
        return out
    finally:
        j.close()
