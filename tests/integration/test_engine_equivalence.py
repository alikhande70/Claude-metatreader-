"""End-to-end engine tests.

The headline test here is ``test_precomputed_and_incremental_paths_agree``. The backtest uses
a fast path -- features computed once over the whole history, then indexed -- while live uses
a rolling recomputation over a bounded window. If those two ever diverge, the research results
are not describing the system that will actually trade, and every conclusion drawn from a
backtest becomes unreliable in a way that is invisible.

So the test runs the same data, the same strategy and the same seed through both providers and
requires the resulting trade lists to be identical, field by field.
"""

from __future__ import annotations

import pytest

from atlas.backtest.runner import BacktestConfig, run_backtest
from atlas.core.enums import DecisionOutcome, HaltReason, RunMode
from atlas.data.synthetic import SyntheticConfig, generate
from atlas.risk.config import RiskConfig
from atlas.strategy.registry import build
from atlas.venues.sim.costs import CostModel

LADDER = {"htf": "H1", "mtf": "M15", "ltf": "M5"}


@pytest.fixture(scope="module")
def bars():
    return generate(SyntheticConfig(bars=14_000, seed=4242))


@pytest.fixture(scope="module")
def short_bars():
    """A smaller series for the equivalence test.

    The rolling provider recomputes features on every trigger bar -- ~15 ms each -- which is
    irrelevant live (one evaluation per five minutes) but makes a long equivalence run take
    minutes. The property being tested does not need length, only enough bars for the
    structural state memory to turn over several times.
    """
    return generate(SyntheticConfig(bars=4_200, seed=4242))


async def _run(tmp_path, bars, gold, *, incremental=False, label="t", warmup=1200,
               window=1500, **cfg_kw):
    return await run_backtest(
        bars={"XAUUSD": bars}, specs={"XAUUSD": gold},
        strategies={"XAUUSD": build("SM1", LADDER)},
        journal_dir=tmp_path / label,
        config=BacktestConfig(
            warmup_bars=warmup, synthetic=True, data_source_label="synthetic",
            incremental_features=incremental, feature_window=window,
            journal_no_setup=False, **cfg_kw,
        ),
        risk_config=RiskConfig(risk_per_trade_pct=0.5),
        label=label,
    )


@pytest.mark.slow
async def test_precomputed_and_incremental_paths_agree(tmp_path, short_bars, gold):
    """The research path and the live path must be the same system.

    Two real defects were found by this test and would have been invisible without it:
    unbounded memory in the structural state machine (fixed by ADR-019), and an emission
    order that let the trigger timeframe be evaluated against a stale higher-timeframe frame.
    Both produced small, plausible-looking differences in the trade list.
    """
    fast = await _run(tmp_path, short_bars, gold, incremental=False, label="fast",
                      warmup=1300, window=1100)
    slow = await _run(tmp_path, short_bars, gold, incremental=True, label="slow",
                      warmup=1300, window=1100)

    assert fast.stats.decisions == slow.stats.decisions
    assert fast.stats.decision_reasons == slow.stats.decision_reasons
    assert len(fast.trades) == len(slow.trades), (
        f"fast path produced {len(fast.trades)} trades, rolling path {len(slow.trades)}"
    )
    for a, b in zip(fast.trades, slow.trades, strict=True):
        assert a.entry_time == b.entry_time
        assert a.entry_price == pytest.approx(b.entry_price)
        assert a.exit_time == b.exit_time
        assert a.exit_price == pytest.approx(b.exit_price)
        assert a.volume == pytest.approx(b.volume)
        assert a.exit_reason == b.exit_reason
        assert a.r_multiple == pytest.approx(b.r_multiple)
    assert fast.metrics.total_r == pytest.approx(slow.metrics.total_r)


async def test_backtest_is_deterministic(tmp_path, bars, gold):
    a = await _run(tmp_path, bars, gold, label="a")
    b = await _run(tmp_path, bars, gold, label="b")
    assert [t.trade_id for t in a.trades] == [t.trade_id for t in b.trades]
    assert a.metrics.total_r == pytest.approx(b.metrics.total_r)
    assert a.stats.decision_reasons == b.stats.decision_reasons


async def test_every_trade_links_back_to_a_decision(tmp_path, bars, gold):
    """The audit chain: trade -> decision -> the gates and evidence that produced it."""
    from atlas.backtest.runner import load_decisions

    res = await _run(tmp_path, bars, gold, label="audit")
    decisions = load_decisions(tmp_path / "audit")
    by_id = {d["decision_id"]: d for d in decisions}
    assert decisions, "the journal must contain decision records"
    assert res.trades, "need at least one trade to check the chain"
    for t in res.trades:
        assert t.decision_id, f"trade {t.trade_id} has no originating decision"
        d = by_id.get(t.decision_id)
        assert d is not None, f"decision {t.decision_id} is not in the journal"
        assert d["outcome"] == DecisionOutcome.SIGNAL.value
        assert d["proposal"] is not None
        assert d["gates"], "the decision must record the gates it passed"
        assert d["evidence"], "a signal must record its evidence"


async def test_no_setup_decisions_are_journalled_with_reasons(tmp_path, bars, gold):
    from atlas.backtest.runner import load_decisions

    await _run(tmp_path, bars, gold, label="reasons")
    decisions = load_decisions(tmp_path / "reasons")
    reasons = {d["reason_code"] for d in decisions}
    assert len(reasons) >= 4, f"expected a spread of stand-aside reasons, got {reasons}"
    assert all(d["reason_code"] for d in decisions), "no decision may be anonymous"


async def test_costs_reduce_performance_monotonically(tmp_path, bars, gold):
    """A sanity check on the cost model: more cost cannot mean more profit."""
    results = []
    for mult, label in ((1.0, "c1"), (2.0, "c2"), (3.0, "c3")):
        r = await run_backtest(
            bars={"XAUUSD": bars}, specs={"XAUUSD": gold},
            strategies={"XAUUSD": build("SM1", LADDER)},
            journal_dir=tmp_path / label,
            config=BacktestConfig(warmup_bars=1200, synthetic=True,
                                  data_source_label="synthetic", journal_no_setup=False),
            risk_config=RiskConfig(risk_per_trade_pct=0.5),
            costs=CostModel(
                commission_per_lot_per_side=3.5 * mult,
                entry_slippage_points=2.0 * mult, stop_slippage_points=6.0 * mult,
            ),
            label=label,
        )
        results.append(r.metrics.net_profit)
    assert results[0] >= results[1] >= results[2], (
        f"net profit must not increase with costs: {results}"
    )


async def test_kill_switch_stops_trading_mid_backtest(tmp_path, bars, gold):
    """A tight drawdown limit must halt the run and suppress every later signal."""
    res = await run_backtest(
        bars={"XAUUSD": bars}, specs={"XAUUSD": gold},
        strategies={"XAUUSD": build("SM1", LADDER)},
        journal_dir=tmp_path / "halt",
        config=BacktestConfig(warmup_bars=1200, synthetic=True,
                              data_source_label="synthetic", journal_no_setup=False),
        risk_config=RiskConfig(risk_per_trade_pct=2.0, max_total_risk_pct=4.0,
                               max_group_risk_pct=4.0, daily_loss_limit_pct=0.4,
                               total_drawdown_limit_pct=0.6),
        label="halt",
    )
    assert res.stats.halts > 0, "such tight limits must trip"
    assert "HALTED" in res.stats.risk_rejections, "post-halt signals must be recorded as suppressed"


async def test_engine_reports_no_phantom_reconciliation_divergences(tmp_path, bars, gold):
    """Regression: closing a position ourselves used to look like a divergence on the same
    bar, training the operator to ignore the alert that matters."""
    res = await _run(tmp_path, bars, gold, label="recon")
    assert res.stats.reconcile_divergences == 0


async def test_synthetic_results_can_never_be_approved(tmp_path, bars, gold):
    res = await _run(tmp_path, bars, gold, label="approve")
    assert res.synthetic
    assert res.approved_for_live is False
    assert "SYNTHETIC" in res.summary()


async def test_config_errors_are_caught_early(tmp_path, bars, gold):
    from atlas.core.errors import ConfigError

    with pytest.raises(ConfigError, match="no data"):
        await run_backtest(
            bars={"XAUUSD": bars}, specs={"XAUUSD": gold},
            strategies={"EURUSD": build("SM1", LADDER)},
            journal_dir=tmp_path / "bad",
        )
    with pytest.raises(ConfigError, match="no symbol spec"):
        await run_backtest(
            bars={"XAUUSD": bars, "EURUSD": bars}, specs={"XAUUSD": gold},
            strategies={"XAUUSD": build("SM1", LADDER)},
            journal_dir=tmp_path / "bad2",
        )


async def test_engine_state_and_run_events_are_journalled(tmp_path, bars, gold):
    from atlas.bus.events import EventKind
    from atlas.bus.journal import Journal

    await _run(tmp_path, bars, gold, label="events")
    j = Journal(tmp_path / "events", mirror_sqlite=True)
    try:
        started = j.latest(EventKind.RUN_STARTED)
        stopped = j.latest(EventKind.RUN_STOPPED)
        assert started is not None and stopped is not None
        assert started.payload["mode"] == RunMode.BACKTEST.value
        assert started.payload["parameters"]["XAUUSD"]["rr_target"] == 2.0
        assert stopped.payload["stats"]["decisions"] > 0
        assert j.latest(EventKind.EQUITY_POINT) is not None
        assert j.latest(EventKind.RECONCILE_OK) is not None
    finally:
        j.close()


def test_halt_reason_enum_is_exhaustive_for_the_engine():
    """Every halt the engine can raise must be a declared reason, not an ad-hoc string."""
    from atlas.risk.state import AUTO_CLEARING, HEALTH_HALTS

    assert set(HaltReason) >= AUTO_CLEARING
    assert set(HaltReason) >= HEALTH_HALTS
    assert not (AUTO_CLEARING & {HaltReason.TOTAL_DRAWDOWN_LIMIT})
