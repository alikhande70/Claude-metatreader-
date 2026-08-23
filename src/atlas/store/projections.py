"""Projections over the event journal.

ADR-011: the dashboard shows only what the journal actually says. Where a value is not known
it is reported as ``None`` and rendered as "unknown" -- never as a plausible default. A
dashboard that interpolates produces confident wrong beliefs during exactly the incidents
where you need truth.

Every projection carries the sequence number and timestamp of the event it was derived from,
so the UI can tell the operator how old what they are looking at is, and can mark it stale
rather than presenting month-old numbers as current.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas.analytics.metrics import PerformanceMetrics, compute_metrics
from atlas.bus.events import Event, EventKind
from atlas.bus.journal import Journal
from atlas.core.trading import Trade


@dataclass(slots=True)
class Stamped:
    """A value with the journal position it came from."""

    value: Any = None
    seq: int = 0
    ts: int = 0

    @property
    def known(self) -> bool:
        return self.seq > 0

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "seq": self.seq, "ts": self.ts, "known": self.known}


@dataclass(slots=True)
class ProjectionState:
    run_id: str = ""
    mode: Stamped = field(default_factory=Stamped)
    engine_state: Stamped = field(default_factory=Stamped)
    account: Stamped = field(default_factory=Stamped)
    venue: Stamped = field(default_factory=Stamped)
    kill_switch: Stamped = field(default_factory=Stamped)
    heartbeat: Stamped = field(default_factory=Stamped)
    reconcile: Stamped = field(default_factory=Stamped)
    started: Stamped = field(default_factory=Stamped)
    stopped: Stamped = field(default_factory=Stamped)

    positions: dict[int, dict] = field(default_factory=dict)
    equity: list[tuple[int, float, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    decision_outcomes: dict[str, dict] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    last_seq: int = 0
    last_ts: int = 0

    #: Bounded so a long-running session cannot exhaust memory. The journal remains the
    #: complete record; these are a live view, not an archive.
    max_decisions: int = 5000
    max_equity: int = 20_000
    #: Uniform-stride decimation state for the equity curve (see the fold for why).
    equity_seen: int = 0
    equity_stride: int = 1
    #: True when the last element is an off-grid "tip" kept so the curve always ends at now.
    equity_tip: bool = False


class ProjectionStore:
    """Folds journal events into the views the API serves.

    Reading is incremental: the API polls ``refresh()``, which consumes only events newer
    than the last one folded. That keeps a dashboard on a multi-hundred-thousand-event
    journal responsive without holding the whole thing in memory.

    **Thread safety is required, not optional.** FastAPI runs synchronous endpoints in a
    threadpool, so a dashboard polling four endpoints at once calls ``refresh()`` from four
    threads simultaneously. Without a lock they all read the same ``last_seq``, all fetch the
    same batch, and all fold it -- which duplicated trades in the UI while the journal itself
    was perfectly clean. Both ``refresh`` and ``apply`` are guarded.
    """

    def __init__(self, run_dir: Path | str, *, max_decisions: int = 5000) -> None:
        self.run_dir = Path(run_dir)
        self.state = ProjectionState(max_decisions=max_decisions)
        self._journal: Journal | None = None
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return (self.run_dir / "events.jsonl").exists()

    def _open(self) -> Journal | None:
        if self._journal is None and self.available:
            self._journal = Journal(self.run_dir, mirror_sqlite=True)
        return self._journal

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._journal is not None:
            with contextlib.suppress(Exception):
                self._journal.close()
            self._journal = None

    def refresh(self, limit: int = 20_000) -> int:
        with self._lock:
            j = self._open()
            if j is None:
                return 0
            folded = 0
            while True:
                batch = j.read(self.state.last_seq, limit=min(limit, 5000))
                if not batch:
                    break
                for event in batch:
                    self._apply_locked(event)
                    folded += 1
                if folded >= limit:
                    break
            return folded

    # -- folding ------------------------------------------------------------------

    def apply(self, event: Event) -> None:
        with self._lock:
            self._apply_locked(event)

    def _apply_locked(self, event: Event) -> None:
        # Already folded. Making the fold idempotent means a duplicate delivery -- a live
        # journal listener racing a poll-driven refresh -- cannot double-count.
        if event.seq <= self.state.last_seq:
            return
        s = self.state
        s.last_seq = max(s.last_seq, event.seq)
        s.last_ts = max(s.last_ts, event.ts)
        if event.run_id:
            s.run_id = event.run_id
        stamp = (event.seq, event.ts)
        k = event.kind
        p = event.payload

        if k == EventKind.RUN_STARTED:
            s.started = Stamped(p, *stamp)
            s.mode = Stamped(p.get("mode"), *stamp)
        elif k == EventKind.RUN_STOPPED:
            s.stopped = Stamped(p, *stamp)
        elif k == EventKind.ENGINE_STATE:
            s.engine_state = Stamped(p.get("state"), *stamp)
        elif k == EventKind.ACCOUNT_SNAPSHOT:
            s.account = Stamped(p, *stamp)
        elif k == EventKind.EQUITY_POINT:
            s.account = Stamped({**(s.account.value or {}), **p}, *stamp)
            # Uniform-stride decimation.
            #
            # Two earlier attempts were wrong in instructive ways. Truncating from the front
            # while keeping the first point drew a straight line from the run's start to the
            # retained window -- a smooth diagonal implying gradual change that never
            # happened. Naive repeated halving fixed that but left the sampling wildly
            # uneven (a 33% gap at the start beside 0.4% gaps at the end), which is its own
            # kind of lie about where the detail is.
            #
            # Keeping every Nth point and doubling N when the buffer fills gives an evenly
            # sampled curve over the whole run at every scale.
            # A pure stride would leave the curve's right edge lagging by up to one stride,
            # which on a live dashboard means the line stops short of the equity the operator
            # can see in the tile above it. One off-grid "tip" point is therefore always kept
            # at the end and replaced on each update, leaving the grid itself uniform.
            s.equity_seen += 1
            point = (event.ts, p.get("equity", 0.0), p.get("balance", 0.0))
            if s.equity_tip and s.equity:
                s.equity.pop()
                s.equity_tip = False
            if s.equity_seen % s.equity_stride == 0:
                s.equity.append(point)
                if len(s.equity) > s.max_equity:
                    # `[1::2]`, not `[::2]`. The retained points sit at multiples of the old
                    # stride, so keeping the ODD positions yields multiples of twice it --
                    # which is exactly the new grid. Keeping the even positions instead
                    # leaves the survivors half a step out of phase with everything appended
                    # afterwards, and that phase error shows up as a single wrong-width gap
                    # in the middle of the chart.
                    s.equity = s.equity[1::2]
                    s.equity_stride *= 2
            else:
                s.equity.append(point)
                s.equity_tip = True
        elif k in (EventKind.VENUE_CONNECTED, EventKind.VENUE_DISCONNECTED,
                   EventKind.VENUE_ERROR):
            s.venue = Stamped({"kind": k, **p}, *stamp)
        elif k == EventKind.HEARTBEAT:
            s.heartbeat = Stamped(p, *stamp)
        elif k in (EventKind.KILL_SWITCH, EventKind.KILL_SWITCH_RESET):
            s.kill_switch = Stamped({"kind": k, **p}, *stamp)
        elif k in (EventKind.RECONCILE_OK, EventKind.RECONCILE_DIVERGENCE,
                   EventKind.RECONCILE_REPAIRED):
            s.reconcile = Stamped({"kind": k, **p}, *stamp)
        elif k == EventKind.POSITION_OPENED:
            s.positions[int(p.get("ticket", 0))] = {**p, "opened_ts": event.ts,
                                                    "symbol": event.stream}
        elif k == EventKind.POSITION_MODIFIED:
            existing = s.positions.get(int(p.get("ticket", 0)))
            if existing is not None:
                existing.update({"stop_loss": p.get("new_stop_loss", existing.get("stop_loss")),
                                 "take_profit": p.get("new_take_profit",
                                                      existing.get("take_profit")),
                                 "last_action": p.get("reason"),
                                 "last_action_detail": p.get("detail")})
        elif k == EventKind.POSITION_CLOSED:
            s.positions.pop(int(p.get("ticket", 0)), None)
        elif k == EventKind.TRADE_RECORDED:
            with contextlib.suppress(Exception):
                s.trades.append(Trade(**p["trade"]))
        elif k == EventKind.DECISION:
            record = p.get("record")
            if record:
                s.decisions.append({**record, "_seq": event.seq})
                if len(s.decisions) > s.max_decisions:
                    del s.decisions[: len(s.decisions) - s.max_decisions]
        elif k == EventKind.DECISION_OUTCOME:
            link = p.get("link") or {}
            if link.get("decision_id"):
                s.decision_outcomes[link["decision_id"]] = link
        elif k in (EventKind.ORDER_SUBMITTED, EventKind.ORDER_ACCEPTED,
                   EventKind.ORDER_REJECTED, EventKind.ORDER_RETRY):
            s.orders.append({"kind": k, "seq": event.seq, "ts": event.ts,
                             "symbol": event.stream, **p})
            if len(s.orders) > 1000:
                del s.orders[: len(s.orders) - 1000]
        elif k == EventKind.ERROR:
            s.errors.append({"seq": event.seq, "ts": event.ts, **p})
            if len(s.errors) > 200:
                del s.errors[: len(s.errors) - 200]

    # -- views --------------------------------------------------------------------

    def metrics(self, starting_equity: float = 10_000.0) -> PerformanceMetrics:
        return compute_metrics(self.state.trades, starting_equity=starting_equity,
                               equity_curve=self.state.equity or None)

    def funnel(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for d in self.state.decisions:
            code = d.get("reason_code", "")
            counts[code] = counts.get(code, 0) + 1
        total = sum(counts.values()) or 1
        return [{"reason": code, "count": n, "share": n / total}
                for code, n in sorted(counts.items(), key=lambda x: -x[1])]

    def staleness_ms(self, now_ms: int) -> int | None:
        """How old the newest journal event is. ``None`` when nothing has been seen at all.

        The distinction matters: "no data yet" and "data from six hours ago" look identical
        on a dashboard that renders both as a blank, and they mean completely different
        things.
        """
        if not self.state.last_ts:
            return None
        return max(0, now_ms - self.state.last_ts)

    def decision(self, decision_id: str) -> dict[str, Any] | None:
        for d in reversed(self.state.decisions):
            if d.get("decision_id") == decision_id:
                return {**d, "outcome_link": self.state.decision_outcomes.get(decision_id)}
        return None

    def recent_decisions(
        self, limit: int = 100, outcome: str | None = None, reason: str | None = None,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        out = []
        for d in reversed(self.state.decisions):
            if outcome and d.get("outcome") != outcome:
                continue
            if reason and d.get("reason_code") != reason:
                continue
            if symbol and d.get("symbol") != symbol:
                continue
            out.append({**d, "outcome_link": self.state.decision_outcomes.get(
                d.get("decision_id", ""))})
            if len(out) >= limit:
                break
        return out


def list_runs(root: Path | str) -> list[dict[str, Any]]:
    """Every run directory under ``root``, newest first."""
    base = Path(root)
    if not base.exists():
        return []
    runs = []
    for path in base.rglob("events.jsonl"):
        d = path.parent
        stat = path.stat()
        runs.append({
            "name": str(d.relative_to(base)),
            "path": str(d),
            "size_bytes": stat.st_size,
            "modified_ms": int(stat.st_mtime * 1000),
        })
    return sorted(runs, key=lambda r: -r["modified_ms"])
