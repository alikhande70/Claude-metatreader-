"""Dashboard API.

The behaviours worth testing here are not "does it return 200" but the ones ADR-011 turns
into promises: unknown values must be distinguishable from zero, staleness must be
distinguishable from absence, and control endpoints must refuse rather than silently do
nothing when there is no engine to control.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.bus.events import EventKind
from atlas.bus.journal import Journal
from atlas.config.settings import example_settings
from atlas.core.enums import ExitReason, HaltReason, Side
from atlas.core.trading import Trade


def seed_run(path, trades: int = 5):
    with Journal(path, run_id="testrun", batch_size=100) as j:
        j.append(EventKind.RUN_STARTED, {"mode": "BACKTEST", "symbols": ["XAUUSD"]})
        j.append(EventKind.ENGINE_STATE, {"state": "RUNNING"})
        j.append(EventKind.VENUE_CONNECTED, {"detail": "simulator", "connected": True})
        for i in range(30):
            j.append(EventKind.EQUITY_POINT, {"equity": 10_000 + i * 3, "balance": 10_000},
                     ts=1_700_000_000_000 + i * 1000)
        for i in range(trades):
            trade = Trade(
                trade_id=f"T{i}", decision_id=f"d{i}", symbol="XAUUSD", side=Side.BUY,
                volume=0.1, entry_price=2600.0, entry_time=1_700_000_000_000,
                exit_price=2605.0, exit_time=1_700_000_100_000, initial_stop=2595.0,
                risk_money=50.0, gross_profit=50.0, exit_reason=ExitReason.TAKE_PROFIT,
                point=0.01,
            )
            j.append(EventKind.TRADE_RECORDED, {"trade": trade}, stream="XAUUSD")
            j.append(EventKind.DECISION, {"record": {
                "decision_id": f"d{i}", "ts": 1_700_000_000_000 + i, "symbol": "XAUUSD",
                "strategy": "SM1", "outcome": "SIGNAL", "reason_code": "SETUP_CONFIRMED",
                "conviction": 0.7, "gates": [{"name": "spread", "passed": True, "value": 20.0,
                                              "threshold": 30.0, "comparison": "<=",
                                              "detail": "", "hard": True}],
                "evidence": [{"name": "htf", "score": 0.8, "weight": 2.0, "detail": ""}],
                "features": {"M5.atr": 1.2}, "regime": "TREND_UP", "bias": "BUY",
                "proposal": None, "risk_notes": [], "sized_volume": 0.1,
                "client_order_id": None, "strategy_version": "1", "reason_detail": "",
            }}, stream="XAUUSD")
            j.append(EventKind.DECISION_OUTCOME, {"link": {
                "decision_id": f"d{i}", "trade_id": f"T{i}", "r_multiple": 1.0,
                "net_profit": 50.0, "mae_points": 10.0, "mfe_points": 60.0,
                "exit_reason": "TAKE_PROFIT", "holding_ms": 100_000,
            }}, stream="XAUUSD")
    return path


@pytest.fixture
def client(tmp_path):
    run = seed_run(tmp_path / "run")
    return TestClient(create_app(example_settings(), run))


def test_health_reports_what_is_and_is_not_known(client):
    body = client.get("/api/health").json()
    assert body["attached"] is False
    assert body["run_available"] is True
    assert body["engine_state"]["value"] == "RUNNING"
    assert body["engine_state"]["known"] is True
    # Nothing ever wrote a kill-switch event, so it must be reported as unknown, not as
    # "not halted" -- those are different claims.
    assert body["kill_switch"]["known"] is False
    assert body["kill_switch"]["value"] is None


def test_staleness_is_absent_when_nothing_was_seen(tmp_path):
    empty = TestClient(create_app(example_settings(), tmp_path / "nothing"))
    body = empty.get("/api/health").json()
    assert body["run_available"] is False
    assert body["staleness_ms"] is None, "'no data' must not be reported as 'stale'"
    assert body["stale"] is None


def test_overview_and_metrics(client):
    body = client.get("/api/overview").json()
    assert body["metrics"]["trades"] == 5
    assert body["risk"] is None, "risk state needs a live engine and must not be invented"
    assert body["account"]["known"] is True


def test_repeated_polling_does_not_duplicate_trades(client):
    """Regression for the threadpool race that showed 61 trades from a 41-trade journal."""
    for _ in range(6):
        client.get("/api/overview")
        client.get("/api/trades")
        client.get("/api/performance")
    trades = client.get("/api/trades").json()
    assert len(trades) == 5
    assert len({t["trade_id"] for t in trades}) == 5


def test_decision_detail_carries_gates_evidence_and_outcome(client):
    listing = client.get("/api/decisions?limit=10").json()
    assert listing
    detail = client.get(f"/api/decisions/{listing[0]['decision_id']}").json()
    assert detail["gates"] and detail["evidence"]
    assert detail["outcome_link"]["r_multiple"] == 1.0
    assert client.get("/api/decisions/nope").status_code == 404


def test_decision_filters(client):
    assert len(client.get("/api/decisions?outcome=SIGNAL").json()) == 5
    assert client.get("/api/decisions?outcome=VETOED").json() == []
    assert len(client.get("/api/decisions?reason=SETUP_CONFIRMED").json()) == 5


def test_funnel_and_equity(client):
    funnel = client.get("/api/funnel").json()
    assert funnel[0]["reason"] == "SETUP_CONFIRMED"
    equity = client.get("/api/equity?points=10").json()
    assert equity["points"]
    assert equity["points"][-1]["ts"] == 1_700_000_000_000 + 29 * 1000, (
        "downsampling must keep the newest point"
    )


def test_controls_refuse_when_not_attached(client):
    halt = client.post("/api/control/halt", json={"reason": "x"})
    assert halt.status_code == 409
    assert "not to a running engine" in halt.json()["detail"]
    assert client.post("/api/control/resume", json={}).status_code == 409


def test_config_never_serves_the_bridge_token(client):
    body = client.get("/api/config").json()
    assert "token" not in body.get("venue", {})


def test_attached_mode_exposes_risk_and_controls(tmp_path):
    """With an engine attached the controls work -- except the ones that must not."""
    from atlas.core.clock import FrozenClock
    from atlas.risk.config import RiskConfig
    from atlas.risk.engine import RiskEngine
    from atlas.risk.state import RiskState

    class FakeEngine:
        def __init__(self, journal):
            self.journal = journal
            self.risk = RiskEngine(RiskConfig(), RiskState())
            self.clock = FrozenClock(1)
            self.state = "RUNNING"

        def command_halt(self, detail):
            self.risk.halt(HaltReason.MANUAL, detail, 1)

        def command_resume(self, operator):
            return self.risk.resume(1, operator)

    run = seed_run(tmp_path / "run")
    journal = Journal(run, run_id="testrun")
    try:
        engine = FakeEngine(journal)
        api = TestClient(create_app(example_settings(), run, engine))
        assert api.get("/api/health").json()["attached"] is True
        assert api.get("/api/overview").json()["risk"] is not None

        assert api.post("/api/control/halt", json={"reason": "stop"}).json()["halted"] is True
        assert engine.risk.state.halted
        assert api.post("/api/control/resume", json={}).json()["resumed"] is True

        # A drawdown halt is not a notification to dismiss from a browser button.
        engine.risk.halt(HaltReason.TOTAL_DRAWDOWN_LIMIT, "breached", 1)
        blocked = api.post("/api/control/resume", json={})
        assert blocked.status_code == 409
        assert "cannot be cleared from the dashboard" in blocked.json()["detail"]
        assert engine.risk.state.halted, "the halt must survive the refusal"
    finally:
        journal.close()


def test_events_endpoint_can_filter_to_money_and_safety_events(client):
    everything = client.get("/api/events?limit=500").json()
    critical = client.get("/api/events?limit=500&critical_only=true").json()
    assert len(critical) < len(everything)
    assert all(e["kind"] in {"trade.recorded", "run.started"} for e in critical)


def test_websocket_greets(client):
    with client.websocket_connect("/ws/events") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["attached"] is False


def test_missing_dashboard_bundle_is_reported_not_blank(client):
    response = client.get("/")
    # Either the bundle is built (200) or the API explains how to build it (503).
    assert response.status_code in (200, 503)
    if response.status_code == 503:
        assert "npm run build" in response.json()["fix"]
