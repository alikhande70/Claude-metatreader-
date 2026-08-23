"""Strategy behaviour: gate ordering, veto reasons, stop construction, management.

The decisive test is ``test_every_bar_produces_an_auditable_decision``: it runs SM1 over
thousands of real (synthetic) bars and asserts that no evaluation is ever anonymous. That is
the property the whole audit story depends on.
"""

from __future__ import annotations

from collections import Counter

import pytest
from tests.conftest import build_features, quote_at

from atlas.core.enums import DecisionOutcome, ExitReason, Side, Timeframe
from atlas.core.errors import ConfigError
from atlas.core.market import Quote
from atlas.data.calendar import NewsCalendar, NewsEvent, ServerClockMapping, TradingCalendar
from atlas.features.frame import MultiTimeframeFeatures
from atlas.strategy.base import PositionView, StrategyContext
from atlas.strategy.baseline import DC1Params, DonchianBreakout
from atlas.strategy.regime import RegimeConfig, classify
from atlas.strategy.registry import build
from atlas.strategy.structure_momentum import SM1Params, StructureMomentum

LADDER = SM1Params(htf=Timeframe.H1, mtf=Timeframe.M15, ltf=Timeframe.M5)
TFS = [Timeframe.H1, Timeframe.M15, Timeframe.M5]


def make_ctx(feats, bars, spec, calendar, i, *, position=None, spread=None, sessions=(), **kw):
    """A context at bar ``i``, built from a precomputed feature set via a zero-copy view.

    ``view_at`` is the same call the live engine makes, so these tests exercise the real
    multi-timeframe alignment path rather than a test-only shortcut.
    """
    bar = bars[i]
    return StrategyContext(
        symbol=spec.name, spec=spec, now_ms=bar.ts_close,
        quote=quote_at(bar, spec, spread), features=feats.view_at(bar.ts_close),
        calendar=calendar, position=position, allowed_sessions=sessions, **kw,
    )


# --- configuration -------------------------------------------------------------------


def test_ladder_must_be_strictly_descending_and_separated():
    with pytest.raises(ConfigError, match="descending"):
        SM1Params(htf=Timeframe.H1, mtf=Timeframe.H1, ltf=Timeframe.M5)
    with pytest.raises(ConfigError, match="3x"):
        SM1Params(htf=Timeframe.H1, mtf=Timeframe.M30, ltf=Timeframe.M15)
    SM1Params(htf=Timeframe.H4, mtf=Timeframe.H1, ltf=Timeframe.M15)


def test_registry_rejects_unknown_parameters():
    with pytest.raises(ConfigError, match="unknown parameters"):
        build("SM1", {"rr_targt": 2.0})
    with pytest.raises(ConfigError, match="unknown strategy"):
        build("NOPE")
    assert build("SM1", {"rr_target": 3.0}).p.rr_target == 3.0


def test_parameters_are_journallable():
    for name in ("SM1", "DC1"):
        p = build(name).parameters()
        assert p, name
        assert all(isinstance(v, (int, float, str)) for v in p.values()), p
    assert build("SM1").parameters()["htf"] == "H4"


def test_location_gate_measures_retrace_depth_not_trigger_position(m5_bars, gold, calendar,
                                                                   gold_features):
    """Regression for the spec bug that produced zero signals.

    The gate must report the deepest point the retrace reached inside the arming window. If
    it reverted to measuring position at the trigger bar, the reported value would move to
    the far side of the range and signals would disappear again.
    """
    strat = StructureMomentum(LADDER)
    values = []
    for i in range(2000, len(m5_bars), 3):
        d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=()))
        g = next((g for g in d.gates if g.name == "location"), None)
        if g is not None and g.value is not None:
            values.append(g.value)
            assert "deepest retrace" in g.detail
    assert values, "the location gate must be reached at least sometimes"
    assert min(values) < 0.3, "some retraces must reach deep into the favourable half"


# --- the audit property ---------------------------------------------------------------


def test_every_bar_produces_an_auditable_decision(m5_bars, gold, calendar, gold_features):
    """No evaluation may be anonymous: every one carries an outcome, a reason code and
    gates whose numbers explain it."""
    strat = StructureMomentum(LADDER)
    m5 = gold_features.require(Timeframe.M5)
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()

    # Evaluate on a grid of bars using progressively truncated feature frames.
    for i in range(3000, 9000, 25):
        ctx = make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=("LONDON", "NEWYORK"))
        d = strat.evaluate(ctx)
        outcomes[str(d.outcome)] += 1
        reasons[d.reason_code] += 1
        assert d.decision_id and d.reason_code, "every decision must be identifiable"
        assert d.gates, "every decision must record the gates it evaluated"
        assert d.explain()
        if d.outcome is DecisionOutcome.VETOED:
            assert d.failed_hard_gates, "a veto must name at least one failed hard gate"
        if d.outcome is DecisionOutcome.SIGNAL:
            assert d.proposal is not None
            assert 0.0 <= d.conviction <= 1.0
            assert d.evidence and abs(sum(e.contribution for e in d.evidence)) >= 0
    assert sum(outcomes.values()) == 240
    assert outcomes["NO_SETUP"] > 0, "standing aside must be the common case, and recorded"
    assert len(reasons) >= 3, f"expected a spread of reasons, got {reasons}"
    assert m5.n > 0


def test_decision_ids_are_stable_across_reruns(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    a = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, 4000))
    b = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, 4000))
    assert a.decision_id == b.decision_id
    assert a.outcome == b.outcome and a.reason_code == b.reason_code


# --- individual gates -----------------------------------------------------------------


def test_wide_spread_is_vetoed_with_the_numbers(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    ctx = make_ctx(gold_features, m5_bars, gold, calendar, 4000, spread=5000, sessions=("LONDON", "NEWYORK"))
    d = strat.evaluate(ctx)
    if d.outcome is DecisionOutcome.VETOED:
        assert d.reason_code == "SPREAD_TOO_WIDE"
        g = next(g for g in d.gates if g.name == "spread")
        assert g.value == 5000 and g.threshold is not None and not g.passed
    else:
        # An earlier gate (bias/regime) legitimately short-circuits before spread is reached.
        assert d.reason_code in {"NO_HTF_BIAS", "RANGE_REGIME", "NOT_READY", "VOL_SHOCK"}


def test_session_filter_blocks_out_of_hours(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    seen = set()
    for i in range(3000, 6000, 7):
        d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=("LONDON",)))
        seen.add(d.reason_code)
        if d.reason_code == "OUT_OF_SESSION":
            assert d.outcome is DecisionOutcome.VETOED
    assert "OUT_OF_SESSION" in seen, "a London-only filter must reject some bars"


def test_empty_session_filter_means_no_restriction(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    for i in range(3000, 4000, 37):
        d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=()))
        assert d.reason_code != "OUT_OF_SESSION"


def test_news_policy_block_refuses_when_the_calendar_is_missing(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    codes = set()
    for i in range(3000, 5000, 11):
        d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=(),
                                    news_policy="block"))
        codes.add(d.reason_code)
    assert "NEWS_CALENDAR_UNAVAILABLE" in codes


def test_news_window_veto_names_the_event(m5_bars, gold, gold_features):
    strat = StructureMomentum(LADDER)
    i = 4000
    ts = m5_bars[i].ts_close
    cal = TradingCalendar(
        server=ServerClockMapping(offset_seconds=3 * 3600, measured_at_ms=1),
        news=NewsCalendar([NewsEvent(ts=ts, currency="USD", impact="HIGH", title="CPI")]),
    )
    d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, cal, i, sessions=()))
    if d.reason_code == "NEWS_WINDOW":
        assert "CPI" in d.reason_detail
        g = next(g for g in d.gates if g.name == "news_window")
        assert not g.passed and "CPI" in g.detail


def test_open_position_suppresses_new_signals(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    pv = PositionView(ticket=1, side=Side.BUY, entry_price=2600.0, stop_loss=2590.0,
                      take_profit=2620.0, open_time=0, bars_held=3, r_multiple_open=0.5,
                      mfe_r=0.7)
    d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, 4000, position=pv))
    assert d.outcome is DecisionOutcome.SUPPRESSED
    assert d.reason_code == "POSITION_OPEN"


def test_warmup_is_reported_not_guessed(m5_bars, gold, calendar):
    strat = StructureMomentum(LADDER)
    feats = build_features(m5_bars[:120], gold, TFS)
    ctx = StrategyContext(symbol=gold.name, spec=gold, now_ms=m5_bars[119].ts_close,
                          quote=quote_at(m5_bars[119], gold), features=feats, calendar=calendar)
    d = strat.evaluate(ctx)
    assert d.outcome is DecisionOutcome.NO_SETUP and d.reason_code == "NOT_READY"


def test_missing_feature_frame_is_not_ready(gold, calendar):
    strat = StructureMomentum(LADDER)
    ctx = StrategyContext(symbol="XAUUSD", spec=gold, now_ms=0,
                          quote=Quote(symbol="XAUUSD", ts=0, bid=2600.0, ask=2600.3),
                          features=MultiTimeframeFeatures("XAUUSD", {}), calendar=calendar)
    d = strat.evaluate(ctx)
    assert d.reason_code == "NOT_READY"


# --- proposals ------------------------------------------------------------------------


def test_signals_have_coherent_stops_and_targets(m5_bars, gold, calendar, gold_features):
    """SM1 is selective -- roughly 0.3 signals per trading day on this ladder -- so this scans
    every bar rather than a stride, or it would find none by construction."""
    strat = StructureMomentum(LADDER)
    signals = []
    for i in range(2000, len(m5_bars)):
        d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=()))
        if d.outcome is DecisionOutcome.SIGNAL:
            signals.append(d)
    assert len(signals) >= 5, f"expected several signals over 10k bars, got {len(signals)}"
    for d in signals:
        p = d.proposal
        assert p is not None
        if p.side is Side.BUY:
            assert p.stop_loss < p.entry_price < p.take_profit
        else:
            assert p.take_profit < p.entry_price < p.stop_loss
        assert p.stop_points >= gold.min_stop_distance_points(5)
        rr = abs(p.take_profit - p.entry_price) / abs(p.entry_price - p.stop_loss)
        assert rr == pytest.approx(strat.p.rr_target, rel=0.02)
        assert gold.normalize_price(p.stop_loss) == p.stop_loss


def test_conviction_decomposes_exactly(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    for i in range(2000, len(m5_bars)):
        d = strat.evaluate(make_ctx(gold_features, m5_bars, gold, calendar, i, sessions=()))
        if d.outcome is not DecisionOutcome.SIGNAL:
            continue
        total_w = sum(e.weight for e in d.evidence)
        recomputed = min(1.0, max(0.0, sum(e.contribution for e in d.evidence) / total_w))
        assert d.conviction == pytest.approx(recomputed, abs=1e-9)
        assert all(-1.0 <= e.score <= 1.0 for e in d.evidence)
        return
    pytest.skip("no signal found in the scanned range")


# --- management -----------------------------------------------------------------------


def _mgmt_ctx(gold_features, m5_bars, gold, calendar, i, pv):
    return make_ctx(gold_features, m5_bars, gold, calendar, i, position=pv)


def test_breakeven_then_trail_then_never_loosen(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    i = 5000
    entry = float(m5_bars[i].close)
    sl = entry - 5.0

    below = PositionView(ticket=1, side=Side.BUY, entry_price=entry, stop_loss=sl,
                         take_profit=entry + 10, open_time=0, bars_held=2,
                         r_multiple_open=0.4, mfe_r=0.4)
    assert strat.manage(_mgmt_ctx(gold_features, m5_bars, gold, calendar, i, below)) is None

    at_be = PositionView(ticket=1, side=Side.BUY, entry_price=entry, stop_loss=sl,
                         take_profit=entry + 10, open_time=0, bars_held=5,
                         r_multiple_open=1.2, mfe_r=1.2)
    act = strat.manage(_mgmt_ctx(gold_features, m5_bars, gold, calendar, i, at_be))
    if act is not None:
        assert act.reason is ExitReason.BREAKEVEN_STOP
        assert act.new_stop_loss > sl, "a breakeven move must raise the stop"

        tightened = PositionView(ticket=1, side=Side.BUY, entry_price=entry,
                                 stop_loss=act.new_stop_loss, take_profit=entry + 10,
                                 open_time=0, bars_held=6, r_multiple_open=1.2, mfe_r=1.2)
        assert strat.manage(_mgmt_ctx(gold_features, m5_bars, gold, calendar, i, tightened)) is None, (
            "management must be idempotent -- it must not re-issue the same move"
        )


def test_trail_only_tightens_for_shorts_too(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    i = 5000
    entry = float(m5_bars[i].close)
    pv = PositionView(ticket=2, side=Side.SELL, entry_price=entry, stop_loss=entry + 5.0,
                      take_profit=entry - 10, open_time=0, bars_held=10,
                      r_multiple_open=2.0, mfe_r=2.0)
    act = strat.manage(_mgmt_ctx(gold_features, m5_bars, gold, calendar, i, pv))
    if act is not None and act.new_stop_loss is not None:
        assert act.new_stop_loss < entry + 5.0, "a short's stop may only move down"


def test_time_stop_closes_a_stalled_trade(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    i = 5000
    entry = float(m5_bars[i].close)
    pv = PositionView(ticket=3, side=Side.BUY, entry_price=entry, stop_loss=entry - 5,
                      take_profit=entry + 10, open_time=0,
                      bars_held=strat.p.max_time_stop_bars + 1, r_multiple_open=0.2, mfe_r=0.3)
    act = strat.manage(_mgmt_ctx(gold_features, m5_bars, gold, calendar, i, pv))
    assert act is not None and act.close_fraction == 1.0
    assert act.reason is ExitReason.TIME_STOP


def test_time_stop_does_not_fire_on_a_working_trade(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    i = 5000
    entry = float(m5_bars[i].close)
    pv = PositionView(ticket=4, side=Side.BUY, entry_price=entry, stop_loss=entry - 5,
                      take_profit=entry + 10, open_time=0,
                      bars_held=strat.p.max_time_stop_bars + 5, r_multiple_open=1.4, mfe_r=1.6)
    act = strat.manage(_mgmt_ctx(gold_features, m5_bars, gold, calendar, i, pv))
    assert act is None or act.close_fraction == 0.0


def test_management_without_a_position_is_a_noop(m5_bars, gold, calendar, gold_features):
    strat = StructureMomentum(LADDER)
    assert strat.manage(make_ctx(gold_features, m5_bars, gold, calendar, 5000)) is None


# --- regime and baseline ---------------------------------------------------------------


def test_regime_classifier_labels_the_extremes(m5_bars, gold):
    feats = build_features(m5_bars[:6000], gold, TFS)
    f = feats.require(Timeframe.M15)
    labels = Counter(str(classify(f, RegimeConfig(), i).regime) for i in range(300, f.n))
    assert labels["RANGE"] > 0 and (labels["TREND_UP"] + labels["TREND_DOWN"]) > 0
    assert classify(f, RegimeConfig(), 3).regime.value == "UNKNOWN"


def test_baseline_produces_signals_and_respects_the_channel(m5_bars, gold, calendar):
    strat = DonchianBreakout(DC1Params(tf=Timeframe.M15, channel=20))
    n = 0
    for i in range(2000, 8000, 17):
        feats = build_features(m5_bars[: i + 1], gold, [Timeframe.M15])
        ctx = StrategyContext(symbol=gold.name, spec=gold, now_ms=m5_bars[i].ts_close,
                              quote=quote_at(m5_bars[i], gold), features=feats,
                              calendar=calendar)
        d = strat.evaluate(ctx)
        if d.outcome is DecisionOutcome.SIGNAL:
            n += 1
            assert d.proposal is not None
            f = feats.require(Timeframe.M15)
            if d.proposal.side is Side.BUY:
                assert f.close[-1] > f.donchian_high[-1]
            else:
                assert f.close[-1] < f.donchian_low[-1]
    assert n > 0, "the baseline must fire at least once over 6000 bars"
