"""Performance metrics.

Everything that can be is expressed in **R** -- multiples of the risk actually taken at entry.
Currency P/L is not comparable across position sizes or account sizes, and an equity curve
denominated in dollars hides whether the system got better or the position sizing got bigger.

Realistic scale, stated plainly because it governs how these numbers should be read: a
genuinely working retail system looks like an expectancy of +0.05 to +0.20 R and a profit
factor of 1.2 to 1.4, with drawdowns that are uncomfortable. A profit factor of 3 on a
backtest is evidence of a bug -- look-ahead, missing costs, or a fill assumption -- not of an
edge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

import numpy as np

from atlas.core.trading import Trade


@dataclass(slots=True)
class PerformanceMetrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    expectancy_r: float = 0.0
    expectancy_money: float = 0.0
    total_r: float = 0.0
    net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    payoff_ratio: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    largest_win_r: float = 0.0
    largest_loss_r: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_money: float = 0.0
    max_drawdown_r: float = 0.0
    longest_losing_streak: int = 0
    longest_flat_trades: int = 0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    sqn: float = 0.0
    recovery_factor: float = 0.0
    total_commission: float = 0.0
    total_swap: float = 0.0
    cost_share_of_gross: float = 0.0
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0
    exit_reasons: dict[str, int] = field(default_factory=dict)
    #: Set when the sample is too small for the numbers above to mean anything.
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


#: Below this many trades, no performance conclusion is defensible. Reported, never hidden.
MIN_MEANINGFUL_TRADES = 100


def compute_metrics(
    trades: Sequence[Trade],
    *,
    starting_equity: float = 10_000.0,
    equity_curve: Sequence[tuple[int, float, float]] | None = None,
    bars_per_year: float = 252 * 24,
) -> PerformanceMetrics:
    m = PerformanceMetrics()
    if not trades:
        m.caveats.append("no trades: nothing to measure")
        return m

    r = np.array([t.r_multiple for t in trades], dtype=float)
    pnl = np.array([t.net_profit for t in trades], dtype=float)
    m.trades = len(trades)
    m.wins = int((pnl > 0).sum())
    m.losses = int((pnl < 0).sum())
    m.win_rate = m.wins / m.trades
    m.total_r = float(r.sum())
    m.expectancy_r = float(r.mean())
    m.expectancy_money = float(pnl.mean())
    m.net_profit = float(pnl.sum())
    m.gross_profit = float(pnl[pnl > 0].sum())
    m.gross_loss = float(-pnl[pnl < 0].sum())
    m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else float("inf")

    wins_r, losses_r = r[pnl > 0], r[pnl < 0]
    m.avg_win_r = float(wins_r.mean()) if len(wins_r) else 0.0
    m.avg_loss_r = float(losses_r.mean()) if len(losses_r) else 0.0
    m.payoff_ratio = abs(m.avg_win_r / m.avg_loss_r) if m.avg_loss_r else float("inf")
    m.largest_win_r = float(r.max())
    m.largest_loss_r = float(r.min())

    m.total_commission = float(sum(t.commission for t in trades))
    m.total_swap = float(sum(t.swap for t in trades))
    gross = float(sum(t.gross_profit for t in trades))
    costs = abs(m.total_commission) + abs(min(0.0, m.total_swap))
    m.cost_share_of_gross = (costs / abs(gross)) if gross else 0.0

    m.avg_mae_r = float(np.mean([t.mae_r for t in trades]))
    m.avg_mfe_r = float(np.mean([t.mfe_r for t in trades]))

    reasons: dict[str, int] = {}
    for t in trades:
        key = str(t.exit_reason)
        reasons[key] = reasons.get(key, 0) + 1
    m.exit_reasons = reasons

    # Drawdown is computed on the trade-by-trade equity path. When a tick-level equity curve
    # is supplied it is used instead, because intra-trade floating drawdown is what actually
    # breaches a limit.
    if equity_curve:
        eq = np.array([e for _, e, _ in equity_curve], dtype=float)
    else:
        eq = starting_equity + np.cumsum(pnl)
        eq = np.insert(eq, 0, starting_equity)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    m.max_drawdown_money = float(dd.max())
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(peak > 0, dd / peak * 100.0, 0.0)
    m.max_drawdown_pct = float(dd_pct.max())

    r_curve = np.insert(np.cumsum(r), 0, 0.0)
    r_peak = np.maximum.accumulate(r_curve)
    m.max_drawdown_r = float((r_peak - r_curve).max())

    m.longest_losing_streak = _longest_streak(pnl < 0)
    m.longest_flat_trades = _longest_flat(np.cumsum(pnl))

    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    m.sharpe = (m.expectancy_r / sd * np.sqrt(len(r))) if sd > 0 else 0.0
    downside = r[r < 0]
    dsd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    m.sortino = (m.expectancy_r / dsd * np.sqrt(len(r))) if dsd > 0 else 0.0
    m.sqn = (np.sqrt(len(r)) * m.expectancy_r / sd) if sd > 0 else 0.0
    m.calmar = (m.total_r / m.max_drawdown_r) if m.max_drawdown_r > 0 else 0.0
    m.recovery_factor = (m.net_profit / m.max_drawdown_money) if m.max_drawdown_money > 0 else 0.0

    if m.trades < MIN_MEANINGFUL_TRADES:
        m.caveats.append(
            f"{m.trades} trades is below the {MIN_MEANINGFUL_TRADES}-trade floor; these "
            f"statistics are descriptive, not evidence of an edge"
        )
    if m.profit_factor > 2.5 and m.trades > 20:
        m.caveats.append(
            f"profit factor {m.profit_factor:.2f} is implausibly high for a retail system; "
            f"check for look-ahead, missing costs, or an optimistic fill assumption"
        )
    if m.cost_share_of_gross > 0.3:
        m.caveats.append(
            f"costs consume {m.cost_share_of_gross:.0%} of gross profit; the strategy is "
            f"cost-constrained and needs a wider stop or lower frequency"
        )
    return m


def _longest_streak(flags: np.ndarray) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _longest_flat(cum: np.ndarray) -> int:
    """Longest run of trades without making a new equity high."""
    best = cur = 0
    peak = -np.inf
    for v in cum:
        if v > peak:
            peak = v
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def r_series(trades: Sequence[Trade]) -> np.ndarray:
    return np.array([t.r_multiple for t in trades], dtype=float)


def summarise(m: PerformanceMetrics) -> str:
    """One-screen human summary, with the caveats attached rather than buried."""
    lines = [
        f"trades {m.trades}  win rate {m.win_rate:.1%}  expectancy {m.expectancy_r:+.3f}R",
        f"profit factor {m.profit_factor:.2f}  payoff {m.payoff_ratio:.2f}  SQN {m.sqn:.2f}",
        f"total {m.total_r:+.1f}R  net {m.net_profit:+.2f}  max DD {m.max_drawdown_pct:.1f}% "
        f"({m.max_drawdown_r:.1f}R)",
        f"longest losing streak {m.longest_losing_streak}  flat for {m.longest_flat_trades} trades",
        f"costs: commission {m.total_commission:.2f} swap {m.total_swap:.2f} "
        f"({m.cost_share_of_gross:.0%} of gross)",
        f"exits: {m.exit_reasons}",
    ]
    lines.extend(f"CAVEAT: {c}" for c in m.caveats)
    return "\n".join(lines)
