"""Order, position and account value objects."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from atlas.core.enums import (
    ExitReason,
    FillPolicy,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)


class OrderRequest(BaseModel):
    """An instruction to a venue. Immutable; a modification is a new request.

    ``client_order_id`` is the idempotency key (ADR-013). It is derived deterministically
    from the originating decision so that a retry after a timeout can be resolved by
    look-up rather than by blind resubmission.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str = Field(max_length=16)
    decision_id: str
    symbol: str
    side: Side
    order_type: OrderType = OrderType.MARKET
    volume: float = Field(gt=0)
    price: float | None = None  # required for LIMIT/STOP; ignored for MARKET
    stop_loss: float | None = None
    take_profit: float | None = None
    deviation_points: int = Field(
        default=20, ge=0, description="Max slippage tolerated on market orders"
    )
    magic: int = 0
    comment: str = ""
    tif: TimeInForce = TimeInForce.GTC
    expiry_ms: int | None = None
    fill_policy: FillPolicy | None = None  # None -> venue picks from symbol spec


class OrderResult(BaseModel):
    """A venue's answer to an order request.

    ``retcode`` is the raw broker return code (MT5 ``TRADE_RETCODE_*``); it is preserved
    verbatim because the mapping to our ``status`` is lossy and the raw code is what a
    support ticket to a broker needs.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    accepted: bool
    status: OrderStatus
    retcode: int = 0
    retcode_text: str = ""
    order_ticket: int = 0
    deal_ticket: int = 0
    position_ticket: int = 0
    filled_volume: float = 0.0
    fill_price: float = 0.0
    requested_price: float = 0.0
    commission: float = 0.0
    ts: int = 0
    message: str = ""

    @property
    def slippage_points(self) -> float | None:
        """Signed fill deviation in price units; None when either price is unknown."""
        if not self.fill_price or not self.requested_price:
            return None
        return self.fill_price - self.requested_price


class Position(BaseModel):
    """An open position as reported by the venue. Broker truth, not our belief."""

    model_config = ConfigDict(frozen=True)

    ticket: int
    symbol: str
    side: Side
    volume: float
    open_price: float
    open_time: int
    stop_loss: float | None = None
    take_profit: float | None = None
    current_price: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""

    @property
    def net_profit(self) -> float:
        return self.profit + self.swap + self.commission

    def unrealised_points(self, point: float) -> float:
        if not self.current_price or point <= 0:
            return 0.0
        return (self.current_price - self.open_price) * self.side.sign / point


class PendingOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticket: int
    symbol: str
    side: Side
    order_type: OrderType
    volume: float
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    setup_time: int = 0
    expiry_ms: int | None = None
    magic: int = 0
    comment: str = ""


class AccountState(BaseModel):
    """Account snapshot. ``equity`` -- not ``balance`` -- drives every risk decision, because
    drawdown limits (especially prop-firm ones) are breached on floating loss."""

    model_config = ConfigDict(frozen=True)

    login: int = 0
    server: str = ""
    currency: str = "USD"
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    leverage: int = 0
    ts: int = 0
    trade_allowed: bool = True

    @property
    def floating_pl(self) -> float:
        return self.equity - self.balance

    @property
    def margin_used_pct(self) -> float:
        return 0.0 if self.equity <= 0 else 100.0 * self.margin / self.equity


class Trade(BaseModel):
    """A completed round trip, reconstructed from fills. The unit of performance analysis.

    ``r_multiple`` is the primary metric: profit expressed in multiples of the risk that was
    actually taken at entry. Currency P/L is not comparable across position sizes; R is.
    """

    model_config = ConfigDict(frozen=True)

    trade_id: str
    decision_id: str
    symbol: str
    side: Side
    volume: float
    entry_price: float
    entry_time: int
    exit_price: float
    exit_time: int
    initial_stop: float
    initial_target: float | None = None
    risk_money: float = 0.0
    gross_profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    exit_reason: ExitReason = ExitReason.UNKNOWN
    mae_points: float = 0.0  # maximum adverse excursion
    mfe_points: float = 0.0  # maximum favourable excursion
    strategy: str = ""
    tags: tuple[str, ...] = ()

    @property
    def net_profit(self) -> float:
        return self.gross_profit + self.commission + self.swap

    @property
    def r_multiple(self) -> float:
        if self.risk_money <= 0:
            return 0.0
        return self.net_profit / self.risk_money

    @property
    def duration_ms(self) -> int:
        return max(0, self.exit_time - self.entry_time)

    @property
    def is_win(self) -> bool:
        return self.net_profit > 0
