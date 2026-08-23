"""SM1 -- Structure Momentum Pullback.

Implements the specification in ``docs/STRATEGY.md`` §1. Where this code and that document
disagree, one of them is a bug; the document is the authority.

Reading order matches the decision funnel, and each stage appends to the ``DecisionRecord``
so that a rejection is never anonymous:

    readiness -> HTF bias -> regime -> hard vetoes -> MTF setup -> location ->
    LTF trigger -> stop construction -> evidence scoring -> signal
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

import numpy as np

from atlas.core.decision import (
    DecisionRecord,
    EvidenceItem,
    GateResult,
    ProposedTrade,
    make_decision_id,
)
from atlas.core.enums import DecisionOutcome, ExitReason, Side, StructureEvent, Timeframe
from atlas.core.errors import ConfigError
from atlas.data.calendar import currencies_of
from atlas.features.frame import FeatureFrame
from atlas.strategy.base import ManagementAction, Strategy, StrategyContext
from atlas.strategy.regime import RegimeConfig, classify


@dataclass(frozen=True, slots=True)
class SM1Params:
    """See docs/STRATEGY.md §1.8 for which of these may be optimised and which are frozen."""

    htf: Timeframe = Timeframe.H4
    mtf: Timeframe = Timeframe.H1
    ltf: Timeframe = Timeframe.M15

    # --- optimisable (swept in walk-forward) ---
    adx_trend_min: float = 22.0
    location_max: float = 0.55
    rr_target: float = 2.0
    sl_buffer_atr: float = 0.35
    be_at_r: float = 1.0

    # --- frozen by structural rationale ---
    er_trend_min: float = 0.35
    atr_pct_max: float = 0.95
    body_min: float = 0.45
    setup_lookback: int = 8
    #: How many LTF bars back the retrace-depth measurement looks. See the note on the
    #: location gate in ``evaluate``: measuring depth only at the trigger bar asks a
    #: self-contradictory question.
    arm_bars: int = 12
    min_stop_atr: float = 0.5
    max_stop_atr: float = 4.0
    stop_safety_points: int = 5
    spread_max_atr: float = 0.35
    trail_after_r: float = 1.5
    trail_atr: float = 1.5
    time_stop_bars: int = 24
    news_before_min: int = 15
    news_after_min: int = 15

    # --- evidence weights (chosen, never swept) ---
    w_htf: float = 2.0
    w_location: float = 1.5
    w_momentum: float = 1.5
    w_imbalance: float = 1.0
    w_liquidity: float = 1.0
    w_volfit: float = 1.0

    def __post_init__(self) -> None:
        # A degenerate ladder (two roles on the same timeframe) silently turns a
        # multi-timeframe strategy into a single-timeframe one that still *reports* itself as
        # multi-timeframe. Reject it rather than let a config typo change the strategy.
        if not (self.htf.seconds > self.mtf.seconds > self.ltf.seconds):
            raise ConfigError(
                f"SM1 timeframe ladder must be strictly descending, got "
                f"htf={self.htf} mtf={self.mtf} ltf={self.ltf}"
            )
        ratio_hm = self.htf.seconds / self.mtf.seconds
        ratio_ml = self.mtf.seconds / self.ltf.seconds
        if ratio_hm < 3 or ratio_ml < 3:
            raise ConfigError(
                f"SM1 timeframe ladder steps must be at least 3x (got {ratio_hm:.1f}x and "
                f"{ratio_ml:.1f}x); adjacent timeframes carry nearly the same information"
            )


class StructureMomentum(Strategy):
    name = "SM1"
    version = "1.0"

    def __init__(self, params: SM1Params | None = None) -> None:
        self.p = params or SM1Params()

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        return (self.p.htf, self.p.mtf, self.p.ltf)

    @property
    def trigger_timeframe(self) -> Timeframe:
        return self.p.ltf

    def parameters(self) -> dict[str, float | int | str]:
        return {
            f.name: (str(v) if isinstance(v := getattr(self.p, f.name), Timeframe) else v)
            for f in fields(self.p)
        }

    # -- evaluation -------------------------------------------------------------

    def evaluate(self, ctx: StrategyContext) -> DecisionRecord:
        p = self.p
        did = make_decision_id(ctx.symbol, self.name, ctx.now_ms, self.version)
        gates: list[GateResult] = []

        def record(
            outcome: DecisionOutcome, code: str, detail: str = "", **kw
        ) -> DecisionRecord:
            return DecisionRecord(
                decision_id=did, ts=ctx.now_ms, symbol=ctx.symbol, strategy=self.name,
                strategy_version=self.version, outcome=outcome, reason_code=code,
                reason_detail=detail, gates=tuple(gates),
                features=ctx.features.values() if ctx.features.frames else {},
                **kw,
            )

        # --- readiness ---------------------------------------------------------
        missing = [
            tf for tf in self.timeframes
            if (f := ctx.features.get(tf)) is None or not f.ready()
        ]
        warm = f"warming up: {[str(t) for t in missing]}" if missing else "ok"
        gates.append(GateResult(name="features_ready", passed=not missing, detail=warm))
        if missing:
            return record(DecisionOutcome.NO_SETUP, "NOT_READY",
                          f"timeframes still warming up: {[str(t) for t in missing]}")

        htf = ctx.features.require(p.htf)
        mtf = ctx.features.require(p.mtf)
        ltf = ctx.features.require(p.ltf)

        # --- already in a position ---------------------------------------------
        if ctx.position is not None:
            gates.append(GateResult(name="no_open_position", passed=False,
                                    detail=f"ticket {ctx.position.ticket} open"))
            return record(DecisionOutcome.SUPPRESSED, "POSITION_OPEN",
                          "SM1 does not pyramid or reverse; see docs/STRATEGY.md §1.7")

        # --- HTF bias ----------------------------------------------------------
        bias = self._bias(htf)
        htf_state = htf.structure.last().break_state
        gates.append(GateResult(
            name="htf_bias", passed=bias is not None,
            value={"BULL": 1.0, "BEAR": -1.0}.get(htf_state, 0.0),
            detail=f"{p.htf} break_state={htf_state} close={htf.close[-1]:.5f} "
                   f"ema_slow={htf.ema_slow[-1]:.5f}",
        ))
        if bias is None:
            return record(DecisionOutcome.NO_SETUP, "NO_HTF_BIAS",
                          "higher timeframe structure and trend filter disagree or are neutral")

        # --- regime ------------------------------------------------------------
        reg = classify(mtf, RegimeConfig(adx_trend_min=p.adx_trend_min,
                                         er_trend_min=p.er_trend_min,
                                         atr_pct_max=p.atr_pct_max))
        gates.append(GateResult(name="vol_shock", passed=not reg.is_shock,
                                value=reg.atr_pct, threshold=p.atr_pct_max, comparison="<=",
                                detail=reg.detail))
        if reg.is_shock:
            return record(DecisionOutcome.VETOED, "VOL_SHOCK", reg.detail,
                          regime=reg.regime, bias=bias)
        gates.append(GateResult(name="trend_regime", passed=reg.is_trending, value=reg.adx,
                                threshold=p.adx_trend_min, comparison=">=", detail=reg.detail,
                                hard=False))
        if not reg.is_trending:
            return record(DecisionOutcome.NO_SETUP, "RANGE_REGIME", reg.detail,
                          regime=reg.regime, bias=bias)

        # --- hard vetoes: cost and timing --------------------------------------
        veto = self._hard_vetoes(ctx, ltf, gates)
        if veto is not None:
            return record(DecisionOutcome.VETOED, veto[0], veto[1], regime=reg.regime, bias=bias)

        # --- MTF setup: is the leg live and is price retracing into it? ---------
        setup_ok, setup_detail = self._mtf_setup(mtf, bias)
        gates.append(GateResult(name="mtf_setup", passed=setup_ok, detail=setup_detail))
        if not setup_ok:
            return record(DecisionOutcome.NO_SETUP, "NO_MTF_SETUP", setup_detail,
                          regime=reg.regime, bias=bias)

        # Location: how deep did the retrace actually get?
        #
        # This deliberately measures the *deepest point reached within the arming window*,
        # not the position at the trigger bar. Those are different questions, and the second
        # one is self-contradictory: a momentum trigger fires precisely when price is leaving
        # the retrace, so by then it has already climbed out of discount. Measured on 12,000
        # bars of test data, location at the trigger bar has a median of 0.73 while the
        # deepest point in the preceding 12 bars has a median of 0.19 -- the original rule was
        # asking for something that essentially never coincides with its own trigger.
        snap = mtf.structure.last()
        lo_i = max(0, ltf.n - p.arm_bars)
        if bias is Side.BUY:
            extreme = float(np.min(ltf.low[lo_i:ltf.n]))
            loc_value = snap.position_in_range(extreme)
        else:
            extreme = float(np.max(ltf.high[lo_i:ltf.n]))
            loc_value = 1.0 - snap.position_in_range(extreme)
        loc_ok = loc_value <= p.location_max
        half = "discount" if bias is Side.BUY else "premium"
        gates.append(GateResult(name="location", passed=loc_ok, value=round(loc_value, 4),
                                threshold=p.location_max, comparison="<=",
                                detail=f"deepest retrace over {p.arm_bars} {p.ltf} bars "
                                       f"reached {loc_value:.2f} into the {half} half "
                                       f"(price {extreme:.5f})"))
        if not loc_ok:
            return record(DecisionOutcome.NO_SETUP, "BAD_LOCATION",
                          f"deepest retrace only reached {loc_value:.2f} into the {half} half "
                          f"(limit {p.location_max})",
                          regime=reg.regime, bias=bias)

        # --- LTF trigger --------------------------------------------------------
        trig_ok, trig_detail = self._ltf_trigger(ltf, bias)
        gates.append(GateResult(name="ltf_trigger", passed=trig_ok, detail=trig_detail))
        if not trig_ok:
            return record(DecisionOutcome.NO_SETUP, "NO_TRIGGER", trig_detail,
                          regime=reg.regime, bias=bias)

        # --- stop construction ---------------------------------------------------
        proposal, stop_gates, stop_err = self._build_trade(ctx, mtf, ltf, bias)
        gates.extend(stop_gates)
        if proposal is None:
            return record(DecisionOutcome.VETOED, stop_err or "STOP_INVALID", stop_err or "",
                          regime=reg.regime, bias=bias)

        # --- evidence ------------------------------------------------------------
        evidence = self._score(ctx, htf, mtf, ltf, bias, reg, loc_value, proposal)
        total_w = sum(e.weight for e in evidence) or 1.0
        conviction = float(np.clip(sum(e.contribution for e in evidence) / total_w, 0.0, 1.0))

        return record(
            DecisionOutcome.SIGNAL, "SETUP_CONFIRMED",
            f"{bias} pullback continuation in {reg.regime}",
            regime=reg.regime, bias=bias, conviction=conviction,
            evidence=tuple(evidence), proposal=proposal,
        )

    # -- stages -----------------------------------------------------------------

    def _bias(self, htf: FeatureFrame) -> Side | None:
        state = htf.structure.last().break_state
        close = float(htf.close[-1])
        ema = float(htf.ema_slow[-1])
        if state == "BULL" and close > ema:
            return Side.BUY
        if state == "BEAR" and close < ema:
            return Side.SELL
        return None

    def _hard_vetoes(
        self, ctx: StrategyContext, ltf: FeatureFrame, gates: list[GateResult]
    ) -> tuple[str, str] | None:
        p = self.p
        cal = ctx.calendar

        atr_points = float(ltf.atr[-1]) / ctx.spec.point
        spread_limit = p.spread_max_atr * atr_points
        spread = ctx.spread_points
        gates.append(GateResult(name="spread", passed=spread <= spread_limit,
                                value=round(spread, 1), threshold=round(spread_limit, 1),
                                comparison="<=",
                                detail=f"{spread:.0f} pts vs {p.spread_max_atr}x ATR"))
        if spread > spread_limit:
            return ("SPREAD_TOO_WIDE",
                    f"spread {spread:.0f} pts exceeds {spread_limit:.0f} pts "
                    f"({p.spread_max_atr}x the {atr_points:.0f}-pt ATR)")

        in_session = cal.in_session(ctx.now_ms, ctx.allowed_sessions)
        gates.append(GateResult(name="session", passed=in_session,
                                detail=f"active={list(cal.active_sessions(ctx.now_ms))} "
                                       f"allowed={list(ctx.allowed_sessions)}"))
        if not in_session:
            return ("OUT_OF_SESSION", f"outside {list(ctx.allowed_sessions)}")

        rollover = cal.is_rollover(ctx.now_ms)
        gates.append(GateResult(name="rollover", passed=not rollover,
                                detail="within +/-60 min of broker server midnight"
                                       if rollover else "ok"))
        if rollover:
            return ("ROLLOVER_WINDOW", "spreads are unreliable around server midnight")

        if not cal.news.available:
            blocked = ctx.news_policy == "block"
            gates.append(GateResult(name="news_calendar", passed=not blocked,
                                    detail=f"calendar unavailable, policy={ctx.news_policy}"))
            if blocked:
                return ("NEWS_CALENDAR_UNAVAILABLE",
                        "no economic calendar loaded and news_policy is 'block'")
        else:
            ev = cal.news_blackout(ctx.now_ms, currencies_of(ctx.symbol),
                                   before_minutes=p.news_before_min,
                                   after_minutes=p.news_after_min)
            gates.append(GateResult(name="news_window", passed=ev is None,
                                    detail=f"{ev.title} ({ev.currency})" if ev else "clear"))
            if ev is not None:
                return ("NEWS_WINDOW", f"high-impact {ev.currency} event: {ev.title}")
        return None

    def _mtf_setup(self, mtf: FeatureFrame, bias: Side) -> tuple[bool, str]:
        p = self.p
        snap = mtf.structure.last()
        want = "BULL" if bias is Side.BUY else "BEAR"
        if snap.break_state != want:
            return False, f"{p.mtf} break_state {snap.break_state} does not match {bias} bias"
        wanted_events = (
            (StructureEvent.BOS_UP, StructureEvent.CHOCH_UP) if bias is Side.BUY
            else (StructureEvent.BOS_DOWN, StructureEvent.CHOCH_DOWN)
        )
        lo = max(0, mtf.n - p.setup_lookback)
        recent = [mtf.structure.event[i] for i in range(lo, mtf.n)]
        hit = [e for e in recent if e in wanted_events]
        if not hit:
            return False, f"no {bias} structural break in the last {p.setup_lookback} {p.mtf} bars"
        return True, f"{hit[-1]} within {p.setup_lookback} {p.mtf} bars"

    def _ltf_trigger(self, ltf: FeatureFrame, bias: Side) -> tuple[bool, str]:
        p = self.p
        ev = ltf.structure.last().event
        wanted = (
            (StructureEvent.BOS_UP, StructureEvent.CHOCH_UP) if bias is Side.BUY
            else (StructureEvent.BOS_DOWN, StructureEvent.CHOCH_DOWN)
        )
        if ev not in wanted:
            return False, f"{p.ltf} event {ev} is not a {bias} trigger"
        body = float(ltf.body_ratio[-1])
        if body < p.body_min:
            return False, f"trigger bar body {body:.2f} < {p.body_min} (indecisive candle)"
        return True, f"{ev} with body ratio {body:.2f}"

    def _build_trade(
        self, ctx: StrategyContext, mtf: FeatureFrame, ltf: FeatureFrame, bias: Side
    ) -> tuple[ProposedTrade | None, list[GateResult], str | None]:
        p = self.p
        spec = ctx.spec
        gates: list[GateResult] = []
        entry = spec.normalize_price(ctx.quote.price_for(str(bias)))

        snap = mtf.structure.last()
        anchor = snap.last_low if bias is Side.BUY else snap.last_high
        if anchor is None:
            return None, gates, "NO_STRUCTURAL_STOP"
        buffer = max(2.0 * ctx.quote.spread, p.sl_buffer_atr * float(ltf.atr[-1]))
        raw_sl = anchor.price - buffer if bias is Side.BUY else anchor.price + buffer
        sl = spec.normalize_price(raw_sl)

        # The stop must be on the correct side of entry. A structural level that has already
        # been passed produces an inverted stop, which must be rejected rather than clamped.
        if (bias is Side.BUY and sl >= entry) or (bias is Side.SELL and sl <= entry):
            gates.append(GateResult(name="stop_side", passed=False, value=sl, threshold=entry,
                                    detail="structural level is already beyond entry"))
            return None, gates, "STOP_INVERTED"

        stop_points = spec.points_between(entry, sl)
        atr_mtf_points = float(mtf.atr[-1]) / spec.point
        lo = p.min_stop_atr * atr_mtf_points
        hi = p.max_stop_atr * atr_mtf_points
        in_bounds = lo <= stop_points <= hi
        gates.append(GateResult(name="stop_bounds", passed=in_bounds, value=round(stop_points, 1),
                                threshold=round(hi, 1), comparison="in",
                                detail=f"allowed {lo:.0f}-{hi:.0f} pts "
                                       f"({p.min_stop_atr}-{p.max_stop_atr} x {p.mtf} ATR)"))
        if not in_bounds:
            return None, gates, "STOP_OUT_OF_BOUNDS"

        broker_min = spec.min_stop_distance_points(p.stop_safety_points)
        ok_broker = stop_points >= broker_min
        gates.append(GateResult(name="broker_stop_level", passed=ok_broker,
                                value=round(stop_points, 1), threshold=broker_min,
                                comparison=">=",
                                detail=f"broker stops_level {spec.stops_level_points} pts "
                                       f"+ {p.stop_safety_points} safety"))
        if not ok_broker:
            return None, gates, "STOP_TOO_TIGHT"

        tp = spec.normalize_price(entry + bias.sign * stop_points * spec.point * p.rr_target)
        return (
            ProposedTrade(
                side=bias, entry_price=entry, stop_loss=sl, take_profit=tp,
                stop_points=round(stop_points, 1), reward_risk=p.rr_target,
                rationale=f"stop beyond {p.mtf} swing {'low' if bias is Side.BUY else 'high'} "
                          f"at {anchor.price:.5f} with a {buffer / spec.point:.0f}-pt buffer",
            ),
            gates,
            None,
        )

    def _score(
        self, ctx: StrategyContext, htf: FeatureFrame, mtf: FeatureFrame, ltf: FeatureFrame,
        bias: Side, reg, loc_value: float, proposal: ProposedTrade,
    ) -> list[EvidenceItem]:
        p = self.p
        sign = bias.sign

        htf_adx = float(htf.adx[-1])
        ema_gap = (float(htf.ema_fast[-1]) - float(htf.ema_slow[-1])) / max(
            float(htf.atr[-1]), 1e-9
        )
        htf_score = _clip01(htf_adx / 40.0) * 0.6 + _clip01(abs(ema_gap) / 2.0) * 0.4
        htf_score *= 1.0 if (ema_gap * sign) > 0 else 0.4

        loc_score = _clip01((p.location_max - loc_value) / max(p.location_max, 1e-9))

        disp = float(ltf.displacement[-1]) if not np.isnan(ltf.displacement[-1]) else 0.0
        mom_score = _clip01((disp * sign) / 1.5)

        fvgs = [g for g in ltf.active_fvgs() if g.bullish == (bias is Side.BUY)]
        in_fvg = any(g.contains(proposal.entry_price) for g in fvgs)
        near = min((abs(g.midpoint - proposal.entry_price) / max(float(ltf.atr[-1]), 1e-9)
                    for g in fvgs), default=99.0)
        imb_score = 1.0 if in_fvg else _clip01((1.5 - near) / 1.5)

        sweep = ltf.sweep_at()
        want_kind = "LOW" if bias is Side.BUY else "HIGH"
        liq_score = 1.0 if (sweep is not None and sweep.kind == want_kind) else 0.0

        atr_pct = float(mtf.atr_pct[-1])
        vol_score = 1.0 - _clip01(abs(atr_pct - 0.55) / 0.45)

        return [
            EvidenceItem(name="htf_alignment", score=round(htf_score, 4), weight=p.w_htf,
                         detail=f"{p.htf} ADX {htf_adx:.1f}, EMA gap {ema_gap:+.2f} ATR"),
            EvidenceItem(name="location", score=round(loc_score, 4), weight=p.w_location,
                         detail=f"retrace depth {loc_value:.2f} (limit {p.location_max})"),
            EvidenceItem(name="momentum", score=round(mom_score, 4), weight=p.w_momentum,
                         detail=f"{p.ltf} displacement {disp:+.2f} ATR"),
            EvidenceItem(name="imbalance", score=round(imb_score, 4), weight=p.w_imbalance,
                         detail="entry inside an unmitigated aligned FVG" if in_fvg
                                else f"nearest aligned FVG {near:.1f} ATR away"),
            EvidenceItem(name="liquidity", score=round(liq_score, 4), weight=p.w_liquidity,
                         detail=f"swept {sweep.kind} pool at {sweep.price:.5f}" if sweep
                                else "no recent sweep"),
            EvidenceItem(name="vol_fit", score=round(vol_score, 4), weight=p.w_volfit,
                         detail=f"{p.mtf} ATR percentile {atr_pct:.2f}"),
        ]

    # -- management --------------------------------------------------------------

    def manage(self, ctx: StrategyContext) -> ManagementAction | None:
        """Breakeven, ATR trail and time stop. Only ever tightens (ADR-016)."""
        pos = ctx.position
        if pos is None or pos.stop_loss is None:
            return None
        p = self.p
        spec = ctx.spec
        ltf = ctx.features.get(p.ltf)
        if ltf is None or not ltf.ready():
            return None
        sign = pos.side.sign
        atr = float(ltf.atr[-1])

        # Time stop: an idea that has not worked within its window is not working.
        if pos.bars_held >= p.time_stop_bars and pos.r_multiple_open < 1.0:
            return ManagementAction(
                close_fraction=1.0, reason=ExitReason.TIME_STOP,
                detail=f"held {pos.bars_held} {p.ltf} bars without reaching +1R "
                       f"(currently {pos.r_multiple_open:+.2f}R)",
            )

        proposed: float | None = None
        reason = ExitReason.UNKNOWN
        detail = ""

        if pos.r_multiple_open >= p.trail_after_r:
            extreme = float(ltf.high[-1]) if pos.side is Side.BUY else float(ltf.low[-1])
            proposed = extreme - sign * p.trail_atr * atr
            reason = ExitReason.TRAILING_STOP
            detail = f"trail {p.trail_atr} ATR from {extreme:.5f} at {pos.r_multiple_open:+.2f}R"
        elif pos.r_multiple_open >= p.be_at_r:
            proposed = pos.entry_price + sign * 2.0 * ctx.quote.spread
            reason = ExitReason.BREAKEVEN_STOP
            detail = f"breakeven+spread at {pos.r_multiple_open:+.2f}R"

        if proposed is None:
            return None
        new_sl = spec.normalize_price(proposed)

        # Monotonic: never loosen a stop, and never move it through the current price.
        if (new_sl - pos.stop_loss) * sign <= 0:
            return None
        market = ctx.quote.exit_price_for(str(pos.side))
        min_gap = spec.min_stop_distance_points(p.stop_safety_points) * spec.point
        if (market - new_sl) * sign <= min_gap:
            return None
        return ManagementAction(new_stop_loss=new_sl, reason=reason, detail=detail)


def _clip01(x: float) -> float:
    if np.isnan(x):
        return 0.0
    return float(min(1.0, max(0.0, x)))


def with_params(strategy: StructureMomentum, **overrides) -> StructureMomentum:
    """Return a copy with some parameters replaced. Used by the walk-forward sweep."""
    return StructureMomentum(replace(strategy.p, **overrides))
