"""DC1 -- Donchian breakout baseline.

Three parameters, one idea, no structure analysis. Its purpose is **diagnostic**, not
profitable: it is the control group. Reporting SM1's numbers without DC1's beside them invites
the reader to credit SM1's machinery for what may simply be the market trending.

If SM1 does not beat DC1 out of sample and net of costs, SM1's extra fifteen parameters are
fitting noise and the honest conclusion is to delete them.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np

from atlas.core.decision import (
    DecisionRecord,
    EvidenceItem,
    GateResult,
    ProposedTrade,
    make_decision_id,
)
from atlas.core.enums import DecisionOutcome, Regime, Side, Timeframe
from atlas.strategy.base import Strategy, StrategyContext


@dataclass(frozen=True, slots=True)
class DC1Params:
    tf: Timeframe = Timeframe.H1
    channel: int = 20
    stop_atr: float = 2.0
    rr_target: float = 2.0
    spread_max_atr: float = 0.35


class DonchianBreakout(Strategy):
    name = "DC1"
    version = "1.0"

    def __init__(self, params: DC1Params | None = None) -> None:
        self.p = params or DC1Params()

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        return (self.p.tf,)

    @property
    def trigger_timeframe(self) -> Timeframe:
        return self.p.tf

    def parameters(self) -> dict[str, float | int | str]:
        return {
            f.name: (str(v) if isinstance(v := getattr(self.p, f.name), Timeframe) else v)
            for f in fields(self.p)
        }

    def evaluate(self, ctx: StrategyContext) -> DecisionRecord:
        p = self.p
        did = make_decision_id(ctx.symbol, self.name, ctx.now_ms, self.version)
        gates: list[GateResult] = []

        def record(outcome, code, detail="", **kw) -> DecisionRecord:
            return DecisionRecord(
                decision_id=did, ts=ctx.now_ms, symbol=ctx.symbol, strategy=self.name,
                strategy_version=self.version, outcome=outcome, reason_code=code,
                reason_detail=detail, gates=tuple(gates),
                features=ctx.features.values() if ctx.features.frames else {}, **kw,
            )

        f = ctx.features.get(p.tf)
        ready = f is not None and f.ready()
        gates.append(GateResult(name="features_ready", passed=ready))
        if not ready or f is None:
            return record(DecisionOutcome.NO_SETUP, "NOT_READY")

        if ctx.position is not None:
            gates.append(GateResult(name="no_open_position", passed=False))
            return record(DecisionOutcome.SUPPRESSED, "POSITION_OPEN")

        atr_points = float(f.atr[-1]) / ctx.spec.point
        limit = p.spread_max_atr * atr_points
        gates.append(GateResult(name="spread", passed=ctx.spread_points <= limit,
                                value=round(ctx.spread_points, 1), threshold=round(limit, 1),
                                comparison="<="))
        if ctx.spread_points > limit:
            return record(DecisionOutcome.VETOED, "SPREAD_TOO_WIDE",
                          f"{ctx.spread_points:.0f} pts > {limit:.0f} pts")

        in_session = ctx.calendar.in_session(ctx.now_ms, ctx.allowed_sessions)
        gates.append(GateResult(name="session", passed=in_session))
        if not in_session:
            return record(DecisionOutcome.VETOED, "OUT_OF_SESSION")
        if ctx.calendar.is_rollover(ctx.now_ms):
            gates.append(GateResult(name="rollover", passed=False))
            return record(DecisionOutcome.VETOED, "ROLLOVER_WINDOW")

        close = float(f.close[-1])
        hi, lo = float(f.donchian_high[-1]), float(f.donchian_low[-1])
        if np.isnan(hi) or np.isnan(lo):
            return record(DecisionOutcome.NO_SETUP, "NOT_READY")

        if close > hi:
            side = Side.BUY
        elif close < lo:
            side = Side.SELL
        else:
            gates.append(GateResult(
                name="breakout", passed=False, value=close,
                detail=f"inside the {p.channel}-bar channel [{lo:.5f}, {hi:.5f}]",
            ))
            return record(DecisionOutcome.NO_SETUP, "NO_BREAKOUT")
        gates.append(GateResult(name="breakout", passed=True, value=close,
                                threshold=hi if side is Side.BUY else lo,
                                comparison=">" if side is Side.BUY else "<"))

        spec = ctx.spec
        entry = spec.normalize_price(ctx.quote.price_for(str(side)))
        stop_dist = p.stop_atr * float(f.atr[-1])
        sl = spec.normalize_price(entry - side.sign * stop_dist)
        stop_points = spec.points_between(entry, sl)
        ok = stop_points >= spec.min_stop_distance_points(5)
        gates.append(GateResult(name="broker_stop_level", passed=ok, value=round(stop_points, 1),
                                threshold=spec.min_stop_distance_points(5), comparison=">="))
        if not ok:
            return record(DecisionOutcome.VETOED, "STOP_TOO_TIGHT")
        tp = spec.normalize_price(entry + side.sign * stop_points * spec.point * p.rr_target)

        return record(
            DecisionOutcome.SIGNAL, "BREAKOUT",
            f"{side} break of the {p.channel}-bar {p.tf} channel",
            regime=Regime.UNKNOWN, bias=side, conviction=0.5,
            evidence=(EvidenceItem(name="breakout", score=1.0, weight=1.0,
                                   detail="baseline strategy scores every signal identically"),),
            proposal=ProposedTrade(
                side=side, entry_price=entry, stop_loss=sl, take_profit=tp,
                stop_points=round(stop_points, 1), reward_risk=p.rr_target,
                rationale=f"{p.stop_atr} x ATR stop",
            ),
        )
