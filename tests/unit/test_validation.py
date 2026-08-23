"""The validation battery.

These tests check that the battery *fails the things it should fail*. A validator that
approves everything is worse than none, because it converts an unexamined strategy into an
approved one.
"""

from __future__ import annotations

import numpy as np
import pytest

from atlas.analytics.metrics import compute_metrics
from atlas.backtest.validation import (
    DEFAULT_THRESHOLDS,
    assemble_report,
    data_snooping_penalty,
    expectancy_ci,
    monte_carlo,
    outlier_dependence,
    plan_windows,
    plateau_analysis,
    walk_forward_efficiency,
)
from atlas.core.enums import ExitReason, Side, Timeframe
from atlas.core.market import Bar
from atlas.core.trading import Trade


def trades_with(r_multiples, risk=100.0):
    out = []
    for i, r in enumerate(r_multiples):
        out.append(Trade(
            trade_id=f"T{i}", decision_id=f"d{i}", symbol="X", side=Side.BUY, volume=0.1,
            entry_price=100.0, entry_time=i * 1000, exit_price=100.0 + r,
            exit_time=i * 1000 + 1, initial_stop=99.0, risk_money=risk,
            gross_profit=r * risk, exit_reason=ExitReason.TAKE_PROFIT, point=0.01,
        ))
    return out


def test_monte_carlo_places_the_historical_drawdown_in_a_distribution():
    rng = np.random.default_rng(3)
    r = rng.normal(0.05, 1.0, 400)
    mc = monte_carlo(trades_with(r), runs=800)
    assert mc.p95_max_dd_r >= mc.median_max_dd_r >= 0
    assert mc.p99_max_dd_r >= mc.p95_max_dd_r
    assert 0.0 <= mc.historical_dd_percentile <= 100.0
    assert mc.p05_total_r <= mc.median_total_r <= mc.p95_total_r


def test_monte_carlo_needs_a_sample():
    assert monte_carlo(trades_with([1.0, -1.0]), runs=100).runs == 0


def test_expectancy_ci_says_no_conclusion_when_it_straddles_zero():
    small = expectancy_ci(trades_with([1.0, -1.0, 1.0, -1.0, 0.5, -0.5]), runs=500)
    assert small.contains_zero
    assert "NO CONCLUSION" in small.summary()

    rng = np.random.default_rng(1)
    strong = expectancy_ci(trades_with(rng.normal(0.5, 0.5, 500)), runs=500)
    assert not strong.contains_zero


def test_outlier_dependence_detects_a_few_lucky_days():
    lucky = trades_with([-0.2] * 40 + [12.0, 11.0, 10.0, 9.0, 8.0])
    result = outlier_dependence(lucky)
    assert result.full_total_r > 0
    assert not result.survives, "removing the best five must expose this as luck"

    genuine = trades_with([0.3] * 60)
    assert outlier_dependence(genuine).survives


def test_outlier_summary_does_not_print_a_nonsense_share():
    """Regression: a mostly-losing sample printed '-127% of gross came from them'.

    It happens when the best five trades are still mostly losses: their sum is negative
    while gross profit is a small positive number, so the ratio is large and negative. The
    share is meaningless there and is suppressed rather than printed.
    """
    pathological = outlier_dependence(trades_with([0.1] + [-1.0] * 30))
    assert pathological.top5_share < 0, "this is the case that produced the bad output"
    assert "% of gross" not in pathological.summary()

    # A normal sample still reports the share, because there it means something.
    normal = outlier_dependence(trades_with([2.0, 1.8, 1.5, 1.2, 1.0] + [0.1] * 40))
    assert "% of gross profit came from them" in normal.summary()


def test_plateau_analysis_distinguishes_a_plateau_from_a_spike():
    plateau = {(a, b): 0.10 for a in (10, 20, 30) for b in (1, 2, 3)}
    assert plateau_analysis(plateau, (20, 2)).is_plateau

    spike = {(a, b): 0.01 for a in (10, 20, 30) for b in (1, 2, 3)}
    spike[(20, 2)] = 0.40
    result = plateau_analysis(spike, (20, 2))
    assert not result.is_plateau
    assert result.relative_drop > 0.9
    assert "SPIKE" in result.summary()


def test_walk_forward_efficiency_is_trade_weighted():
    is_m = [compute_metrics(trades_with([0.2] * 100))]
    oos_m = [compute_metrics(trades_with([0.1] * 100))]
    assert walk_forward_efficiency(is_m, oos_m) == pytest.approx(0.5, rel=0.05)

    # A two-trade window must not dominate a fifty-trade one. Compared against the
    # unweighted alternative, which would let the tiny window set the answer.
    is_two = [compute_metrics(trades_with([0.2] * 50)), compute_metrics(trades_with([0.2] * 2))]
    oos_two = [compute_metrics(trades_with([0.1] * 50)), compute_metrics(trades_with([5.0] * 2))]
    weighted = walk_forward_efficiency(is_two, oos_two)
    unweighted = ((0.1 + 5.0) / 2) / ((0.2 + 0.2) / 2)
    assert weighted < unweighted / 5, (
        f"trade weighting must blunt the tiny window: {weighted:.2f} vs {unweighted:.2f}"
    )


def test_walk_forward_efficiency_of_a_losing_in_sample_is_zero():
    losing = [compute_metrics(trades_with([-0.2] * 50))]
    assert walk_forward_efficiency(losing, [compute_metrics(trades_with([0.3] * 50))]) == 0.0


def test_window_planning_anchored_versus_rolling():
    bars = [Bar(symbol="X", tf=Timeframe.M5, ts=i * 300_000, open=1, high=1, low=1, close=1)
            for i in range(1000)]
    anchored = plan_windows(bars, is_bars=400, oos_bars=100, anchored=True)
    assert len(anchored) == 6
    assert all(w.is_start == anchored[0].is_start for w in anchored), "anchored: fixed start"
    assert anchored[0].oos_start > anchored[0].is_end, "OOS must follow IS, never overlap"

    rolling = plan_windows(bars, is_bars=400, oos_bars=100, anchored=False)
    assert rolling[1].is_start > rolling[0].is_start, "rolling: the window slides"


def test_data_snooping_penalty_grows_with_the_search():
    assert data_snooping_penalty(1) == 0.0
    assert data_snooping_penalty(100) < data_snooping_penalty(5000)
    assert data_snooping_penalty(5000) == pytest.approx(4.12, abs=0.05)


def test_a_good_strategy_on_real_data_can_be_approved():
    """The battery must be passable, or it is not a test, it is a wall."""
    rng = np.random.default_rng(7)
    oos = trades_with(rng.normal(0.22, 0.9, 400))
    is_ = trades_with(rng.normal(0.28, 0.9, 400))
    report = assemble_report(
        label="good", data_source="real broker history", synthetic=False,
        is_trades=is_, oos_trades=oos,
        cost_sensitivity={"1.5x": 0.15, "2.0x": 0.08},
        walk_forward=None,
    )
    failed = [c.name for c in report.criteria if not c.passed]
    assert failed == [], f"a genuinely good sample must pass, but failed: {failed}"
    assert report.approved
    assert "APPROVED" in report.render()


def test_synthetic_data_can_never_be_approved():
    rng = np.random.default_rng(7)
    great = trades_with(rng.normal(0.5, 0.5, 500))
    report = assemble_report(
        label="synthetic", data_source="generator", synthetic=True,
        is_trades=great, oos_trades=great, cost_sensitivity={"2.0x": 0.4},
    )
    assert not report.approved
    assert "SYNTHETIC DATA" in report.render()
    assert any(c.name == "data_is_real" and not c.passed for c in report.criteria)


def test_a_marginal_strategy_fails_on_the_criteria_that_matter():
    rng = np.random.default_rng(9)
    marginal = trades_with(rng.normal(0.01, 1.0, 300))
    report = assemble_report(
        label="marginal", data_source="real", synthetic=False,
        is_trades=marginal, oos_trades=marginal,
        cost_sensitivity={"2.0x": -0.05},
    )
    failed = {c.name for c in report.criteria if not c.passed}
    assert "expectancy_ci_excludes_zero" in failed
    assert "expectancy_at_2x_costs" in failed
    assert not report.approved


def test_thresholds_are_fixed_in_advance_and_documented():
    """They are a contract with the future, not a dial to turn after seeing the result."""
    assert DEFAULT_THRESHOLDS["min_oos_trades"] == 250
    assert DEFAULT_THRESHOLDS["min_wfe"] == 0.5
    spec = (__import__("pathlib").Path(__file__).resolve().parents[2]
            / "docs" / "STRATEGY.md").read_text(encoding="utf-8")
    assert "250" in spec and "0.5" in spec, "docs/STRATEGY.md must state the same thresholds"
