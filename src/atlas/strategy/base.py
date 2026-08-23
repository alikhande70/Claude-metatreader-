"""Strategy protocol and the context a strategy is allowed to see.

A strategy's job is narrow on purpose: given market state, decide whether there is a trade
and where its stop belongs. It does **not** know the account balance, does not size the
position, and cannot place an order. Sizing and capital protection live in the risk engine
(ADR-010), so a bug in signal logic can never bypass them.

The context is deliberately a snapshot of immutable data rather than handles to live objects.
A strategy that received the mutable series could index into the future; one that receives
frozen frames cannot.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from atlas.core.decision import DecisionRecord
from atlas.core.enums import ExitReason, Side, Timeframe
from atlas.core.instrument import SymbolSpec
from atlas.core.market import Quote
from atlas.core.trading import Position
from atlas.data.calendar import TradingCalendar
from atlas.features.frame import MultiTimeframeFeatures


@dataclass(frozen=True, slots=True)
class PositionView:
    """What a strategy may know about its own open position.

    Excludes money: a strategy that can see its own P/L will, sooner or later, be written to
    behave differently when losing. Risk-of-ruin behaviour belongs to the risk engine.
    """

    ticket: int
    side: Side
    entry_price: float
    stop_loss: float | None
    take_profit: float | None
    open_time: int
    bars_held: int
    r_multiple_open: float
    mfe_r: float
    stop_reconstructed: bool = False

    @classmethod
    def from_position(
        cls, pos: Position, initial_stop: float, spec: SymbolSpec, *, bars_held: int,
        mfe_price: float | None = None, reconstructed: bool = False,
    ) -> PositionView:
        risk = abs(pos.open_price - initial_stop)
        move = (pos.current_price - pos.open_price) * pos.side.sign
        mfe_move = ((mfe_price - pos.open_price) * pos.side.sign) if mfe_price is not None else move
        return cls(
            ticket=pos.ticket, side=pos.side, entry_price=pos.open_price,
            stop_loss=pos.stop_loss, take_profit=pos.take_profit, open_time=pos.open_time,
            bars_held=bars_held,
            r_multiple_open=(move / risk) if risk > 0 else 0.0,
            mfe_r=(mfe_move / risk) if risk > 0 else 0.0,
            stop_reconstructed=reconstructed,
        )


@dataclass(frozen=True, slots=True)
class StrategyContext:
    symbol: str
    spec: SymbolSpec
    now_ms: int
    quote: Quote
    features: MultiTimeframeFeatures
    calendar: TradingCalendar
    position: PositionView | None = None
    news_policy: str = "warn"  # warn | block -- what to do when the calendar is unavailable
    allowed_sessions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def spread_points(self) -> float:
        return self.quote.spread_points(self.spec.point)


@dataclass(frozen=True, slots=True)
class ManagementAction:
    """An adjustment to an open position. Only ever tightens risk (ADR-016)."""

    new_stop_loss: float | None = None
    new_take_profit: float | None = None
    close_fraction: float = 0.0  # 1.0 closes the position
    reason: ExitReason = ExitReason.UNKNOWN
    detail: str = ""

    @property
    def is_noop(self) -> bool:
        return (
            self.new_stop_loss is None and self.new_take_profit is None
            and self.close_fraction <= 0
        )


class Strategy(ABC):
    """Base class for all strategies."""

    name: str = "unnamed"
    version: str = "1"

    @property
    @abstractmethod
    def timeframes(self) -> tuple[Timeframe, ...]:
        """Every timeframe this strategy reads. Fixed before testing; see docs/STRATEGY.md."""

    @property
    @abstractmethod
    def trigger_timeframe(self) -> Timeframe:
        """The timeframe whose bar close drives evaluation and trade management."""

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> DecisionRecord:
        """Produce a decision. Must return a record on **every** call, including no-trade
        ones -- the frequency and reasons for standing aside are data (ADR-008)."""

    def manage(self, ctx: StrategyContext) -> ManagementAction | None:
        """Adjust an open position. Default: no management."""
        return None

    def parameters(self) -> dict[str, float | int | str]:
        """Flat parameter dict, journalled with every run so results are reproducible."""
        return {}

    def describe(self) -> str:
        return f"{self.name} v{self.version}"
