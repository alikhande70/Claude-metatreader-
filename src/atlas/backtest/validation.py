"""Validation battery.

The default assumption is that a backtest is optimistic, and that is not pessimism -- it is
the base rate. The process that produces backtests (try things, keep what worked) selects for
luck exactly as reliably as it selects for edge, and the winner cannot be told from the lucky
by looking at it.

This module runs the tests that can tell them apart, and produces a report with an explicit
verdict against thresholds **fixed in advance** (``docs/STRATEGY.md`` §1.10). Fixing the
thresholds before the run is the difference between validating a strategy and negotiating
with the data.

Included:

* in-sample / out-of-sample split
* anchored walk-forward with Walk-Forward Efficiency
* Monte Carlo resampling of the trade sequence for the drawdown distribution
* bootstrap confidence interval on expectancy
* outlier dependence (drop the best five trades)
* parameter neighbourhood: plateau versus spike
* cost sensitivity at 1.5x and 2x
* a **data-snooping penalty** derived from how many parameter combinations were tried
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from atlas.analytics.metrics import PerformanceMetrics, compute_metrics
from atlas.core.market import Bar, ms_to_dt
from atlas.core.trading import Trade

# --- resampling -------------------------------------------------------------------------


@dataclass(slots=True)
class MonteCarloResult:
    runs: int
    median_total_r: float
    p05_total_r: float
    p95_total_r: float
    median_max_dd_r: float
    p95_max_dd_r: float
    p99_max_dd_r: float
    prob_negative: float
    historical_max_dd_r: float
    historical_dd_percentile: float

    def summary(self) -> str:
        return (
            f"MC({self.runs}): total R p05 {self.p05_total_r:+.1f} / median "
            f"{self.median_total_r:+.1f} / p95 {self.p95_total_r:+.1f}; "
            f"max DD median {self.median_max_dd_r:.1f}R p95 {self.p95_max_dd_r:.1f}R "
            f"p99 {self.p99_max_dd_r:.1f}R; P(total < 0) = {self.prob_negative:.1%}; "
            f"the historical drawdown sits at the {self.historical_dd_percentile:.0f}th "
            f"percentile of this distribution"
        )


def monte_carlo(trades: Sequence[Trade], runs: int = 5000, seed: int = 7) -> MonteCarloResult:
    """Bootstrap-resample the trade sequence.

    The historical maximum drawdown is **one draw** from a distribution, and because the
    historical ordering is the one that happened to occur, it usually sits toward the
    optimistic end. Sizing an account for it is sizing for the good case. Size for the p95.

    Resampling is with replacement over the R-multiple series, which assumes trades are
    independent. They are not perfectly -- consecutive trades share a regime -- so the tails
    here are, if anything, still too kind.
    """
    r = np.array([t.r_multiple for t in trades], dtype=float)
    if len(r) < 5:
        return MonteCarloResult(0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0)
    rng = np.random.default_rng(seed)
    n = len(r)
    totals = np.empty(runs)
    dds = np.empty(runs)
    for i in range(runs):
        sample = rng.choice(r, size=n, replace=True)
        curve = np.cumsum(sample)
        totals[i] = curve[-1]
        peak = np.maximum.accumulate(np.insert(curve, 0, 0.0))
        dds[i] = float((peak - np.insert(curve, 0, 0.0)).max())

    hist_curve = np.insert(np.cumsum(r), 0, 0.0)
    hist_dd = float((np.maximum.accumulate(hist_curve) - hist_curve).max())
    return MonteCarloResult(
        runs=runs,
        median_total_r=float(np.median(totals)),
        p05_total_r=float(np.percentile(totals, 5)),
        p95_total_r=float(np.percentile(totals, 95)),
        median_max_dd_r=float(np.median(dds)),
        p95_max_dd_r=float(np.percentile(dds, 95)),
        p99_max_dd_r=float(np.percentile(dds, 99)),
        prob_negative=float((totals < 0).mean()),
        historical_max_dd_r=hist_dd,
        historical_dd_percentile=float((dds <= hist_dd).mean() * 100.0),
    )


@dataclass(slots=True)
class ExpectancyCI:
    mean_r: float
    lo_r: float
    hi_r: float
    contains_zero: bool
    n: int

    def summary(self) -> str:
        verdict = "NO CONCLUSION AVAILABLE" if self.contains_zero else "positive"
        return (
            f"expectancy {self.mean_r:+.3f}R, 95% CI [{self.lo_r:+.3f}, {self.hi_r:+.3f}] "
            f"over {self.n} trades -- {verdict}"
        )


def expectancy_ci(trades: Sequence[Trade], runs: int = 5000, seed: int = 11) -> ExpectancyCI:
    """Bootstrap confidence interval on per-trade expectancy.

    If the interval contains zero, no conclusion is available yet, and saying so is the
    correct answer rather than reporting the point estimate as if it were a finding.
    """
    r = np.array([t.r_multiple for t in trades], dtype=float)
    if len(r) < 5:
        return ExpectancyCI(0.0, 0.0, 0.0, True, len(r))
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(r, size=len(r), replace=True).mean() for _ in range(runs)])
    lo, hi = float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
    return ExpectancyCI(float(r.mean()), lo, hi, lo <= 0.0 <= hi, len(r))


@dataclass(slots=True)
class OutlierDependence:
    full_total_r: float
    without_top5_r: float
    top5_share: float
    survives: bool

    def summary(self) -> str:
        return (
            f"total {self.full_total_r:+.1f}R, {self.without_top5_r:+.1f}R without the best "
            f"five trades ({self.top5_share:.0%} of gross came from them) -- "
            f"{'survives' if self.survives else 'DOES NOT SURVIVE'}"
        )


def outlier_dependence(trades: Sequence[Trade], drop: int = 5) -> OutlierDependence:
    """Remove the best trades. If the edge vanishes, it was a few lucky days."""
    r = np.array([t.r_multiple for t in trades], dtype=float)
    if len(r) <= drop:
        return OutlierDependence(float(r.sum()), 0.0, 1.0, False)
    order = np.argsort(r)[::-1]
    kept = np.delete(r, order[:drop])
    gross = r[r > 0].sum()
    top = r[order[:drop]].sum()
    return OutlierDependence(
        full_total_r=float(r.sum()), without_top5_r=float(kept.sum()),
        top5_share=float(top / gross) if gross > 0 else 1.0,
        survives=bool(kept.sum() > 0),
    )


# --- window planning ------------------------------------------------------------------


@dataclass(slots=True)
class Window:
    index: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int

    def describe(self) -> str:
        f = "%Y-%m-%d"
        return (
            f"W{self.index}: IS {ms_to_dt(self.is_start):{f}}..{ms_to_dt(self.is_end):{f}} "
            f"-> OOS {ms_to_dt(self.oos_start):{f}}..{ms_to_dt(self.oos_end):{f}}"
        )


def plan_windows(
    bars: Sequence[Bar], *, is_bars: int, oos_bars: int, anchored: bool = True,
    max_windows: int = 12,
) -> list[Window]:
    """Walk-forward window schedule over a bar series.

    Anchored windows grow the in-sample period from a fixed start (more data each time,
    slower to adapt); rolling windows keep it a fixed length (adapts faster, forgets more).
    Anchored is the default because it is the harder test: later windows must work with
    parameters fitted on progressively more, and more varied, history.
    """
    n = len(bars)
    out: list[Window] = []
    start = 0
    is_end = is_bars
    idx = 0
    while is_end + oos_bars <= n and idx < max_windows:
        out.append(Window(
            index=idx,
            is_start=bars[start].ts, is_end=bars[is_end - 1].ts,
            oos_start=bars[is_end].ts, oos_end=bars[min(n, is_end + oos_bars) - 1].ts,
        ))
        idx += 1
        is_end += oos_bars
        if not anchored:
            start += oos_bars
    return out


def slice_bars(bars: Sequence[Bar], start_ts: int, end_ts: int) -> list[Bar]:
    return [b for b in bars if start_ts <= b.ts <= end_ts]


# --- walk forward ---------------------------------------------------------------------


@dataclass(slots=True)
class WalkForwardWindow:
    window: Window
    best_params: dict
    is_metrics: PerformanceMetrics
    oos_metrics: PerformanceMetrics
    combinations_tested: int


@dataclass(slots=True)
class WalkForwardResult:
    windows: list[WalkForwardWindow] = field(default_factory=list)
    efficiency: float = 0.0
    profitable_oos_fraction: float = 0.0
    total_oos_trades: int = 0
    total_oos_r: float = 0.0
    combinations_per_window: int = 0
    parameter_stability: float = 0.0

    def summary(self) -> str:
        return (
            f"WFA over {len(self.windows)} windows: WFE {self.efficiency:.2f}, "
            f"{self.profitable_oos_fraction:.0%} of OOS windows profitable, "
            f"{self.total_oos_trades} OOS trades totalling {self.total_oos_r:+.1f}R; "
            f"parameter stability {self.parameter_stability:.2f} "
            f"({self.combinations_per_window} combinations tested per window)"
        )


def walk_forward_efficiency(is_metrics: list[PerformanceMetrics],
                            oos_metrics: list[PerformanceMetrics]) -> float:
    """WFE = out-of-sample expectancy / in-sample expectancy, trade-weighted.

    Definition matters and implementations differ, so it is stated explicitly: both sides use
    **expectancy in R per trade**, weighted by trade count, so a window that produced two
    trades cannot dominate one that produced fifty.

    Reading: 1.0 means OOS matched IS. Below ~0.5 means most of the in-sample performance was
    fitted. Above 1.0 is not a triumph -- it usually means the in-sample period was unusually
    hard, and is worth investigating rather than celebrating.
    """
    is_n = sum(m.trades for m in is_metrics)
    oos_n = sum(m.trades for m in oos_metrics)
    if is_n == 0 or oos_n == 0:
        return 0.0
    is_exp = sum(m.expectancy_r * m.trades for m in is_metrics) / is_n
    oos_exp = sum(m.expectancy_r * m.trades for m in oos_metrics) / oos_n
    if is_exp <= 0:
        return 0.0
    return float(oos_exp / is_exp)


# --- parameter surface -----------------------------------------------------------------


@dataclass(slots=True)
class PlateauResult:
    best_params: dict
    best_score: float
    neighbour_scores: list[float]
    neighbour_mean: float
    relative_drop: float
    is_plateau: bool

    def summary(self) -> str:
        shape = "plateau" if self.is_plateau else "SPIKE (fitted)"
        return (
            f"best {self.best_score:+.3f}R, neighbours mean {self.neighbour_mean:+.3f}R "
            f"({self.relative_drop:.0%} drop) -- {shape}"
        )


def plateau_analysis(
    surface: dict[tuple, float], best: tuple, *, tolerance: float = 0.25
) -> PlateauResult:
    """Is the chosen parameter set on a plateau or a spike?

    Real edges sit on plateaus: neighbouring parameter values also work. Fitted noise sits on
    spikes: one combination is excellent and its immediate neighbours lose. Neighbours here
    are combinations differing in exactly one coordinate by one grid step.
    """
    keys = list(surface)
    if not keys or best not in surface:
        return PlateauResult({}, 0.0, [], 0.0, 1.0, False)
    axes = [sorted({k[i] for k in keys}) for i in range(len(best))]
    neighbours: list[float] = []
    for i, axis in enumerate(axes):
        pos = axis.index(best[i])
        for step in (-1, 1):
            j = pos + step
            if 0 <= j < len(axis):
                cand = list(best)
                cand[i] = axis[j]
                score = surface.get(tuple(cand))
                if score is not None:
                    neighbours.append(score)
    best_score = surface[best]
    if not neighbours:
        return PlateauResult(dict(enumerate(best)), best_score, [], best_score, 0.0, False)
    mean = float(np.mean(neighbours))
    drop = 1.0 - (mean / best_score) if best_score > 0 else 1.0
    return PlateauResult(
        best_params=dict(enumerate(best)), best_score=best_score,
        neighbour_scores=neighbours, neighbour_mean=mean, relative_drop=float(drop),
        is_plateau=bool(best_score <= 0 or drop <= tolerance),
    )


def data_snooping_penalty(combinations: int) -> float:
    """Expected best-of-N Sharpe from pure noise, in standard errors.

    With ``N`` independent random strategies the best one shows a Sharpe roughly
    ``sqrt(2 ln N)`` standard errors above zero **by construction**. Testing 5,000
    combinations and reporting the winner without this correction is reporting a selection
    artefact. The number is an approximation (real parameter combinations are correlated, so
    the true penalty is smaller) but it is the right order of magnitude and it makes the
    problem visible instead of invisible.
    """
    if combinations <= 1:
        return 0.0
    return float(math.sqrt(2.0 * math.log(combinations)))


# --- report ------------------------------------------------------------------------------


@dataclass(slots=True)
class Criterion:
    name: str
    value: float | str
    threshold: float | str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class ValidationReport:
    label: str
    data_source: str
    synthetic: bool
    oos_metrics: PerformanceMetrics | None = None
    is_metrics: PerformanceMetrics | None = None
    walk_forward: WalkForwardResult | None = None
    monte_carlo: MonteCarloResult | None = None
    expectancy: ExpectancyCI | None = None
    outliers: OutlierDependence | None = None
    plateau: PlateauResult | None = None
    cost_sensitivity: dict[str, float] = field(default_factory=dict)
    snooping_penalty: float = 0.0
    criteria: list[Criterion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        """A configuration is approved only if every criterion passes AND the data is real.

        Synthetic data can validate machinery; it can never validate an edge, because the
        only thing a strategy tuned on a generator has learned is the generator (ADR-006).
        """
        if self.synthetic:
            return False
        return bool(self.criteria) and all(c.passed for c in self.criteria)

    def render(self) -> str:
        lines = [
            f"VALIDATION REPORT -- {self.label}",
            f"data source: {self.data_source}",
            "",
        ]
        if self.synthetic:
            lines += [
                "*** SYNTHETIC DATA ***",
                "These results validate the SYSTEM, not the strategy. A strategy evaluated on",
                "generated data has been evaluated against the generator's assumptions. This",
                "report cannot approve a configuration for live trading.",
                "",
            ]
        if self.is_metrics and self.oos_metrics:
            lines.append(
                f"in-sample     : {self.is_metrics.trades} trades, "
                f"{self.is_metrics.expectancy_r:+.3f}R, "
                f"PF {self.is_metrics.profit_factor:.2f}"
            )
            lines.append(
                f"out-of-sample : {self.oos_metrics.trades} trades, "
                f"{self.oos_metrics.expectancy_r:+.3f}R, "
                f"PF {self.oos_metrics.profit_factor:.2f}"
            )
        for part in (self.expectancy, self.outliers, self.monte_carlo, self.walk_forward,
                     self.plateau):
            if part is not None:
                lines.append(part.summary())
        if self.cost_sensitivity:
            lines.append("cost sensitivity: " + ", ".join(
                f"{k} -> {v:+.3f}R" for k, v in self.cost_sensitivity.items()
            ))
        if self.snooping_penalty:
            lines.append(
                f"data-snooping penalty: the best of the combinations tested would show "
                f"~{self.snooping_penalty:.1f} standard errors of Sharpe from noise alone"
            )
        lines.append("")
        lines.append("CRITERIA")
        for c in self.criteria:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.value} (need {c.threshold}) {c.detail}")
        lines.extend(self.notes)
        lines.append("")
        lines.append(f"VERDICT: {'APPROVED' if self.approved else 'NOT APPROVED'}")
        if self.synthetic:
            lines.append("  (synthetic data can never yield an approval)")
        return "\n".join(lines)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.render(), encoding="utf-8")


#: Thresholds from docs/STRATEGY.md §1.10, fixed before any test is run.
DEFAULT_THRESHOLDS = {
    "min_oos_trades": 250,
    "min_oos_expectancy_r": 0.05,
    "min_oos_profit_factor": 1.15,
    "min_wfe": 0.5,
    "max_mc_p95_dd_r": 25.0,
    "max_plateau_drop": 0.25,
    "min_cost_stress_expectancy_r": 0.0,
}


def build_criteria(report: ValidationReport, thresholds: dict | None = None) -> list[Criterion]:
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: list[Criterion] = []
    m = report.oos_metrics
    if m is not None:
        out.append(Criterion("oos_trades", m.trades, t["min_oos_trades"],
                             m.trades >= t["min_oos_trades"],
                             "below this the confidence interval swallows the result"))
        out.append(Criterion("oos_expectancy_r", round(m.expectancy_r, 4),
                             t["min_oos_expectancy_r"],
                             m.expectancy_r >= t["min_oos_expectancy_r"]))
        out.append(Criterion("oos_profit_factor", round(m.profit_factor, 3),
                             t["min_oos_profit_factor"],
                             m.profit_factor >= t["min_oos_profit_factor"]))
    if report.expectancy is not None:
        out.append(Criterion("expectancy_ci_excludes_zero",
                             f"[{report.expectancy.lo_r:+.3f},{report.expectancy.hi_r:+.3f}]",
                             "excludes 0", not report.expectancy.contains_zero))
    if report.outliers is not None:
        out.append(Criterion("survives_outlier_removal",
                             round(report.outliers.without_top5_r, 2), "> 0",
                             report.outliers.survives,
                             "positive after removing the five best trades"))
    if report.walk_forward is not None:
        out.append(Criterion("walk_forward_efficiency",
                             round(report.walk_forward.efficiency, 3), t["min_wfe"],
                             report.walk_forward.efficiency >= t["min_wfe"]))
    if report.monte_carlo is not None:
        out.append(Criterion("mc_p95_max_drawdown_r",
                             round(report.monte_carlo.p95_max_dd_r, 2), t["max_mc_p95_dd_r"],
                             report.monte_carlo.p95_max_dd_r <= t["max_mc_p95_dd_r"],
                             "size the account for this, not for the historical drawdown"))
    if report.plateau is not None:
        out.append(Criterion("parameter_plateau", round(report.plateau.relative_drop, 3),
                             f"<= {t['max_plateau_drop']}", report.plateau.is_plateau,
                             "neighbouring parameters must also work"))
    if report.cost_sensitivity:
        worst = min(report.cost_sensitivity.values())
        out.append(Criterion("expectancy_at_2x_costs", round(worst, 4),
                             t["min_cost_stress_expectancy_r"],
                             worst >= t["min_cost_stress_expectancy_r"],
                             "spread doubling is a normal condition, not a tail event"))
    out.append(Criterion("data_is_real", "synthetic" if report.synthetic else "real",
                         "real", not report.synthetic,
                         "a strategy tuned on generated data has learned the generator"))
    return out


def assemble_report(
    *,
    label: str,
    data_source: str,
    synthetic: bool,
    is_trades: Sequence[Trade],
    oos_trades: Sequence[Trade],
    walk_forward: WalkForwardResult | None = None,
    plateau: PlateauResult | None = None,
    cost_sensitivity: dict[str, float] | None = None,
    combinations_tested: int = 0,
    starting_equity: float = 10_000.0,
    thresholds: dict | None = None,
) -> ValidationReport:
    report = ValidationReport(
        label=label, data_source=data_source, synthetic=synthetic,
        is_metrics=compute_metrics(is_trades, starting_equity=starting_equity),
        oos_metrics=compute_metrics(oos_trades, starting_equity=starting_equity),
        walk_forward=walk_forward,
        monte_carlo=monte_carlo(oos_trades) if len(oos_trades) >= 5 else None,
        expectancy=expectancy_ci(oos_trades) if len(oos_trades) >= 5 else None,
        outliers=outlier_dependence(oos_trades) if len(oos_trades) > 5 else None,
        plateau=plateau,
        cost_sensitivity=cost_sensitivity or {},
        snooping_penalty=data_snooping_penalty(combinations_tested),
    )
    report.criteria = build_criteria(report, thresholds)
    if report.oos_metrics is not None:
        report.notes.extend(f"  note: {c}" for c in report.oos_metrics.caveats)
    return report


def summarise_grid(
    runner: Callable[[dict], PerformanceMetrics], grid: dict[str, Sequence]
) -> dict[tuple, float]:
    """Evaluate a parameter grid, returning ``{combination: expectancy_r}``.

    The number of combinations is exactly what ``data_snooping_penalty`` needs, which is why
    the grid is enumerated here rather than left implicit in a caller's loop.
    """
    import itertools

    names = list(grid)
    surface: dict[tuple, float] = {}
    for combo in itertools.product(*(grid[n] for n in names)):
        params = dict(zip(names, combo, strict=True))
        surface[combo] = runner(params).expectancy_r
    return surface
