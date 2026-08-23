"""Broker instrument specification and the money maths that depends on it.

Every price and volume in the system passes through this module before it reaches a venue.
Hardcoding digits, point size or pip value is the single most common source of "works on my
broker" bugs: gold quotes at 2 or 3 digits depending on broker, FX at 4 or 5, and symbol
names carry suffixes (``XAUUSD.m``, ``GOLD``, ``XAUUSD_i``). Nothing in ATLAS may assume
these values -- they are read from the venue at startup and carried in ``SymbolSpec``.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atlas.core.enums import FillPolicy


class SymbolSpec(BaseModel):
    """Immutable snapshot of a broker's contract specification for one symbol.

    Sourced from ``SymbolInfoDouble``/``SymbolInfoInteger`` on the terminal side. Treated as
    slowly-changing: refreshed on connect and on a configurable interval, and any change is
    journalled because it can silently invalidate open-position risk.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    digits: int = Field(ge=0, le=8)
    point: float = Field(gt=0)
    tick_size: float = Field(gt=0)
    tick_value: float = Field(
        gt=0, description="Account-currency value of one tick_size move for 1.0 lot"
    )
    contract_size: float = Field(gt=0)

    volume_min: float = Field(gt=0)
    volume_max: float = Field(gt=0)
    volume_step: float = Field(gt=0)

    stops_level_points: int = Field(
        default=0, ge=0, description="Minimum SL/TP distance from price, in points"
    )
    freeze_level_points: int = Field(
        default=0, ge=0, description="Distance within which modification is refused"
    )

    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""

    margin_initial: float = Field(
        default=0.0, ge=0, description="Margin per lot; 0 means derive from leverage"
    )
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_mode: int = 0
    swap_rollover_3days: int = 3  # ISO weekday index used by MT5 for triple swap (3 = Wednesday)

    filling_modes: tuple[FillPolicy, ...] = (FillPolicy.FOK,)
    trade_allowed: bool = True

    # --- derived -----------------------------------------------------------------

    @field_validator("filling_modes", mode="before")
    @classmethod
    def _coerce_filling(cls, v: object) -> object:
        if isinstance(v, (list, tuple)):
            return tuple(FillPolicy(x) for x in v)
        return v

    @property
    def ticks_per_point(self) -> float:
        """How many ``tick_size`` units make one ``point``.

        Usually 1.0. It differs on instruments where the broker quotes a tick size coarser
        than the point (some indices, some crypto CFDs)."""
        return self.point / self.tick_size

    @property
    def value_per_point_per_lot(self) -> float:
        """Account-currency P/L of a one-point move on 1.0 lot."""
        return self.tick_value * self.ticks_per_point

    @property
    def pip_size(self) -> float:
        """Conventional pip. For 3/5-digit quotes a pip is ten points; otherwise one point.

        Used only for human display -- all internal maths is in points, never pips.
        """
        return self.point * 10 if self.digits in (3, 5) else self.point

    # --- conversions -------------------------------------------------------------

    def normalize_price(self, price: float) -> float:
        """Round a price to a valid tick of this instrument.

        Rounds to ``tick_size`` first (the actual tradeable grid) and then to ``digits`` to
        remove binary float residue that would otherwise be rejected as an invalid price.
        """
        if self.tick_size <= 0:
            return round(price, self.digits)
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, self.digits)

    def normalize_volume(self, volume: float) -> float:
        """Snap a volume down to the broker's lot grid and clamp to [min, max].

        Rounding **down** is deliberate: rounding up would silently exceed the risk budget
        that produced this volume. Returns 0.0 when the requested size is below the minimum
        lot, which callers must treat as "cannot trade this setup", not as an error.
        """
        if volume <= 0:
            return 0.0
        steps = math.floor(round(volume / self.volume_step, 9))
        vol = steps * self.volume_step
        vol = round(vol, _decimals_of(self.volume_step))
        if vol < self.volume_min:
            return 0.0
        return min(vol, self.volume_max)

    def points_between(self, a: float, b: float) -> float:
        """Absolute distance between two prices expressed in points."""
        return abs(a - b) / self.point

    def price_offset(self, price: float, points: float) -> float:
        """Return ``price`` shifted by ``points`` (signed), normalised to the tick grid."""
        return self.normalize_price(price + points * self.point)

    def money_for_points(self, points: float, volume: float) -> float:
        """Account-currency value of a ``points`` move on ``volume`` lots."""
        return points * self.value_per_point_per_lot * volume

    def volume_for_risk(self, risk_money: float, stop_points: float) -> float:
        """Raw (un-normalised) volume that puts exactly ``risk_money`` at risk.

        Caller must pass the result through :meth:`normalize_volume`. Kept separate so the
        risk engine can report both the ideal and the achievable size -- the difference is
        real risk drift and is worth surfacing.
        """
        if stop_points <= 0:
            raise ValueError("stop_points must be positive")
        denom = stop_points * self.value_per_point_per_lot
        if denom <= 0:
            raise ValueError(f"non-positive point value for {self.name}")
        return risk_money / denom

    def min_stop_distance_points(self, safety_points: int = 0) -> float:
        """Smallest SL/TP distance the broker will accept, plus an optional safety margin."""
        return float(self.stops_level_points + safety_points)

    def is_stop_distance_valid(self, price: float, stop: float, safety_points: int = 0) -> bool:
        return self.points_between(price, stop) >= self.min_stop_distance_points(safety_points)

    def preferred_filling(self) -> FillPolicy:
        """Pick a filling mode the broker actually supports, preferring IOC then FOK.

        IOC first because a partially fillable market order is better than an outright
        rejection when liquidity is thin; RETURN is last because it leaves a resting
        remainder that the router would then have to manage.
        """
        for mode in (FillPolicy.IOC, FillPolicy.FOK, FillPolicy.RETURN):
            if mode in self.filling_modes:
                return mode
        return FillPolicy.FOK


def _decimals_of(step: float) -> int:
    """Number of decimal places implied by a lot step (0.01 -> 2)."""
    s = f"{step:.10f}".rstrip("0")
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])
