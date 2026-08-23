"""Time sources.

Nothing in ATLAS calls ``time.time()`` or ``datetime.now()`` directly. Every component takes
a ``Clock``. That is what makes a backtest run the same code as live (ADR-003): the backtest
injects a ``SimulatedClock`` driven by bar timestamps, and time-dependent logic -- session
filters, time stops, daily reset boundaries, staleness detection -- behaves identically.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime


class Clock(ABC):
    @abstractmethod
    def now_ms(self) -> int:
        """Current time, epoch milliseconds UTC."""

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.now_ms() / 1000, tz=UTC)

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Await ``seconds`` of this clock's time."""

    @property
    def is_simulated(self) -> bool:
        return False


class SystemClock(Clock):
    """Wall clock. Used in paper and live runs."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimulatedClock(Clock):
    """Clock advanced explicitly by the replay engine.

    ``sleep`` returns immediately rather than advancing time. That is deliberate: in a
    backtest the *data* advances time, and letting a sleep move the clock would let a busy
    component skip past bars it should have processed.
    """

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def set(self, ms: int) -> None:
        """Jump to an absolute time. Backwards jumps are allowed only across runs, so we
        guard: silently moving time backwards mid-run would corrupt daily-reset logic."""
        if ms < self._now:
            raise ValueError(f"simulated clock moved backwards: {self._now} -> {ms}")
        self._now = ms

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("cannot advance by a negative amount")
        self._now += ms

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)  # yield to the loop without consuming simulated time

    @property
    def is_simulated(self) -> bool:
        return True


class FrozenClock(Clock):
    """Non-advancing clock for unit tests that assert on exact timestamps."""

    def __init__(self, at_ms: int = 0) -> None:
        self.at_ms = at_ms

    def now_ms(self) -> int:
        return self.at_ms

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)

    @property
    def is_simulated(self) -> bool:
        return True
