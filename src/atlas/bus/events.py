"""Event taxonomy and envelope.

Event *kinds* are stable strings persisted forever. Adding a kind is backwards compatible;
renaming or repurposing one is not. Payload schemas are additive only -- readers must tolerate
unknown keys, because a journal written by a newer build will be read by an older one during
rollback.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventKind:
    """Namespaced event kinds. Plain constants rather than an enum so that an unknown kind
    read from an old journal is still a valid value instead of a load-time crash."""

    # lifecycle
    RUN_STARTED = "run.started"
    RUN_STOPPED = "run.stopped"
    ENGINE_STATE = "engine.state"
    HEARTBEAT = "engine.heartbeat"
    ERROR = "engine.error"

    # connectivity
    VENUE_CONNECTED = "venue.connected"
    VENUE_DISCONNECTED = "venue.disconnected"
    VENUE_ERROR = "venue.error"

    # market data
    BAR_CLOSED = "market.bar_closed"
    QUOTE = "market.quote"
    SPEC_CHANGED = "market.spec_changed"
    DATA_STALE = "market.data_stale"

    # decisions
    DECISION = "decision.recorded"
    DECISION_OUTCOME = "decision.outcome"

    # risk
    RISK_EVALUATED = "risk.evaluated"
    RISK_REJECTED = "risk.rejected"
    KILL_SWITCH = "risk.kill_switch"
    KILL_SWITCH_RESET = "risk.kill_switch_reset"

    # execution
    ORDER_SUBMITTED = "order.submitted"
    ORDER_ACCEPTED = "order.accepted"
    ORDER_REJECTED = "order.rejected"
    ORDER_FILLED = "order.filled"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_RETRY = "order.retry"
    POSITION_OPENED = "position.opened"
    POSITION_MODIFIED = "position.modified"
    POSITION_CLOSED = "position.closed"
    TRADE_RECORDED = "trade.recorded"

    # portfolio / account
    ACCOUNT_SNAPSHOT = "account.snapshot"
    EQUITY_POINT = "account.equity_point"

    # reconciliation
    RECONCILE_STARTED = "reconcile.started"
    RECONCILE_OK = "reconcile.ok"
    RECONCILE_DIVERGENCE = "reconcile.divergence"
    RECONCILE_REPAIRED = "reconcile.repaired"

    # operator actions
    COMMAND = "operator.command"


#: Kinds that must never be dropped by sampling or filtering: they carry money or safety
#: semantics and their absence would make the journal an unreliable record.
CRITICAL_KINDS: frozenset[str] = frozenset(
    {
        EventKind.RUN_STARTED,
        EventKind.RUN_STOPPED,
        EventKind.ORDER_SUBMITTED,
        EventKind.ORDER_ACCEPTED,
        EventKind.ORDER_REJECTED,
        EventKind.ORDER_FILLED,
        EventKind.ORDER_CANCELLED,
        EventKind.POSITION_OPENED,
        EventKind.POSITION_MODIFIED,
        EventKind.POSITION_CLOSED,
        EventKind.TRADE_RECORDED,
        EventKind.KILL_SWITCH,
        EventKind.KILL_SWITCH_RESET,
        EventKind.RECONCILE_DIVERGENCE,
        EventKind.RECONCILE_REPAIRED,
        EventKind.ERROR,
        EventKind.COMMAND,
    }
)


class Event(BaseModel):
    """One journalled fact.

    ``seq`` is a per-journal monotonic integer assigned at append time and is the only
    ordering that matters; ``ts`` is wall/simulated time and may repeat or, across a clock
    correction, go backwards.
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    ts: int
    kind: str
    stream: str = "system"  # symbol, or a subsystem name
    run_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)
