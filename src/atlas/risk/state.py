"""Risk state and the kill-switch state machine.

Two properties matter more than anything else here:

1. **A halt survives a restart.** State is persisted, so a process that dies while halted
   comes back halted. A system that silently re-arms itself after a crash is worse than one
   with no kill switch, because it creates a false belief that the limit is enforced.
2. **Auto-clearing is per-reason and explicit.** A daily-loss halt clears at the next daily
   reset, because that is what a daily limit means. A total-drawdown or reconciliation halt
   requires an operator, because those say something is wrong that time does not fix.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from atlas.core.enums import HaltReason
from atlas.core.market import ms_to_dt

#: Halts that clear on their own at the next daily reset. Everything else needs an operator.
AUTO_CLEARING: frozenset[HaltReason] = frozenset(
    {HaltReason.DAILY_LOSS_LIMIT, HaltReason.CONSECUTIVE_LOSSES}
)

#: Halts caused by system health rather than by a risk limit. They clear when the underlying
#: condition clears, and are re-evaluated on every health check.
HEALTH_HALTS: frozenset[HaltReason] = frozenset(
    {HaltReason.VENUE_UNAVAILABLE, HaltReason.STALE_MARKET_DATA}
)


class RiskState(BaseModel):
    """Persistent risk state. Serialised to JSON alongside the journal."""

    initial_balance: float = 0.0
    equity_hwm: float = 0.0
    day_key: str = ""
    day_start_equity: float = 0.0
    realized_today: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0

    halted: bool = False
    halt_reason: HaltReason = HaltReason.NONE
    halt_detail: str = ""
    halted_at_ms: int = 0
    halt_count: int = 0

    last_equity: float = 0.0
    last_update_ms: int = 0
    notes: list[str] = Field(default_factory=list)

    # -- day boundary -----------------------------------------------------------

    @staticmethod
    def day_key_for(ts_ms: int, tz: str, reset_hour: int) -> str:
        """Trading-day identifier.

        The boundary is ``reset_hour`` local time in ``tz``, which for a prop account is the
        firm's timezone and is rarely the broker's server time. Times before the reset hour
        belong to the previous trading day.
        """
        local = ms_to_dt(ts_ms).astimezone(ZoneInfo(tz))
        if local.hour < reset_hour:
            local = local - timedelta(days=1)
        return local.strftime("%Y-%m-%d")

    def roll_day_if_needed(self, ts_ms: int, equity: float, tz: str, reset_hour: int) -> bool:
        """Start a new trading day if the reset boundary has been crossed.

        Returns True if a roll happened. Auto-clearing halts are released here -- that is the
        entire meaning of a *daily* limit.
        """
        key = self.day_key_for(ts_ms, tz, reset_hour)
        if key == self.day_key:
            return False
        self.day_key = key
        self.day_start_equity = equity
        self.realized_today = 0.0
        self.trades_today = 0
        if self.halted and self.halt_reason in AUTO_CLEARING:
            self.notes.append(
                f"{ms_to_dt(ts_ms).isoformat()}: auto-cleared {self.halt_reason} at daily reset"
            )
            self.halted = False
            self.halt_reason = HaltReason.NONE
            self.halt_detail = ""
            self.consecutive_losses = 0
        return True

    # -- observation ------------------------------------------------------------

    def observe_equity(self, ts_ms: int, equity: float, balance: float) -> None:
        if self.initial_balance <= 0:
            self.initial_balance = balance if balance > 0 else equity
        if self.day_start_equity <= 0:
            self.day_start_equity = equity
        self.equity_hwm = max(self.equity_hwm, equity)
        self.last_equity = equity
        self.last_update_ms = ts_ms

    # -- derived measures --------------------------------------------------------

    def daily_loss_pct(self, equity: float) -> float:
        """Loss since the day's start, as a percentage of the day's starting equity.

        Computed on **equity**, so an open floating loss counts. Prop daily limits are
        breached on floating loss without a single trade being closed.
        """
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, (self.day_start_equity - equity) / self.day_start_equity * 100.0)

    def drawdown_pct(self, equity: float, *, trailing: bool) -> float:
        base = self.equity_hwm if trailing else self.initial_balance
        if base <= 0:
            return 0.0
        return max(0.0, (base - equity) / base * 100.0)

    # -- halting -----------------------------------------------------------------

    def trip(self, reason: HaltReason, detail: str, ts_ms: int) -> bool:
        """Trip the kill switch. Returns True if this call caused the transition.

        A non-auto-clearing reason always overrides an auto-clearing one that is already
        active, so a total-drawdown breach during a daily-loss halt is not swallowed.
        """
        if self.halted and self.halt_reason == reason:
            return False
        if self.halted and reason in AUTO_CLEARING and self.halt_reason not in AUTO_CLEARING:
            return False  # do not downgrade a serious halt to a lesser one
        self.halted = True
        self.halt_reason = reason
        self.halt_detail = detail
        self.halted_at_ms = ts_ms
        self.halt_count += 1
        self.notes.append(f"{ms_to_dt(ts_ms).isoformat()}: HALT {reason} -- {detail}")
        return True

    def clear(self, ts_ms: int, operator: str = "operator") -> bool:
        if not self.halted:
            return False
        self.notes.append(
            f"{ms_to_dt(ts_ms).isoformat()}: {operator} cleared {self.halt_reason}"
        )
        self.halted = False
        self.halt_reason = HaltReason.NONE
        self.halt_detail = ""
        self.consecutive_losses = 0
        return True

    def record_trade_result(self, net_profit: float) -> None:
        self.realized_today += net_profit
        self.trades_today += 1
        if net_profit < 0:
            self.consecutive_losses += 1
        elif net_profit > 0:
            self.consecutive_losses = 0

    # -- persistence -------------------------------------------------------------

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(p)  # atomic: a crash mid-write must not leave a truncated state file

    @classmethod
    def load(cls, path: Path | str, now_ms: int = 0) -> RiskState:
        """Load persisted state.

        ``now_ms`` timestamps the fail-safe halt below. It is a parameter rather than a wall
        clock read because nothing in the decision path may call ``datetime.now()`` -- that
        is what makes a backtest reproducible, and the rule holds even in recovery paths
        (``tests/unit/test_no_mode_branching.py`` enforces it).
        """
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls(**json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError):
            # A corrupt state file must fail SAFE: assume halted until an operator looks.
            st = cls()
            st.trip(
                HaltReason.MANUAL,
                f"risk state file at {p} was unreadable; halted pending operator review",
                now_ms,
            )
            return st
