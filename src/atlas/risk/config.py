"""Risk configuration.

Every limit here is enforced in code with a hard halt, and every threshold is deliberately
set *inside* whatever external limit applies. If a prop firm's daily limit is 5%, ATLAS halts
at 4%: the last percent is consumed by slippage on the exit, the spread, swap, and the trade
that was already open when the limit was reached. A system that halts exactly at the external
limit breaches it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # --- per-trade -------------------------------------------------------------
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=5.0)
    #: Conviction scales size between this fraction of base risk and 1.0. Never above 1.0 --
    #: a "high conviction" trade is a normal-sized trade, not an oversized one.
    min_risk_fraction: float = Field(default=0.5, gt=0, le=1.0)
    conviction_scaling: bool = True

    # --- aggregate -------------------------------------------------------------
    max_total_risk_pct: float = Field(default=2.0, gt=0, le=20.0)
    max_positions: int = Field(default=3, ge=1)
    max_positions_per_symbol: int = Field(default=1, ge=1)
    #: Symbols sharing a group are counted as ONE exposure. Long gold and short EURUSD are
    #: substantially the same bet; counting them separately understates real risk.
    correlation_groups: dict[str, str] = Field(
        default_factory=lambda: {
            "XAUUSD": "USD_RISK", "XAGUSD": "USD_RISK", "GOLD": "USD_RISK",
            "EURUSD": "USD_MAJOR", "GBPUSD": "USD_MAJOR", "AUDUSD": "USD_RISK",
        }
    )
    max_group_risk_pct: float = Field(default=1.5, gt=0, le=20.0)

    # --- drawdown limits (halt thresholds, set inside any external limit) --------
    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=50.0)
    total_drawdown_limit_pct: float = Field(default=8.0, gt=0, le=90.0)
    #: Trailing drawdown follows the equity high-water mark (strict, prop-style). Static
    #: measures from the initial balance.
    trailing_drawdown: bool = True
    max_consecutive_losses: int = Field(default=6, ge=1)

    # --- margin ----------------------------------------------------------------
    min_free_margin_pct: float = Field(default=30.0, ge=0, le=100.0)

    # --- daily reset -----------------------------------------------------------
    #: The reset boundary is in the *firm's* timezone, which is rarely the broker's server
    #: time. Both are needed and they are reconciled explicitly rather than assumed equal.
    daily_reset_tz: str = "UTC"
    daily_reset_hour: int = Field(default=0, ge=0, le=23)

    # --- operational -----------------------------------------------------------
    #: Reject a signal whose realised (post-rounding) risk drifts more than this fraction
    #: below the intended risk -- a sign the lot step is too coarse for the account size.
    max_size_drift: float = Field(default=0.35, ge=0, le=1.0)
    allow_new_trades: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> RiskConfig:
        if self.max_total_risk_pct < self.risk_per_trade_pct:
            raise ValueError(
                f"max_total_risk_pct ({self.max_total_risk_pct}) is below the per-trade risk "
                f"({self.risk_per_trade_pct}): no trade could ever be opened"
            )
        if self.max_group_risk_pct < self.risk_per_trade_pct:
            raise ValueError(
                f"max_group_risk_pct ({self.max_group_risk_pct}) is below the per-trade risk"
            )
        if self.daily_loss_limit_pct >= self.total_drawdown_limit_pct:
            raise ValueError(
                "daily_loss_limit_pct must be below total_drawdown_limit_pct, otherwise the "
                "daily limit can never bind before the total one"
            )
        return self

    def group_of(self, symbol: str) -> str:
        """Correlation group for a symbol, tolerating broker suffixes (``XAUUSD.m``)."""
        core = "".join(ch for ch in symbol.upper() if ch.isalpha())
        if core in self.correlation_groups:
            return self.correlation_groups[core]
        for key, grp in self.correlation_groups.items():
            if core.startswith(key):
                return grp
        return f"SINGLE:{core}"

    def consecutive_losses_to_daily_breach(self) -> float:
        """How many full-risk losers end the day. The single most behaviour-changing number
        to put in front of someone sizing an account."""
        return self.daily_loss_limit_pct / self.risk_per_trade_pct
