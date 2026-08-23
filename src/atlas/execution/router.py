"""Order router: idempotent submission, bounded retry, and in-flight tracking.

The failure this module exists to prevent
------------------------------------------
A market order is sent. The request times out. Was it filled? Nobody knows. The naive
response is to resend, and the classic outcome is two positions where one was intended --
double the risk, at the worst possible moment, with the risk engine's caps silently violated
because it only ever approved one.

MT5 has no server-side idempotency key, so the only safe answer is **look before you retry**
(ADR-013). Every request carries a ``client_order_id`` derived deterministically from the
decision that produced it; before any retry the router asks the venue whether that id already
produced a position. If the venue cannot answer (``capabilities.client_id_lookup`` is False),
the router refuses to retry at all -- an unfilled order is a missed trade, a double fill is a
loss, and those are not symmetric.

Retry policy: only ``TransientError`` and a small set of broker retcodes are retried, with
exponential backoff and a hard attempt cap. Everything else fails immediately and loudly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from atlas.core.clock import Clock
from atlas.core.enums import ExitReason, OrderStatus
from atlas.core.errors import TransientError, VenueError
from atlas.core.trading import OrderRequest, OrderResult, Position
from atlas.execution.venue import ExecutionVenue

#: MT5 return codes that describe a *temporary* condition. Anything not listed is treated as
#: final: retrying an "invalid stops" or "no money" rejection just repeats the same failure.
RETRYABLE_RETCODES: frozenset[int] = frozenset(
    {
        10004,  # REQUOTE
        10008,  # PLACED but not confirmed
        10010,  # DONE_PARTIAL
        10018,  # MARKET_CLOSED (may reopen)
        10021,  # PRICE_OFF -- no quotes to process the request
        10024,  # TOO_MANY_REQUESTS
        10031,  # CONNECTION lost
    }
)

#: Codes that mean the request was definitively rejected and must not be resent unchanged.
FINAL_RETCODES: frozenset[int] = frozenset(
    {
        10006,  # REJECT
        10013,  # INVALID request
        10014,  # INVALID_VOLUME
        10015,  # INVALID_PRICE
        10016,  # INVALID_STOPS
        10019,  # NO_MONEY
        10017,  # TRADE_DISABLED
        10027,  # CLIENT_DISABLES_AT
    }
)


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    backoff: float = 2.0

    def delay_for(self, attempt: int) -> float:
        return min(self.max_delay_s, self.base_delay_s * (self.backoff ** max(0, attempt - 1)))


@dataclass(slots=True)
class SubmissionOutcome:
    """What happened to one logical order, across however many attempts it took."""

    client_order_id: str
    decision_id: str
    accepted: bool
    status: OrderStatus
    attempts: int
    result: OrderResult | None = None
    position: Position | None = None
    error: str = ""
    log: list[str] = field(default_factory=list)

    @property
    def resolved_by_lookup(self) -> bool:
        return any("resolved by lookup" in line for line in self.log)


class OrderRouter:
    def __init__(
        self,
        venue: ExecutionVenue,
        clock: Clock,
        *,
        policy: RetryPolicy | None = None,
        on_event=None,
    ) -> None:
        self.venue = venue
        self.clock = clock
        self.policy = policy or RetryPolicy()
        self._on_event = on_event
        #: Orders submitted but not yet known to have filled or failed.
        self.in_flight: dict[str, OrderRequest] = {}

    def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is not None:
            self._on_event(kind, payload)

    async def submit(self, request: OrderRequest) -> SubmissionOutcome:
        log: list[str] = []
        self.in_flight[request.client_order_id] = request
        attempt = 0
        last_error = ""

        while attempt < self.policy.max_attempts:
            attempt += 1
            # Before every attempt after the first, check whether a previous attempt landed.
            if attempt > 1:
                existing = await self._lookup(request.client_order_id, log)
                if existing is not None:
                    log.append(f"attempt {attempt}: resolved by lookup, position "
                               f"{existing.ticket} already exists")
                    self.in_flight.pop(request.client_order_id, None)
                    return SubmissionOutcome(
                        client_order_id=request.client_order_id,
                        decision_id=request.decision_id, accepted=True,
                        status=OrderStatus.FILLED, attempts=attempt, position=existing, log=log,
                    )
                if not self.venue.capabilities.client_id_lookup:
                    log.append("venue cannot look up by client id; refusing to retry blind")
                    self.in_flight.pop(request.client_order_id, None)
                    return SubmissionOutcome(
                        client_order_id=request.client_order_id,
                        decision_id=request.decision_id, accepted=False,
                        status=OrderStatus.REJECTED, attempts=attempt,
                        error=f"{last_error} (not retried: no idempotency lookup available)",
                        log=log,
                    )

            try:
                result = await self.venue.submit(request)
            except TransientError as exc:
                last_error = f"transient: {exc}"
                log.append(f"attempt {attempt}: {last_error}")
                self._emit("order.retry", {"client_order_id": request.client_order_id,
                                           "attempt": attempt, "error": str(exc)})
                await self._backoff(attempt)
                continue
            except VenueError as exc:
                log.append(f"attempt {attempt}: venue rejected -- {exc}")
                self.in_flight.pop(request.client_order_id, None)
                return SubmissionOutcome(
                    client_order_id=request.client_order_id, decision_id=request.decision_id,
                    accepted=False, status=OrderStatus.REJECTED, attempts=attempt,
                    error=str(exc), log=log,
                )

            if result.accepted:
                log.append(f"attempt {attempt}: accepted ({result.status}, "
                           f"retcode {result.retcode})")
                if result.status is not OrderStatus.PENDING_NEW:
                    self.in_flight.pop(request.client_order_id, None)
                return SubmissionOutcome(
                    client_order_id=request.client_order_id, decision_id=request.decision_id,
                    accepted=True, status=result.status, attempts=attempt, result=result,
                    log=log,
                )

            last_error = f"retcode {result.retcode}: {result.retcode_text}"
            log.append(f"attempt {attempt}: {last_error}")
            if result.retcode in FINAL_RETCODES or result.retcode not in RETRYABLE_RETCODES:
                self.in_flight.pop(request.client_order_id, None)
                return SubmissionOutcome(
                    client_order_id=request.client_order_id, decision_id=request.decision_id,
                    accepted=False, status=OrderStatus.REJECTED, attempts=attempt,
                    result=result, error=last_error, log=log,
                )
            self._emit("order.retry", {"client_order_id": request.client_order_id,
                                       "attempt": attempt, "retcode": result.retcode})
            await self._backoff(attempt)

        # Attempts exhausted. One final lookup: the last attempt may have landed after its
        # response was lost, and reporting a failure for a live position is dangerous.
        existing = await self._lookup(request.client_order_id, log)
        self.in_flight.pop(request.client_order_id, None)
        if existing is not None:
            log.append("resolved by lookup after exhausting attempts")
            return SubmissionOutcome(
                client_order_id=request.client_order_id, decision_id=request.decision_id,
                accepted=True, status=OrderStatus.FILLED, attempts=attempt,
                position=existing, log=log,
            )
        return SubmissionOutcome(
            client_order_id=request.client_order_id, decision_id=request.decision_id,
            accepted=False, status=OrderStatus.REJECTED, attempts=attempt,
            error=last_error or "exhausted retry attempts", log=log,
        )

    async def _lookup(self, client_order_id: str, log: list[str]) -> Position | None:
        if not self.venue.capabilities.client_id_lookup:
            return None
        try:
            return await self.venue.find_by_client_id(client_order_id)
        except Exception as exc:
            log.append(f"idempotency lookup failed: {exc}")
            return None

    async def _backoff(self, attempt: int) -> None:
        await self.clock.sleep(self.policy.delay_for(attempt))

    # -- modification and closing ----------------------------------------------------

    async def modify(
        self, ticket: int, *, stop_loss: float | None = None, take_profit: float | None = None
    ) -> OrderResult:
        """Modify a position's protective levels.

        Modification is naturally idempotent -- setting a stop to a value it already has is a
        no-op at the broker -- so it retries without a lookup. What it must NOT do is widen a
        stop; that check belongs to the caller (the trade manager), which knows the intent.
        """
        last: OrderResult | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                last = await self.venue.modify_position(
                    ticket, stop_loss=stop_loss, take_profit=take_profit
                )
            except TransientError:
                await self._backoff(attempt)
                continue
            if last.accepted or last.retcode in FINAL_RETCODES:
                return last
            await self._backoff(attempt)
        return last or OrderResult(
            client_order_id="", accepted=False, status=OrderStatus.REJECTED,
            retcode_text="modify exhausted retries",
        )

    async def close(
        self, ticket: int, *, volume: float | None = None,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> OrderResult:
        """Close a position.

        Retried more persistently than an open, and deliberately so: failing to open is a
        missed opportunity, failing to close is unbounded risk. A close that keeps failing
        escalates to the caller, which halts the engine rather than leaving the position
        unmanaged.
        """
        last: OrderResult | None = None
        for attempt in range(1, self.policy.max_attempts + 2):
            try:
                last = await self.venue.close_position(ticket, volume=volume, reason=reason)
            except TransientError:
                await self._backoff(attempt)
                continue
            if last.accepted:
                return last
            if last.retcode in FINAL_RETCODES and last.retcode != 10006:
                return last
            await self._backoff(attempt)
        return last or OrderResult(
            client_order_id="", accepted=False, status=OrderStatus.REJECTED,
            retcode_text="close exhausted retries",
        )


async def gather_with_limit(coros, limit: int = 4):
    """Run coroutines with bounded concurrency.

    Brokers rate-limit, and MT5 bridges are single-threaded on the terminal side; firing
    twenty simultaneous requests is a reliable way to get ``TRADE_RETCODE_TOO_MANY_REQUESTS``.
    """
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))
