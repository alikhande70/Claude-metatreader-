"""Trading sessions, broker server time, and the news blackout calendar.

Why this module is more careful than it looks
---------------------------------------------
Session logic is where trading systems silently break twice a year. Three separate hazards:

1. **DST.** London, New York and Sydney change clocks on *different dates*. A session window
   pinned to fixed UTC hours drifts by an hour for several weeks each year, and the drift
   lands exactly on session opens -- the highest-volatility minutes of the day. ATLAS
   therefore defines sessions in their **market-local timezone** and converts per-day via
   ``zoneinfo``, so DST is handled by the tz database rather than by our arithmetic.

2. **Broker server time.** ``TimeCurrent()`` in MT5 is server time, commonly UTC+2/UTC+3 but
   never guaranteed. The offset is *measured* at runtime and re-measured periodically, never
   hardcoded (see :class:`ServerClockMapping`).

3. **Rollover.** Spreads widen 5-20x around server midnight. The window is derived from the
   measured server offset, not from a fixed UTC hour, because it follows the broker's day.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from atlas.core.market import ms_to_dt


class SessionDef(BaseModel):
    """A trading session defined in its own market's local time.

    ``start``/``end`` are local wall-clock times in ``tz``. Sessions that wrap midnight
    (end < start) are supported and are common for Asia-anchored definitions.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    tz: str
    start: time
    end: time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon..Fri, ISO weekday()-style (Mon=0)

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(ZoneInfo(self.tz))
        if self.start <= self.end:
            in_window = self.start <= local.time() < self.end
            day = local.weekday()
        else:
            # Wraps midnight: the second half belongs to the *previous* trading day.
            if local.time() >= self.start:
                in_window, day = True, local.weekday()
            elif local.time() < self.end:
                in_window, day = True, (local - timedelta(days=1)).weekday()
            else:
                in_window, day = False, local.weekday()
        return in_window and day in self.weekdays


#: Default session definitions. Times reflect the liquid core of each session rather than
#: the exchange's nominal hours, which is what a filter actually wants.
DEFAULT_SESSIONS: tuple[SessionDef, ...] = (
    SessionDef(name="ASIA", tz="Asia/Tokyo", start=time(9, 0), end=time(18, 0)),
    SessionDef(name="LONDON", tz="Europe/London", start=time(8, 0), end=time(17, 0)),
    SessionDef(name="NEWYORK", tz="America/New_York", start=time(8, 0), end=time(17, 0)),
    # The London open burst and the London/NY overlap are the two highest-quality windows.
    SessionDef(name="LONDON_OPEN", tz="Europe/London", start=time(8, 0), end=time(10, 0)),
    SessionDef(name="NY_OVERLAP", tz="America/New_York", start=time(8, 0), end=time(12, 0)),
)


class ServerClockMapping(BaseModel):
    """Measured relationship between broker server time and UTC.

    Never assume UTC+2/+3. The offset is measured by comparing a server timestamp with the
    UTC time at which it was observed, then rounded to the nearest 15 minutes to absorb
    network latency and clock skew without inventing a bogus offset.
    """

    model_config = ConfigDict(frozen=True)

    offset_seconds: int = 0
    measured_at_ms: int = 0
    samples: int = 0

    @classmethod
    def measure(
        cls, server_epoch_ms: int, utc_epoch_ms: int, samples: int = 1
    ) -> ServerClockMapping:
        raw = (server_epoch_ms - utc_epoch_ms) / 1000.0
        quantum = 900.0  # 15 minutes
        offset = int(round(raw / quantum) * quantum)
        return cls(offset_seconds=offset, measured_at_ms=utc_epoch_ms, samples=samples)

    @property
    def offset_hours(self) -> float:
        return self.offset_seconds / 3600.0

    def to_server(self, utc_ms: int) -> int:
        return utc_ms + self.offset_seconds * 1000

    def to_utc(self, server_ms: int) -> int:
        return server_ms - self.offset_seconds * 1000

    def is_stale(self, now_ms: int, max_age_hours: float = 12.0) -> bool:
        """DST shifts move the offset, so a mapping older than half a day is not trusted."""
        if not self.measured_at_ms:
            return True
        return (now_ms - self.measured_at_ms) > max_age_hours * 3_600_000


class NewsEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: int  # epoch ms UTC
    currency: str
    impact: str  # HIGH / MEDIUM / LOW
    title: str = ""

    @property
    def is_high_impact(self) -> bool:
        return self.impact.upper() == "HIGH"


class NewsCalendar:
    """Economic-event blackout windows.

    ``available`` is deliberately exposed rather than hidden: a system that silently trades
    through NFP because the calendar file was missing is worse than one that refuses, and
    both behaviours are legitimate -- the choice belongs in configuration, not in this class.
    """

    def __init__(self, events: Iterable[NewsEvent] = (), *, available: bool = True) -> None:
        self.events: list[NewsEvent] = sorted(events, key=lambda e: e.ts)
        self.available = available

    @classmethod
    def from_file(cls, path: Path | str) -> NewsCalendar:
        """Load a JSON array of events. A missing file yields an *unavailable* calendar
        rather than an empty one, so callers can distinguish "no events" from "no data"."""
        p = Path(path)
        if not p.exists():
            return cls((), available=False)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            return cls([NewsEvent(**e) for e in raw], available=True)
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls((), available=False)

    def blackout(
        self,
        now_ms: int,
        currencies: Iterable[str],
        *,
        before_minutes: int = 15,
        after_minutes: int = 15,
        min_impact: str = "HIGH",
    ) -> NewsEvent | None:
        """Return the event causing a blackout at ``now_ms``, or None."""
        wanted = {c.upper() for c in currencies}
        rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        floor = rank.get(min_impact.upper(), 2)
        lo = now_ms - after_minutes * 60_000
        hi = now_ms + before_minutes * 60_000
        for e in self.events:
            if e.ts > hi:
                break  # sorted: nothing later can match
            if e.ts < lo:
                continue
            if rank.get(e.impact.upper(), 0) < floor:
                continue
            if e.currency.upper() in wanted:
                return e
        return None


class TradingCalendar:
    """Answers the time-based questions the risk engine and strategies ask."""

    def __init__(
        self,
        sessions: Iterable[SessionDef] = DEFAULT_SESSIONS,
        *,
        server: ServerClockMapping | None = None,
        rollover_before_minutes: int = 60,
        rollover_after_minutes: int = 60,
        news: NewsCalendar | None = None,
    ) -> None:
        self.sessions = {s.name: s for s in sessions}
        self.server = server or ServerClockMapping()
        self.rollover_before = rollover_before_minutes
        self.rollover_after = rollover_after_minutes
        self.news = news or NewsCalendar((), available=False)

    def active_sessions(self, ts_ms: int) -> tuple[str, ...]:
        dt = ms_to_dt(ts_ms)
        return tuple(name for name, s in self.sessions.items() if s.contains(dt))

    def in_session(self, ts_ms: int, names: Iterable[str]) -> bool:
        wanted = set(names)
        if not wanted:
            return True  # empty filter means "no session restriction"
        return bool(wanted & set(self.active_sessions(ts_ms)))

    def is_rollover(self, ts_ms: int) -> bool:
        """True inside the window around **broker server midnight**.

        Derived from the measured offset, so it follows the broker's day boundary rather
        than a guessed UTC hour. This is where spreads blow out and prints are unreliable.
        """
        server_dt = ms_to_dt(self.server.to_server(ts_ms))
        minutes_from_midnight = server_dt.hour * 60 + server_dt.minute
        if minutes_from_midnight >= 24 * 60 - self.rollover_before:
            return True
        return minutes_from_midnight < self.rollover_after

    def is_weekend(self, ts_ms: int) -> bool:
        """Market-closed window: Friday 21:00 UTC to Sunday 21:00 UTC (approximate, and
        deliberately conservative at both ends -- Friday's close and Sunday's open both have
        unreliable liquidity)."""
        dt = ms_to_dt(ts_ms)
        wd, hour = dt.weekday(), dt.hour
        if wd == 5:  # Saturday
            return True
        if wd == 4 and hour >= 21:  # Friday evening
            return True
        return wd == 6 and hour < 21  # Sunday before the open

    def is_triple_swap_day(self, ts_ms: int, rollover_weekday: int = 2) -> bool:
        """True on the day carrying 3x swap (MT5 default is Wednesday, index 2).

        The broker's ``swap_rollover_3days`` field is authoritative and is passed through
        from the symbol spec; the default here is only a fallback.
        """
        return ms_to_dt(self.server.to_server(ts_ms)).weekday() == rollover_weekday

    def news_blackout(self, ts_ms: int, currencies: Iterable[str], **kw) -> NewsEvent | None:
        return self.news.blackout(ts_ms, currencies, **kw)

    def describe(self, ts_ms: int) -> dict[str, object]:
        """Everything time-related about an instant, for the decision record and dashboard."""
        return {
            "utc": ms_to_dt(ts_ms).isoformat(),
            "server": ms_to_dt(self.server.to_server(ts_ms)).isoformat(),
            "server_offset_hours": self.server.offset_hours,
            "sessions": list(self.active_sessions(ts_ms)),
            "rollover": self.is_rollover(ts_ms),
            "weekend": self.is_weekend(ts_ms),
            "news_calendar_available": self.news.available,
        }


def currencies_of(symbol: str) -> tuple[str, ...]:
    """Currencies whose news should gate a symbol.

    Handles broker suffixes (``XAUUSD.m``, ``EURUSD_i``, ``GOLD``) by stripping non-alpha
    characters and matching the leading six letters. Metals map to their metal code plus the
    quote currency; gold is dominated by USD data regardless of the quote leg.
    """
    core = "".join(ch for ch in symbol.upper() if ch.isalpha())
    if core.startswith("GOLD"):
        return ("XAU", "USD")
    if core.startswith("SILVER"):
        return ("XAG", "USD")
    if len(core) >= 6:
        base, quote = core[:3], core[3:6]
        return (base, quote)
    return (core,)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    """Convenience for tests and config: an aware UTC datetime."""
    return datetime(y, m, d, hh, mm, tzinfo=UTC)
