"""Configuration.

One file describes a whole run: which symbols, which strategy and parameters, which risk
limits, which venue. It is journalled verbatim at run start, so any result can be reproduced
from its own journal without reference to what happened to be on disk at the time.

Unknown keys are a hard error. A typo that silently leaves a parameter at its default is the
kind of bug that shows up as an inexplicable difference between two runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas.core.enums import RunMode, Timeframe
from atlas.core.errors import ConfigError
from atlas.core.instrument import SymbolSpec
from atlas.features.frame import FeatureConfig
from atlas.risk.config import RiskConfig


class VenueSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = "sim"  # sim | mt5_bridge
    #: ZeroMQ endpoints for the MT5 bridge. The REQ/REP socket carries commands; the
    #: PUB/SUB socket carries pushed quotes and trade transactions.
    command_endpoint: str = "tcp://127.0.0.1:5555"
    event_endpoint: str = "tcp://127.0.0.1:5556"
    request_timeout_ms: int = 5000
    #: Shared secret. Read from the ATLAS_BRIDGE_TOKEN environment variable when blank, so a
    #: credential never has to live in a config file.
    token: str = ""
    starting_balance: float = 10_000.0
    leverage: int = 100

    def resolved_token(self) -> str:
        return self.token or os.environ.get("ATLAS_BRIDGE_TOKEN", "")


class SymbolSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    strategy: str = "SM1"
    parameters: dict[str, Any] = Field(default_factory=dict)
    #: Optional spec override. In live runs the spec comes from the venue and this is only a
    #: fallback for offline work.
    spec: SymbolSpec | None = None
    data_file: str | None = None
    server_offset_hours: float | None = None


class AtlasSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "atlas"
    mode: RunMode = RunMode.BACKTEST
    base_timeframe: Timeframe = Timeframe.M5
    symbols: list[SymbolSettings] = Field(default_factory=list)
    venue: VenueSettings = Field(default_factory=VenueSettings)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    features: FeatureConfig | None = None
    magic: int = 20260823
    allowed_sessions: list[str] = Field(default_factory=list)
    news_policy: str = "warn"  # warn | block
    news_file: str | None = None
    warmup_bars: int = 1500
    feature_window: int = 1500
    journal_dir: str = "runs"
    state_dir: str = "runs/state"
    api_host: str = "127.0.0.1"
    api_port: int = 8080

    @classmethod
    def load(cls, path: Path | str) -> AtlasSettings:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"no such config file: {p}")
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{p} is not valid JSON: {exc}") from exc
        try:
            return cls(**raw)
        except Exception as exc:  # pydantic validation error
            raise ConfigError(f"{p}: {exc}") from exc

    def feature_config(self) -> FeatureConfig:
        return self.features or FeatureConfig()

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")


def example_settings() -> AtlasSettings:
    """A complete, runnable example. Emitted by ``atlas init``."""
    gold = SymbolSpec(
        name="XAUUSD", description="Gold vs US Dollar", digits=2, point=0.01, tick_size=0.01,
        tick_value=1.0, contract_size=100, volume_min=0.01, volume_max=50.0,
        volume_step=0.01, stops_level_points=50, currency_base="XAU", currency_profit="USD",
        currency_margin="USD", margin_initial=2000.0, swap_long=-4.5, swap_short=1.2,
        swap_mode=1, swap_rollover_3days=3,
    )
    return AtlasSettings(
        name="atlas-xauusd",
        symbols=[SymbolSettings(
            symbol="XAUUSD", strategy="SM1",
            parameters={"htf": "H4", "mtf": "H1", "ltf": "M15"}, spec=gold,
        )],
        allowed_sessions=["LONDON", "NEWYORK"],
        risk=RiskConfig(risk_per_trade_pct=0.5, max_total_risk_pct=1.5,
                        daily_loss_limit_pct=3.0, total_drawdown_limit_pct=8.0),
    )
