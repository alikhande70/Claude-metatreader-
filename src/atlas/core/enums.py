"""Enumerations shared across the whole system.

Values are stable strings because they are persisted in the event journal and sent over the
MT5 wire protocol. Renaming a value is a breaking change to both.
"""

from __future__ import annotations

from enum import StrEnum


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"

    @property
    def seconds(self) -> int:
        return _TF_SECONDS[self]

    @property
    def minutes(self) -> int:
        return _TF_SECONDS[self] // 60

    @classmethod
    def from_minutes(cls, minutes: int) -> Timeframe:
        for tf, secs in _TF_SECONDS.items():
            if secs == minutes * 60:
                return tf
        raise ValueError(f"no timeframe with {minutes} minutes")


_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
}


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short. Used to make price maths direction-agnostic."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(StrEnum):
    PENDING_NEW = "PENDING_NEW"  # created locally, not yet acknowledged by the venue
    WORKING = "WORKING"  # live at the venue (pending order resting)
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}


class TimeInForce(StrEnum):
    GTC = "GTC"
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class FillPolicy(StrEnum):
    """MT5 order filling mode. Broker-dependent; querying the symbol is mandatory."""

    FOK = "FOK"
    IOC = "IOC"
    RETURN = "RETURN"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    TIME_STOP = "TIME_STOP"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    SESSION_CLOSE = "SESSION_CLOSE"
    RISK_HALT = "RISK_HALT"
    MANUAL = "MANUAL"
    STOP_OUT = "STOP_OUT"
    PARTIAL_TAKE = "PARTIAL_TAKE"
    UNKNOWN = "UNKNOWN"


class DecisionOutcome(StrEnum):
    """What the strategy concluded on one evaluation."""

    SIGNAL = "SIGNAL"  # a tradeable setup passed every gate
    NO_SETUP = "NO_SETUP"  # nothing to trade; not an error
    VETOED = "VETOED"  # a setup existed but a hard gate rejected it
    SUPPRESSED = "SUPPRESSED"  # signal existed but risk/portfolio layer refused it


class Regime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL_SHOCK = "HIGH_VOL_SHOCK"
    UNKNOWN = "UNKNOWN"


class StructureEvent(StrEnum):
    BOS_UP = "BOS_UP"  # break of structure, continuation upward
    BOS_DOWN = "BOS_DOWN"
    CHOCH_UP = "CHOCH_UP"  # change of character, potential reversal upward
    CHOCH_DOWN = "CHOCH_DOWN"
    NONE = "NONE"


class RunMode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class EngineState(StrEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"  # running but a dependency is unhealthy
    HALTED = "HALTED"  # kill switch tripped; no new orders
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class HaltReason(StrEnum):
    NONE = "NONE"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    TOTAL_DRAWDOWN_LIMIT = "TOTAL_DRAWDOWN_LIMIT"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    RECONCILIATION_DIVERGENCE = "RECONCILIATION_DIVERGENCE"
    VENUE_UNAVAILABLE = "VENUE_UNAVAILABLE"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    MANUAL = "MANUAL"
    UNHANDLED_ERROR = "UNHANDLED_ERROR"
