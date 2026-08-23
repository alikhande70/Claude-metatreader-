"""Decision records -- the audit trail that makes the system evaluable (ADR-008).

Every strategy evaluation produces one of these, including the overwhelming majority that
conclude "nothing to do". That is deliberate: knowing *how often* and *why* the system stood
aside is as informative as knowing why it traded, and it is the only way to distinguish
"the edge stopped working" from "the filters stopped letting trades through".
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas.core.enums import DecisionOutcome, Regime, Side


class GateResult(BaseModel):
    """One boolean condition that a setup had to satisfy.

    Records the *numbers*, not just the verdict, so a rejection can be re-examined later
    without re-running the backtest: "vetoed on spread" is much less useful than
    "spread 42.0 pts vs limit 25.0 pts".
    """

    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    value: float | None = None
    threshold: float | None = None
    comparison: str = ""  # e.g. "<=", ">=", "in"
    detail: str = ""
    hard: bool = True  # hard gates veto; soft gates only reduce conviction


class EvidenceItem(BaseModel):
    """One scored factor contributing to conviction.

    ``score`` is bounded [-1, 1] and ``weight`` is non-negative, so the contribution
    ``score * weight`` is directly comparable across factors and the total decomposes
    exactly. No factor may be unbounded -- that is what makes the sum interpretable.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    score: float = Field(ge=-1.0, le=1.0)
    weight: float = Field(ge=0.0)
    detail: str = ""

    @property
    def contribution(self) -> float:
        return self.score * self.weight


class ProposedTrade(BaseModel):
    """The trade a SIGNAL decision is asking for, before risk sizing.

    Prices are strategy-determined; volume is *not* -- the risk engine owns sizing (ADR-010).
    """

    model_config = ConfigDict(frozen=True)

    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float | None = None
    stop_points: float
    reward_risk: float | None = None
    rationale: str = ""


class DecisionRecord(BaseModel):
    """The complete, self-contained account of one strategy evaluation."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    ts: int
    symbol: str
    strategy: str
    strategy_version: str = "1"
    outcome: DecisionOutcome
    reason_code: str = ""
    reason_detail: str = ""

    regime: Regime = Regime.UNKNOWN
    bias: Side | None = None
    conviction: float = 0.0  # normalised [0, 1]

    features: dict[str, float] = Field(default_factory=dict)
    gates: tuple[GateResult, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    proposal: ProposedTrade | None = None

    # Populated by later stages, never by the strategy itself.
    risk_notes: tuple[str, ...] = ()
    sized_volume: float | None = None
    client_order_id: str | None = None

    @property
    def failed_hard_gates(self) -> tuple[GateResult, ...]:
        return tuple(g for g in self.gates if g.hard and not g.passed)

    @property
    def evidence_total(self) -> float:
        return sum(e.contribution for e in self.evidence)

    def explain(self) -> str:
        """One-line human summary. Used in logs, the CLI and the dashboard tooltip."""
        if self.outcome is DecisionOutcome.SIGNAL and self.proposal:
            p = self.proposal
            return (
                f"{self.symbol} {p.side} @ {p.entry_price} SL {p.stop_loss} "
                f"({p.stop_points:.0f}pt) conviction {self.conviction:.2f} regime {self.regime}"
            )
        if self.outcome is DecisionOutcome.VETOED:
            failed = ", ".join(
                f"{g.name}({g.value}{g.comparison}{g.threshold})" for g in self.failed_hard_gates
            )
            return f"{self.symbol} vetoed: {self.reason_code} [{failed}]"
        return f"{self.symbol} {self.outcome}: {self.reason_code or 'no setup'}"


def make_decision_id(symbol: str, strategy: str, ts: int, salt: str = "") -> str:
    """Deterministic decision id.

    Determinism matters: replaying the same data must yield the same ids, otherwise a
    backtest cannot be diffed against a previous run and decision->trade links break on
    re-import. Sixteen hex chars gives ~64 bits, ample for the ~1e6 decisions/year scale.
    """
    raw = f"{symbol}|{strategy}|{ts}|{salt}".encode()
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


def make_client_order_id(decision_id: str, attempt_group: int = 0) -> str:
    """Idempotency key for order submission (ADR-013).

    Kept to 16 chars because MT5 order comments are truncated by many brokers (commonly at
    31 characters) and we must also fit a short prefix.
    """
    raw = f"{decision_id}|{attempt_group}".encode()
    return "A" + hashlib.blake2b(raw, digest_size=7).hexdigest()  # 1 + 14 = 15 chars


class DecisionOutcomeLink(BaseModel):
    """Joins a decision to what actually happened, closing the evaluation loop.

    Written when the resulting trade closes. Without this the decision store can say why a
    trade was taken but not whether that reasoning was any good.
    """

    model_config = ConfigDict(frozen=True)

    decision_id: str
    trade_id: str
    r_multiple: float
    net_profit: float
    mae_points: float
    mfe_points: float
    exit_reason: str
    holding_ms: int


def features_digest(features: dict[str, Any]) -> str:
    """Stable hash of a feature snapshot, for detecting silent feature-pipeline changes.

    If this digest changes for identical input data, something in the feature layer moved
    and previously-recorded decisions are no longer comparable to new ones.
    """
    items = sorted(
        (k, round(float(v), 10)) for k, v in features.items() if isinstance(v, (int, float))
    )
    raw = "|".join(f"{k}={v}" for k, v in items).encode()
    return hashlib.blake2b(raw, digest_size=8).hexdigest()
