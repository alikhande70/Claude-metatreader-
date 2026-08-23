"""The trading engine.

**This is the single decision path** (ADR-003). The same object runs a backtest, a paper
session and a live session; only the ``MarketDataSource``, the ``ExecutionVenue`` and the
``FeatureProvider`` differ, and all three are injected. There is deliberately no ``mode``
branch anywhere in this file, and ``tests/unit/test_no_mode_branching.py`` fails the build if
one appears.

Loop shape
----------
The engine is **bar-close driven**. Quotes maintain the current price, the account snapshot
and staleness detection; they never trigger a decision. Every decision, every management
action and every order originates from the close of a strategy's trigger timeframe. That is
what makes results reproducible across the simulator, the Strategy Tester and live: none of
them agree about tick delivery, and all of them agree about bar closes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from atlas.bus.events import EventKind
from atlas.bus.journal import Journal
from atlas.core.clock import Clock, SimulatedClock
from atlas.core.decision import DecisionOutcomeLink, DecisionRecord, make_client_order_id
from atlas.core.enums import (
    DecisionOutcome,
    EngineState,
    HaltReason,
    RunMode,
    Timeframe,
)
from atlas.core.errors import AtlasError
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Quote
from atlas.core.trading import AccountState, OrderRequest, Trade
from atlas.data.calendar import TradingCalendar
from atlas.data.source import MarketDataSource, UpdateKind
from atlas.execution.router import OrderRouter
from atlas.execution.venue import ExecutionVenue
from atlas.risk.engine import OpenExposure, RiskEngine
from atlas.runtime.features import FeatureProvider, IncrementalFeatureProvider
from atlas.runtime.tracker import PositionTracker, TrackedPosition
from atlas.strategy.base import Strategy, StrategyContext


@dataclass(slots=True)
class EngineConfig:
    mode: RunMode = RunMode.BACKTEST
    magic: int = 20260823
    allowed_sessions: tuple[str, ...] = ()
    news_policy: str = "warn"
    #: A quote older than this makes market data stale and halts new entries.
    quote_staleness_ms: int = 120_000
    #: How often to reconcile our position set against the venue's, in trigger-bar closes.
    reconcile_every_bars: int = 20
    #: Emit a heartbeat this often (ms of engine time).
    heartbeat_ms: int = 60_000
    journal_decisions: bool = True
    #: Journalling every NO_SETUP decision is ~1 record per bar per symbol. Worth it live
    #: (it is the audit trail); optional in bulk parameter sweeps where it is pure overhead.
    journal_no_setup: bool = True


@dataclass(slots=True)
class EngineStats:
    updates: int = 0
    bars: int = 0
    decisions: int = 0
    signals: int = 0
    orders_submitted: int = 0
    orders_accepted: int = 0
    orders_rejected: int = 0
    risk_rejections: dict[str, int] = field(default_factory=dict)
    decision_reasons: dict[str, int] = field(default_factory=dict)
    trades_closed: int = 0
    halts: int = 0
    reconcile_divergences: int = 0
    wall_seconds: float = 0.0


class TradingEngine:
    def __init__(
        self,
        *,
        source: MarketDataSource,
        venue: ExecutionVenue,
        features: FeatureProvider,
        strategies: dict[str, Strategy],
        risk: RiskEngine,
        calendar: TradingCalendar,
        journal: Journal,
        clock: Clock,
        config: EngineConfig | None = None,
        on_state_change: Callable[[EngineState], None] | None = None,
    ) -> None:
        self.source = source
        self.venue = venue
        self.features = features
        self.strategies = strategies
        self.risk = risk
        self.calendar = calendar
        self.journal = journal
        self.clock = clock
        self.cfg = config or EngineConfig()
        self.router = OrderRouter(venue, clock, on_event=self._on_router_event)
        self.tracker = PositionTracker()
        self.stats = EngineStats()
        self.state = EngineState.IDLE
        self._on_state_change = on_state_change

        self.specs: dict[str, SymbolSpec] = {}
        self.quotes: dict[str, Quote] = {}
        self.account = AccountState()
        self._last_quote_ms: dict[str, int] = {}
        self._bars_since_reconcile = 0
        self._last_heartbeat = 0
        self._pending_decisions: dict[str, DecisionRecord] = {}
        self._trades_seen: set[str] = set()
        self._last_trade_scan_ms = 0
        self.closed_trades: list[Trade] = []

    # -- lifecycle ---------------------------------------------------------------

    def _set_state(self, state: EngineState, detail: str = "") -> None:
        if state is self.state:
            return
        self.state = state
        self.journal.append(EventKind.ENGINE_STATE, {"state": state, "detail": detail})
        if self._on_state_change is not None:
            self._on_state_change(state)

    async def run(self) -> EngineStats:
        started = time.perf_counter()
        self._set_state(EngineState.STARTING)
        try:
            await self._startup()
            self._set_state(EngineState.RUNNING)
            async for update in self.source.stream():
                await self._handle(update)
        except AtlasError as exc:
            self._set_state(EngineState.ERROR, str(exc))
            self.journal.append(EventKind.ERROR, {"error": str(exc), "type": type(exc).__name__})
            raise
        finally:
            self.stats.wall_seconds = time.perf_counter() - started
            await self._shutdown()
        return self.stats

    async def _startup(self) -> None:
        health = await self.venue.connect()
        self.journal.append(EventKind.VENUE_CONNECTED, {
            "connected": health.connected, "detail": health.detail,
            "server_offset_seconds": health.server_offset_seconds,
        })
        symbols = list(self.source.symbols)
        venue_specs = await self.venue.symbol_specs(symbols)
        for sym in symbols:
            self.specs[sym] = venue_specs.get(sym) or self.source.spec(sym)

        warm = await self.source.warmup()
        for (sym, tf), series in warm.items():
            if isinstance(self.features, IncrementalFeatureProvider):
                self.features.seed(sym, tf, series)
        self.account = await self.venue.account()
        self.risk.observe(self.account, self.clock.now_ms())
        self.journal.append(EventKind.RUN_STARTED, {
            "mode": self.cfg.mode, "symbols": symbols,
            "strategies": {s: st.describe() for s, st in self.strategies.items()},
            "parameters": {s: st.parameters() for s, st in self.strategies.items()},
            "magic": self.cfg.magic,
            "warmup_bars": {f"{s}/{tf}": len(v) for (s, tf), v in warm.items()},
        })

    async def _shutdown(self) -> None:
        self._set_state(EngineState.STOPPING)
        self.journal.append(EventKind.RUN_STOPPED, {
            "stats": {
                "updates": self.stats.updates, "bars": self.stats.bars,
                "decisions": self.stats.decisions, "signals": self.stats.signals,
                "orders_accepted": self.stats.orders_accepted,
                "trades_closed": self.stats.trades_closed,
                "decision_reasons": self.stats.decision_reasons,
                "risk_rejections": self.stats.risk_rejections,
            }
        })
        self.journal.flush()
        self._set_state(EngineState.STOPPED)

    # -- update handling ----------------------------------------------------------

    async def _handle(self, update) -> None:
        self.stats.updates += 1
        if isinstance(self.clock, SimulatedClock):
            self.clock.set(max(self.clock.now_ms(), update.ts))

        if update.kind is UpdateKind.QUOTE and update.quote is not None:
            self._on_quote(update.symbol, update.quote)
            return

        if update.kind is UpdateKind.BAR and update.bar is not None and update.tf is not None:
            await self._on_bar(update.symbol, update.tf, update.bar, update.ts)

    def _on_quote(self, symbol: str, quote: Quote) -> None:
        self.quotes[symbol] = quote
        self._last_quote_ms[symbol] = quote.ts
        self.venue.observe_quote(quote)
        for tp in self.tracker.by_symbol(symbol):
            tp.observe_price(quote.exit_price_for(str(tp.side)))

    async def _on_bar(self, symbol: str, tf: Timeframe, bar, ts: int) -> None:
        self.stats.bars += 1
        self.features.on_bar(symbol, tf, bar)
        strategy = self.strategies.get(symbol)
        if strategy is None or tf is not strategy.trigger_timeframe:
            return

        for tp in self.tracker.by_symbol(symbol):
            tp.bars_held += 1

        self.account = await self.venue.account()
        halt = self.risk.observe(self.account, ts)
        if halt is not None:
            self._on_halt(halt, ts)
        self.journal.append(EventKind.EQUITY_POINT, {
            "equity": self.account.equity, "balance": self.account.balance,
            "margin_level": self.account.margin_level,
        }, ts=ts)

        await self._sync_positions(symbol, ts)
        await self._manage(symbol, strategy, ts)
        await self._decide(symbol, strategy, ts)

        self._bars_since_reconcile += 1
        if self._bars_since_reconcile >= self.cfg.reconcile_every_bars:
            self._bars_since_reconcile = 0
            await self._reconcile(ts)
        if ts - self._last_heartbeat >= self.cfg.heartbeat_ms:
            self._last_heartbeat = ts
            self._heartbeat(ts)

    # -- decisions ------------------------------------------------------------------

    async def _decide(self, symbol: str, strategy: Strategy, ts: int) -> None:
        quote = self.quotes.get(symbol)
        spec = self.specs.get(symbol)
        if quote is None or spec is None:
            return
        held = self.tracker.by_symbol(symbol)
        position_view = None
        if held:
            live = {p.ticket: p for p in await self.venue.positions()}
            pos = live.get(held[0].ticket)
            if pos is not None:
                position_view = held[0].to_view(pos)

        ctx = StrategyContext(
            symbol=symbol, spec=spec, now_ms=ts, quote=quote,
            features=self.features.features_at(symbol, ts), calendar=self.calendar,
            position=position_view, news_policy=self.cfg.news_policy,
            allowed_sessions=self.cfg.allowed_sessions,
        )
        decision = strategy.evaluate(ctx)
        self.stats.decisions += 1
        self.stats.decision_reasons[decision.reason_code] = (
            self.stats.decision_reasons.get(decision.reason_code, 0) + 1
        )
        if decision.outcome is not DecisionOutcome.SIGNAL or decision.proposal is None:
            self._journal_decision(decision, ts)
            return

        self.stats.signals += 1
        assessment = self.risk.evaluate(
            symbol=symbol, spec=spec, side=decision.proposal.side,
            stop_points=decision.proposal.stop_points, account=self.account,
            open_exposure=self._exposure(), conviction=decision.conviction, now_ms=ts,
        )
        if not assessment.approved:
            self.stats.risk_rejections[assessment.reason_code] = (
                self.stats.risk_rejections.get(assessment.reason_code, 0) + 1
            )
            suppressed = decision.model_copy(update={
                "outcome": DecisionOutcome.SUPPRESSED,
                "reason_code": assessment.reason_code,
                "reason_detail": assessment.detail,
                "gates": decision.gates + tuple(assessment.gates),
                "risk_notes": tuple(assessment.notes),
            })
            self._journal_decision(suppressed, ts, force=True)
            self.journal.append(EventKind.RISK_REJECTED, {
                "decision_id": decision.decision_id, "reason": assessment.reason_code,
                "detail": assessment.detail,
            }, stream=symbol, ts=ts)
            return

        coid = make_client_order_id(decision.decision_id)
        approved = decision.model_copy(update={
            "gates": decision.gates + tuple(assessment.gates),
            "sized_volume": assessment.volume,
            "client_order_id": coid,
            "risk_notes": tuple(assessment.notes),
        })
        self._journal_decision(approved, ts, force=True)
        self._pending_decisions[coid] = approved
        await self._submit(symbol, spec, approved, assessment.volume, ts)

    def _journal_decision(self, decision: DecisionRecord, ts: int, *, force: bool = False) -> None:
        if not self.cfg.journal_decisions:
            return
        skip_no_setup = not force and not self.cfg.journal_no_setup
        if skip_no_setup and decision.outcome is DecisionOutcome.NO_SETUP:
            return
        self.journal.append(EventKind.DECISION, {"record": decision},
                            stream=decision.symbol, ts=ts)

    async def _submit(
        self, symbol: str, spec: SymbolSpec, decision: DecisionRecord, volume: float, ts: int
    ) -> None:
        p = decision.proposal
        assert p is not None
        request = OrderRequest(
            client_order_id=decision.client_order_id or make_client_order_id(decision.decision_id),
            decision_id=decision.decision_id, symbol=symbol, side=p.side,
            volume=volume, stop_loss=p.stop_loss, take_profit=p.take_profit,
            magic=self.cfg.magic, comment=decision.client_order_id or "",
            fill_policy=spec.preferred_filling(),
        )
        self.stats.orders_submitted += 1
        self.journal.append(EventKind.ORDER_SUBMITTED, {
            "request": request, "decision_id": decision.decision_id,
            "conviction": decision.conviction,
        }, stream=symbol, ts=ts)
        outcome = await self.router.submit(request)
        if outcome.accepted:
            self.stats.orders_accepted += 1
            self.journal.append(EventKind.ORDER_ACCEPTED, {
                "client_order_id": outcome.client_order_id, "status": outcome.status,
                "attempts": outcome.attempts, "log": outcome.log,
                "resolved_by_lookup": outcome.resolved_by_lookup,
            }, stream=symbol, ts=ts)
        else:
            self.stats.orders_rejected += 1
            self._pending_decisions.pop(request.client_order_id, None)
            self.journal.append(EventKind.ORDER_REJECTED, {
                "client_order_id": outcome.client_order_id, "error": outcome.error,
                "attempts": outcome.attempts, "log": outcome.log,
            }, stream=symbol, ts=ts)

    # -- position lifecycle -----------------------------------------------------------

    async def _sync_positions(self, symbol: str, ts: int) -> None:
        """Diff the venue's positions against ours, opening and closing tracked records.

        The venue is the authority. Anything it reports that we do not know about is adopted;
        anything we know about that it no longer reports is closed.
        """
        live = await self.venue.positions()
        live_by_ticket = {p.ticket: p for p in live}

        for pos in live:
            known = self.tracker.get(pos.ticket)
            if known is not None:
                # The venue is authoritative about the stop and the volume. Without this the
                # tracker keeps reporting the ENTRY stop as live risk, so a position moved to
                # breakeven still consumes its full share of the aggregate risk cap and
                # blocks trades the account can afford.
                known.observe_venue(pos)
                continue
            decision = self._match_decision(pos)
            if decision is not None and decision.proposal is not None:
                tp = TrackedPosition(
                    ticket=pos.ticket, symbol=pos.symbol, side=pos.side,
                    strategy=decision.strategy, decision_id=decision.decision_id,
                    client_order_id=decision.client_order_id or "",
                    entry_price=pos.open_price,
                    initial_stop=decision.proposal.stop_loss,
                    initial_target=decision.proposal.take_profit,
                    opened_ts=pos.open_time, volume=pos.volume,
                    current_stop=pos.stop_loss,
                )
                tp.observe_venue(pos)
                self.tracker.add(tp)
                self.journal.append(EventKind.POSITION_OPENED, {
                    "ticket": pos.ticket, "decision_id": decision.decision_id,
                    "entry": pos.open_price, "volume": pos.volume,
                    "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                    "slippage_price": round(pos.open_price - decision.proposal.entry_price, 8),
                }, stream=pos.symbol, ts=ts)
            elif pos.magic == self.cfg.magic:
                owner = self.strategies.get(pos.symbol)
                tp = self.tracker.adopt(
                    pos, strategy=owner.name if owner is not None else "",
                    reason="adopted: matching magic but no local decision record",
                )
                self.journal.append(EventKind.RECONCILE_REPAIRED, {
                    "action": "adopted_position", "ticket": pos.ticket,
                    "detail": tp.notes[-1], "stop_reconstructed": True,
                }, stream=pos.symbol, ts=ts)

        for ticket in list(self.tracker.tickets()):
            if ticket in live_by_ticket:
                continue
            removed = self.tracker.remove(ticket)
            if removed is not None:
                tp = removed
                self.journal.append(EventKind.POSITION_CLOSED, {
                    "ticket": ticket, "decision_id": tp.decision_id,
                }, stream=tp.symbol, ts=ts)
        await self._collect_trades(ts)

    def _match_decision(self, pos) -> DecisionRecord | None:
        """Match a venue position back to the decision that requested it.

        Primary key is the client order id carried in the order comment. Many brokers
        truncate or overwrite comments, so the fallback is magic + symbol + side + a volume
        and open-time window -- deliberately narrow, because a wrong match attributes a trade
        to the wrong decision and corrupts the evaluation data.
        """
        comment = (pos.comment or "").strip()
        if comment and comment in self._pending_decisions:
            return self._pending_decisions.pop(comment)
        for coid, d in list(self._pending_decisions.items()):
            if d.symbol != pos.symbol or d.proposal is None:
                continue
            if d.proposal.side is not pos.side or pos.magic != self.cfg.magic:
                continue
            if d.sized_volume is not None and abs(d.sized_volume - pos.volume) > 1e-9:
                continue
            if abs(pos.open_time - d.ts) > 5 * 60_000:
                continue
            return self._pending_decisions.pop(coid)
        return None

    async def _collect_trades(self, ts: int) -> None:
        for trade in await self.venue.closed_trades(self._last_trade_scan_ms):
            if trade.trade_id in self._trades_seen:
                continue
            self._trades_seen.add(trade.trade_id)
            self.closed_trades.append(trade)
            self.stats.trades_closed += 1
            self.journal.append(EventKind.TRADE_RECORDED, {"trade": trade},
                                stream=trade.symbol, ts=trade.exit_time)
            if trade.decision_id:
                link = DecisionOutcomeLink(
                    decision_id=trade.decision_id, trade_id=trade.trade_id,
                    r_multiple=trade.r_multiple, net_profit=trade.net_profit,
                    mae_points=trade.mae_points, mfe_points=trade.mfe_points,
                    exit_reason=str(trade.exit_reason), holding_ms=trade.duration_ms,
                )
                self.journal.append(EventKind.DECISION_OUTCOME, {"link": link},
                                    stream=trade.symbol, ts=trade.exit_time)
            halt = self.risk.on_trade_closed(trade.net_profit, ts)
            if halt is not None:
                self._on_halt(halt, ts)
        self._last_trade_scan_ms = ts

    async def _manage(self, symbol: str, strategy: Strategy, ts: int) -> None:
        held = self.tracker.by_symbol(symbol)
        if not held:
            return
        quote = self.quotes.get(symbol)
        spec = self.specs.get(symbol)
        if quote is None or spec is None:
            return
        # A reconciliation halt freezes management too: acting on state we do not trust is
        # worse than doing nothing, and the server-side stop still protects the position.
        frozen = (
            self.risk.state.halted
            and self.risk.state.halt_reason is HaltReason.RECONCILIATION_DIVERGENCE
        )
        if frozen:
            return
        live = {p.ticket: p for p in await self.venue.positions()}
        for tp in held:
            pos = live.get(tp.ticket)
            if pos is None:
                continue
            ctx = StrategyContext(
                symbol=symbol, spec=spec, now_ms=ts, quote=quote,
                features=self.features.features_at(symbol, ts), calendar=self.calendar,
                position=tp.to_view(pos), news_policy=self.cfg.news_policy,
                allowed_sessions=self.cfg.allowed_sessions,
            )
            action = strategy.manage(ctx)
            if action is None or action.is_noop:
                continue
            if action.close_fraction > 0:
                full = action.close_fraction >= 1.0
                vol = None if full else pos.volume * action.close_fraction
                result = await self.router.close(tp.ticket, volume=vol, reason=action.reason)
                self.journal.append(EventKind.POSITION_CLOSED, {
                    "ticket": tp.ticket, "reason": action.reason, "detail": action.detail,
                    "accepted": result.accepted, "retcode": result.retcode,
                }, stream=symbol, ts=ts)
                if result.accepted and full:
                    # Drop it immediately. Waiting for the next sync would leave the
                    # reconciler seeing a position we ourselves just closed and reporting a
                    # divergence that is not one -- noise that trains the operator to ignore
                    # the alert that matters.
                    self.tracker.remove(tp.ticket)
                    await self._collect_trades(ts)
                continue
            result = await self.router.modify(
                tp.ticket, stop_loss=action.new_stop_loss, take_profit=action.new_take_profit
            )
            self.journal.append(EventKind.POSITION_MODIFIED, {
                "ticket": tp.ticket, "new_stop_loss": action.new_stop_loss,
                "new_take_profit": action.new_take_profit, "reason": action.reason,
                "detail": action.detail, "accepted": result.accepted,
                "retcode": result.retcode, "retcode_text": result.retcode_text,
            }, stream=symbol, ts=ts)

    def _exposure(self) -> list[OpenExposure]:
        out = []
        for tp in self.tracker:
            spec = self.specs.get(tp.symbol)
            quote = self.quotes.get(tp.symbol)
            if spec is None or quote is None:
                continue
            price = quote.exit_price_for(str(tp.side))
            out.append(OpenExposure(
                symbol=tp.symbol, side=tp.side,
                risk_money=tp.risk_money(spec, price),
                group=self.risk.cfg.group_of(tp.symbol),
            ))
        return out

    # -- reconciliation, health, heartbeat ---------------------------------------------

    async def _reconcile(self, ts: int) -> None:
        """Compare our belief about the world with the venue's.

        This is where most live systems fail: a partial fill, a manual intervention, or a
        broker-side stop-out leaves the engine acting on a position that no longer exists.
        The venue always wins; divergence is journalled and, if it cannot be repaired,
        halts trading.
        """
        self.journal.append(EventKind.RECONCILE_STARTED, {}, ts=ts)
        try:
            live = await self.venue.positions()
            account = await self.venue.account()
        except AtlasError as exc:
            self.risk.report_health(venue_ok=False, data_fresh=True, now_ms=ts)
            self.journal.append(EventKind.RECONCILE_DIVERGENCE, {
                "error": str(exc), "detail": "could not read venue state",
            }, ts=ts)
            self.stats.reconcile_divergences += 1
            return

        live_tickets = {p.ticket for p in live}
        ours = self.tracker.tickets()
        missing = ours - live_tickets
        unknown = {p.ticket for p in live if p.magic == self.cfg.magic} - ours
        stale = [
            s for s, last in self._last_quote_ms.items()
            if ts - last > self.cfg.quote_staleness_ms
        ]
        data_fresh = not stale
        self.risk.report_health(venue_ok=True, data_fresh=data_fresh, now_ms=ts)

        if missing or unknown:
            self.stats.reconcile_divergences += 1
            self.journal.append(EventKind.RECONCILE_DIVERGENCE, {
                "missing_at_venue": sorted(missing), "unknown_locally": sorted(unknown),
                "detail": "adopting venue state as authoritative",
            }, ts=ts)
            for ticket in missing:
                self.tracker.remove(ticket)
            for pos in live:
                if pos.ticket in unknown:
                    self.tracker.adopt(pos, strategy="", reason="found during reconciliation")
            self.journal.append(EventKind.RECONCILE_REPAIRED, {
                "positions": len(self.tracker),
            }, ts=ts)
        else:
            self.journal.append(EventKind.RECONCILE_OK, {
                "positions": len(live), "equity": account.equity, "stale_symbols": stale,
            }, ts=ts)
        if stale:
            self.journal.append(EventKind.DATA_STALE, {"symbols": stale}, ts=ts)
        if self.risk.state.halted and self.state is EngineState.RUNNING:
            self._set_state(EngineState.HALTED, self.risk.state.halt_detail)
        elif not self.risk.state.halted and self.state is EngineState.HALTED:
            self._set_state(EngineState.RUNNING, "kill switch cleared")

    def _on_halt(self, reason: HaltReason, ts: int) -> None:
        self.stats.halts += 1
        self.journal.append(EventKind.KILL_SWITCH, {
            "reason": reason, "detail": self.risk.state.halt_detail,
            "equity": self.account.equity,
        }, ts=ts)
        self._set_state(EngineState.HALTED, self.risk.state.halt_detail)

    def _heartbeat(self, ts: int) -> None:
        self.journal.append(EventKind.HEARTBEAT, {
            "state": self.state, "equity": self.account.equity,
            "open_positions": len(self.tracker), "halted": self.risk.state.halted,
            "decisions": self.stats.decisions, "trades": self.stats.trades_closed,
        }, ts=ts)

    def _on_router_event(self, kind: str, payload: dict) -> None:
        self.journal.append(EventKind.ORDER_RETRY, payload)

    # -- operator commands -------------------------------------------------------------

    def command_halt(self, detail: str = "operator halt") -> None:
        self.risk.halt(HaltReason.MANUAL, detail, self.clock.now_ms())
        self.journal.append(EventKind.COMMAND, {"command": "halt", "detail": detail})
        self._set_state(EngineState.HALTED, detail)

    def command_resume(self, operator: str = "operator") -> bool:
        ok = self.risk.resume(self.clock.now_ms(), operator)
        self.journal.append(EventKind.COMMAND, {"command": "resume", "operator": operator,
                                                "applied": ok})
        if ok:
            self._set_state(EngineState.RUNNING, "resumed by operator")
        return ok


