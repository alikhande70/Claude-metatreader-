"""Projection store: folding, thread safety, and honest unknowns."""

from __future__ import annotations

import threading

import pytest

from atlas.bus.events import EventKind
from atlas.bus.journal import Journal
from atlas.core.enums import ExitReason, Side
from atlas.core.trading import Trade
from atlas.store.projections import ProjectionStore, Stamped, list_runs


def make_trade(i: int) -> Trade:
    return Trade(
        trade_id=f"T{i}", decision_id=f"d{i}", symbol="XAUUSD", side=Side.BUY, volume=0.1,
        entry_price=2600.0, entry_time=i * 1000, exit_price=2605.0, exit_time=i * 1000 + 500,
        initial_stop=2595.0, risk_money=50.0, gross_profit=50.0,
        exit_reason=ExitReason.TAKE_PROFIT, point=0.01,
    )


def seed(tmp_path, trades: int = 20, equity_points: int = 100):
    with Journal(tmp_path, run_id="r", batch_size=500) as j:
        j.append(EventKind.RUN_STARTED, {"mode": "BACKTEST", "symbols": ["XAUUSD"]})
        j.append(EventKind.ENGINE_STATE, {"state": "RUNNING"})
        for i in range(equity_points):
            j.append(EventKind.EQUITY_POINT, {"equity": 10_000 + i, "balance": 10_000},
                     ts=i * 1000)
        for i in range(trades):
            j.append(EventKind.TRADE_RECORDED, {"trade": make_trade(i)}, stream="XAUUSD")
            j.append(EventKind.DECISION, {"record": {
                "decision_id": f"d{i}", "ts": i * 1000, "symbol": "XAUUSD", "strategy": "SM1",
                "outcome": "SIGNAL", "reason_code": "SETUP_CONFIRMED", "conviction": 0.6,
                "gates": [], "evidence": [], "features": {}, "regime": "TREND_UP",
            }})
    return tmp_path


def test_folds_events_into_projections(tmp_path):
    seed(tmp_path)
    store = ProjectionStore(tmp_path)
    store.refresh()
    assert len(store.state.trades) == 20
    assert len(store.state.decisions) == 20
    assert store.state.engine_state.value == "RUNNING"
    assert store.state.mode.value == "BACKTEST"
    store.close()


def test_refresh_is_idempotent(tmp_path):
    seed(tmp_path)
    store = ProjectionStore(tmp_path)
    for _ in range(5):
        store.refresh()
    assert len(store.state.trades) == 20
    store.close()


def test_concurrent_refresh_does_not_duplicate(tmp_path):
    """Regression for a real bug.

    FastAPI runs synchronous endpoints in a threadpool, so a dashboard polling several
    endpoints at once calls refresh() from several threads simultaneously. Without a lock they
    all read the same last_seq, all fetch the same batch, and all fold it -- the UI showed 61
    trades from a journal containing 41, while the journal itself was perfectly clean.
    """
    seed(tmp_path, trades=40)
    store = ProjectionStore(tmp_path)
    threads = [threading.Thread(target=lambda: [store.refresh() for _ in range(5)])
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [t.trade_id for t in store.state.trades]
    assert len(ids) == len(set(ids)) == 40
    store.close()


def test_apply_is_idempotent_for_replayed_events(tmp_path):
    """A live journal listener and a poll-driven refresh can deliver the same event."""
    seed(tmp_path, trades=3)
    store = ProjectionStore(tmp_path)
    store.refresh()
    before = len(store.state.trades)
    j = Journal(tmp_path, mirror_sqlite=True)
    try:
        for event in j.read(0, kinds=[EventKind.TRADE_RECORDED], limit=100):
            store.apply(event)
    finally:
        j.close()
    assert len(store.state.trades) == before
    store.close()


def test_equity_buffer_decimates_rather_than_dropping_the_head(tmp_path):
    """Regression: truncating from the front while keeping the first point drew a straight
    line from the run's start to the retained window -- a smooth diagonal implying gradual
    change that never happened."""
    store = ProjectionStore(tmp_path)
    store.state.max_equity = 100
    from atlas.bus.events import Event

    for i in range(500):
        store.apply(Event(seq=i + 1, ts=i * 1000, kind=EventKind.EQUITY_POINT,
                          payload={"equity": float(i), "balance": 10_000.0}))
    curve = store.state.equity
    assert len(curve) <= 100
    assert curve[-1][0] == 499 * 1000, "the curve must end at the newest point"
    assert curve[0][0] <= 0.05 * curve[-1][0], "and start near the beginning of the run"
    gaps = [b[0] - a[0] for a, b in zip(curve, curve[1:], strict=False)]
    # The grid is uniform; only the final "tip" gap may differ, and it exists so the curve
    # always ends at the newest point rather than lagging by up to one stride.
    # Every gap except the final tip is identical: no phase seam anywhere in the chart.
    assert len(set(gaps[:-1])) == 1, f"grid must be even, saw {sorted(set(gaps[:-1]))}"
    assert max(gaps) <= 0.05 * (curve[-1][0] - curve[0][0]), "no gap may span the chart"


def test_unknown_values_are_distinguishable_from_zero():
    s = Stamped()
    assert not s.known and s.value is None
    assert Stamped(value=0.0, seq=5, ts=1).known


def test_staleness_distinguishes_never_seen_from_old(tmp_path):
    store = ProjectionStore(tmp_path)
    assert store.staleness_ms(1_000_000) is None, "'no data' is not the same as 'old data'"
    seed(tmp_path, trades=1, equity_points=1)
    store.refresh()
    assert store.staleness_ms(10_000_000) is not None
    store.close()


def test_missing_run_directory_is_reported_not_faked(tmp_path):
    store = ProjectionStore(tmp_path / "nope")
    assert not store.available
    assert store.refresh() == 0
    assert store.staleness_ms(1) is None


def test_funnel_and_decision_lookup(tmp_path):
    seed(tmp_path, trades=5)
    store = ProjectionStore(tmp_path)
    store.refresh()
    funnel = store.funnel()
    assert funnel and funnel[0]["reason"] == "SETUP_CONFIRMED"
    assert funnel[0]["share"] == pytest.approx(1.0)
    assert store.decision("d3") is not None
    assert store.decision("nope") is None
    store.close()


def test_decision_buffer_is_bounded(tmp_path):
    seed(tmp_path, trades=0, equity_points=1)
    store = ProjectionStore(tmp_path)
    store.state.max_decisions = 10
    from atlas.bus.events import Event

    for i in range(50):
        store.apply(Event(seq=1000 + i, ts=i, kind=EventKind.DECISION,
                          payload={"record": {"decision_id": f"x{i}", "reason_code": "NO_SETUP",
                                              "outcome": "NO_SETUP", "ts": i,
                                              "symbol": "X", "strategy": "S"}}))
    assert len(store.state.decisions) == 10
    assert store.state.decisions[-1]["decision_id"] == "x49"


def test_list_runs_finds_journals(tmp_path):
    seed(tmp_path / "run_a", trades=1, equity_points=1)
    seed(tmp_path / "nested" / "run_b", trades=1, equity_points=1)
    runs = list_runs(tmp_path)
    assert {r["name"] for r in runs} == {"run_a", "nested/run_b"}
    assert list_runs(tmp_path / "missing") == []
