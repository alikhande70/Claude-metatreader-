"""The risk engine.

Nothing reaches a venue without passing through :meth:`RiskEngine.evaluate` (ADR-010). The
engine can veto a strategy signal and can size it; it can never create one. That separation
means a defect in signal logic cannot bypass capital protection, and risk policy can be
changed and audited without touching strategy code.

Checks are ordered cheapest-and-most-fatal first, so a halted system does no work and the
reason a signal was refused is the first genuine reason rather than an incidental one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.core.decision import GateResult
from atlas.core.enums import HaltReason, Side
from atlas.core.instrument import SymbolSpec
from atlas.core.trading import AccountState
from atlas.risk.config import RiskConfig
from atlas.risk.sizing import SizingResult, size_position
from atlas.risk.state import HEALTH_HALTS, RiskState


@dataclass(frozen=True, slots=True)
class OpenExposure:
    """Risk currently live in the market, per position."""

    symbol: str
    side: Side
    risk_money: float  # remaining money at risk between current price and the stop
    group: str = ""


@dataclass(slots=True)
class RiskAssessment:
    approved: bool
    reason_code: str = ""
    detail: str = ""
    volume: float = 0.0
    sizing: SizingResult | None = None
    gates: list[GateResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        state: RiskState | None = None,
        *,
        on_halt=None,
    ) -> None:
        self.cfg = config
        self.state = state or RiskState()
        self._on_halt = on_halt

    # -- account observation ------------------------------------------------------

    def observe(self, account: AccountState, now_ms: int) -> HaltReason | None:
        """Update state from an account snapshot and evaluate the drawdown limits.

        Called on every account update, not only after a trade closes: daily and trailing
        drawdown limits are breached on **floating** loss, so waiting for a close is waiting
        too long.
        """
        cfg = self.cfg
        st = self.state
        rolled = st.roll_day_if_needed(now_ms, account.equity, cfg.daily_reset_tz,
                                       cfg.daily_reset_hour)
        st.observe_equity(now_ms, account.equity, account.balance)
        if rolled:
            st.day_start_equity = account.equity

        # Limits are checked MOST SERIOUS FIRST, and the first breach wins.
        #
        # This ordering is a safety property, not a style choice. Total drawdown is a
        # non-auto-clearing halt; the daily limit clears at the next reset. If a single large
        # loss breaches both and the daily check ran first, the system would halt with an
        # auto-clearing reason and re-arm itself the following day with the *total* limit
        # still breached. Checking the more serious limit first prevents that.
        dd = st.drawdown_pct(account.equity, trailing=cfg.trailing_drawdown)
        if dd >= cfg.total_drawdown_limit_pct:
            basis = "trailing high-water mark" if cfg.trailing_drawdown else "initial balance"
            return self._trip(
                HaltReason.TOTAL_DRAWDOWN_LIMIT,
                f"drawdown {dd:.2f}% from the {basis} reached the "
                f"{cfg.total_drawdown_limit_pct:.2f}% halt threshold",
                now_ms,
            )

        if st.consecutive_losses >= cfg.max_consecutive_losses:
            return self._trip(
                HaltReason.CONSECUTIVE_LOSSES,
                f"{st.consecutive_losses} consecutive losing trades reached the limit of "
                f"{cfg.max_consecutive_losses}",
                now_ms,
            )

        daily = st.daily_loss_pct(account.equity)
        if daily >= cfg.daily_loss_limit_pct:
            return self._trip(
                HaltReason.DAILY_LOSS_LIMIT,
                f"daily loss {daily:.2f}% reached the {cfg.daily_loss_limit_pct:.2f}% halt "
                f"threshold (equity {account.equity:.2f} vs day start "
                f"{st.day_start_equity:.2f})",
                now_ms,
            )
        return None

    def report_health(self, *, venue_ok: bool, data_fresh: bool, now_ms: int) -> HaltReason | None:
        """Health-driven halts. Unlike risk halts these clear as soon as the cause does."""
        if not venue_ok:
            return self._trip(HaltReason.VENUE_UNAVAILABLE, "venue is not reachable", now_ms)
        if not data_fresh:
            return self._trip(HaltReason.STALE_MARKET_DATA,
                              "market data is older than the staleness tolerance", now_ms)
        if self.state.halted and self.state.halt_reason in HEALTH_HALTS:
            self.state.clear(now_ms, operator="health-check")
        return None

    def on_trade_closed(self, net_profit: float, now_ms: int) -> HaltReason | None:
        self.state.record_trade_result(net_profit)
        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            return self._trip(
                HaltReason.CONSECUTIVE_LOSSES,
                f"{self.state.consecutive_losses} consecutive losing trades",
                now_ms,
            )
        return None

    def halt(self, reason: HaltReason, detail: str, now_ms: int) -> HaltReason | None:
        return self._trip(reason, detail, now_ms)

    def resume(self, now_ms: int, operator: str = "operator") -> bool:
        return self.state.clear(now_ms, operator)

    def _trip(self, reason: HaltReason, detail: str, now_ms: int) -> HaltReason | None:
        if self.state.trip(reason, detail, now_ms):
            if self._on_halt is not None:
                self._on_halt(reason, detail)
            return reason
        return None

    # -- pre-trade evaluation ------------------------------------------------------

    def evaluate(
        self,
        *,
        symbol: str,
        spec: SymbolSpec,
        side: Side,
        stop_points: float,
        account: AccountState,
        open_exposure: list[OpenExposure],
        conviction: float | None = None,
        now_ms: int = 0,
    ) -> RiskAssessment:
        cfg = self.cfg
        st = self.state
        gates: list[GateResult] = []

        def fail(code: str, detail: str) -> RiskAssessment:
            return RiskAssessment(approved=False, reason_code=code, detail=detail, gates=gates)

        # 1. kill switch -- cheapest and most fatal
        halt_detail = f"{st.halt_reason}: {st.halt_detail}" if st.halted else "armed"
        gates.append(GateResult(name="kill_switch", passed=not st.halted, detail=halt_detail))
        if st.halted:
            return fail("HALTED", f"kill switch active: {st.halt_reason} -- {st.halt_detail}")

        gates.append(GateResult(
            name="trading_enabled",
            passed=cfg.allow_new_trades and account.trade_allowed,
            detail=f"config={cfg.allow_new_trades} broker={account.trade_allowed}",
        ))
        if not cfg.allow_new_trades:
            return fail("TRADING_DISABLED", "new trades are disabled in the risk configuration")
        if not account.trade_allowed:
            return fail("BROKER_TRADING_DISABLED", "the broker reports trading is not allowed")

        # 2. position count caps
        same_symbol = [e for e in open_exposure if e.symbol == symbol]
        gates.append(GateResult(name="max_positions_per_symbol",
                                passed=len(same_symbol) < cfg.max_positions_per_symbol,
                                value=len(same_symbol), threshold=cfg.max_positions_per_symbol,
                                comparison="<"))
        if len(same_symbol) >= cfg.max_positions_per_symbol:
            return fail("SYMBOL_POSITION_LIMIT",
                        f"{len(same_symbol)} position(s) already open on {symbol}")
        gates.append(GateResult(name="max_positions", passed=len(open_exposure) < cfg.max_positions,
                                value=len(open_exposure), threshold=cfg.max_positions,
                                comparison="<"))
        if len(open_exposure) >= cfg.max_positions:
            return fail("POSITION_LIMIT",
                        f"{len(open_exposure)} positions already open (limit {cfg.max_positions})")

        # 3. sizing
        if account.equity <= 0:
            return fail("NO_EQUITY", "account equity is zero or unknown")
        sizing = size_position(
            spec, account.equity, cfg.risk_per_trade_pct, stop_points,
            leverage=account.leverage or 100,
            conviction=conviction if cfg.conviction_scaling else None,
            min_fraction=cfg.min_risk_fraction,
        )
        gates.append(GateResult(name="min_lot", passed=sizing.tradeable,
                                value=round(sizing.ideal_volume, 4), threshold=spec.volume_min,
                                comparison=">=",
                                detail=f"ideal {sizing.ideal_volume:.4f} lots vs broker minimum "
                                       f"{spec.volume_min}"))
        if not sizing.tradeable:
            min_risk = spec.money_for_points(stop_points, spec.volume_min)
            return fail(
                "BELOW_MIN_LOT",
                f"risk budget of {sizing.intended_risk_money:.2f} over a {stop_points:.0f}-pt "
                f"stop needs {sizing.ideal_volume:.4f} lots, below the {spec.volume_min} "
                f"minimum. Trading the minimum lot would risk {min_risk:.2f}.",
            )

        drift_ok = sizing.drift <= cfg.max_size_drift
        gates.append(GateResult(name="size_drift", passed=drift_ok, value=round(sizing.drift, 4),
                                threshold=cfg.max_size_drift, comparison="<=",
                                detail=f"lot rounding leaves {sizing.risk_money:.2f} at risk vs "
                                       f"an intended {sizing.intended_risk_money:.2f}"))
        if not drift_ok:
            return fail("SIZE_DRIFT",
                        f"lot step is too coarse for this account: realised risk "
                        f"{sizing.risk_money:.2f} is {sizing.drift:.0%} below the intended "
                        f"{sizing.intended_risk_money:.2f}")

        # 4. aggregate and correlated exposure
        total_open = sum(e.risk_money for e in open_exposure)
        total_after = total_open + sizing.risk_money
        total_pct = total_after / account.equity * 100.0
        gates.append(GateResult(name="total_open_risk", passed=total_pct <= cfg.max_total_risk_pct,
                                value=round(total_pct, 3), threshold=cfg.max_total_risk_pct,
                                comparison="<=",
                                detail=f"{len(open_exposure)} open position(s) risking "
                                       f"{total_open:.2f} + {sizing.risk_money:.2f} new"))
        if total_pct > cfg.max_total_risk_pct:
            return fail("TOTAL_RISK_LIMIT",
                        f"total open risk would reach {total_pct:.2f}% (limit "
                        f"{cfg.max_total_risk_pct}%)")

        group = cfg.group_of(symbol)
        group_open = sum(
            e.risk_money for e in open_exposure
            if (e.group or cfg.group_of(e.symbol)) == group
        )
        group_pct = (group_open + sizing.risk_money) / account.equity * 100.0
        gates.append(GateResult(name="group_risk", passed=group_pct <= cfg.max_group_risk_pct,
                                value=round(group_pct, 3), threshold=cfg.max_group_risk_pct,
                                comparison="<=",
                                detail=f"correlation group {group!r} -- correlated instruments "
                                       f"count as one exposure"))
        if group_pct > cfg.max_group_risk_pct:
            return fail("GROUP_RISK_LIMIT",
                        f"correlated exposure in group {group!r} would reach {group_pct:.2f}% "
                        f"(limit {cfg.max_group_risk_pct}%)")

        # 5. margin headroom
        free_after = account.free_margin - sizing.margin_required
        free_pct = (free_after / account.equity * 100.0) if account.equity > 0 else 0.0
        margin_ok = free_pct >= cfg.min_free_margin_pct
        gates.append(GateResult(name="free_margin", passed=margin_ok, value=round(free_pct, 2),
                                threshold=cfg.min_free_margin_pct, comparison=">=",
                                detail=f"margin required {sizing.margin_required:.2f}, free "
                                       f"margin {account.free_margin:.2f}"))
        if not margin_ok:
            return fail("INSUFFICIENT_MARGIN",
                        f"free margin after this trade would be {free_pct:.1f}% of equity "
                        f"(minimum {cfg.min_free_margin_pct}%)")

        notes = []
        headroom = cfg.daily_loss_limit_pct - st.daily_loss_pct(account.equity)
        losers_left = headroom / max(sizing.risk_pct, 1e-9)
        notes.append(
            f"{losers_left:.1f} further full-size losses would reach today's halt threshold"
        )
        if sizing.drift > 0.1:
            notes.append(f"lot rounding gives up {sizing.drift:.0%} of the intended risk")
        return RiskAssessment(approved=True, reason_code="APPROVED", volume=sizing.volume,
                              sizing=sizing, gates=gates, notes=notes)
