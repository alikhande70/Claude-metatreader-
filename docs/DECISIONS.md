# ATLAS — Architecture Decision Record (ADR) Log

Append-only. Each entry: context, options considered, decision, consequences, and how to
falsify it later. Superseded entries stay, marked `SUPERSEDED BY ADR-xxx`.

---

## ADR-001 — The problem we are actually solving

**Status:** Accepted

**Context.** The brief is "an autonomous trading system connected to MetaTrader with a web
dashboard". Interpreted naively that becomes an Expert Advisor with two indicators and a
chart page. That artefact is not usable: it cannot tell you *why* it traded, cannot be
validated, cannot recover from a disconnect, and its backtest will not resemble its live
behaviour.

The real problem has four hard parts, in descending order of how often they kill systems:

1. **Trust** — being able to answer, months later, *why* trade #412 was taken, what the
   system saw, and what happened next. Without this, no rational person increases size.
2. **State integrity** — the system's belief about positions/orders/account must converge to
   broker truth after any crash, disconnect, partial fill, or manual intervention.
3. **Validity of the edge** — a number produced by a backtest is worthless unless it survives
   costs, out-of-sample data, walk-forward re-fitting, and trade-order resampling.
4. **Signal generation** — the part everyone starts with, and the smallest source of failure.

ATLAS is therefore architected around auditability and state correctness first, with the
strategy as a replaceable component.

**Consequences.** Substantial engineering goes into the event journal, decision records, and
reconciliation, which do not "make money" directly. Accepted.

---

## ADR-002 — Event-sourced core with derived projections

**Status:** Accepted

**Options.**
- (a) Mutable in-memory state + periodic snapshots to SQLite.
- (b) Append-only event journal, all state derived by folding events; projections cached.
- (c) Full external event store (Kafka/NATS).

**Decision:** (b).

**Why.** Three requirements collapse into one solution. Auditability ("why did this happen")
needs an ordered record of causes. Crash recovery needs a durable log we can replay.
Backtesting needs to push historical events through the same code that runs live. An
append-only journal with deterministic fold gives all three. (c) adds an operational
dependency for a single-node system with no throughput problem — rejected on
maintainability. (a) makes "why" unanswerable — rejected on correctness.

**Implementation.** `atlas.bus.journal.Journal` writes newline-delimited JSON to a segment
file *and* mirrors to SQLite for indexed queries. The JSONL file is the source of truth; the
SQLite tables are a rebuildable projection (`atlas store rebuild`).

**Falsification.** If journal write latency ever appears in the tick-handling hot path
(> 1 ms p99), revisit with a write-behind buffer. Measured, not assumed.

---

## ADR-003 — One decision path across backtest, paper and live

**Status:** Accepted

**Decision.** `Strategy`, `RiskEngine`, `TradeManager` and `OrderRouter` are identical objects
in all three modes. Only two things are swapped: the `MarketDataSource` and the
`ExecutionVenue`. Mode is a property of the *edges* of the system, never of its centre.

**Why.** The dominant reason live results diverge from backtests (after costs) is that the two
run different code. Any `if self.mode == "backtest"` inside strategy or risk logic is a
defect, not a feature.

**Enforcement.** `tests/unit/test_no_mode_branching.py` greps the strategy/risk/execution
packages for mode branching and fails the build if it appears.

---

## ADR-004 — Bar-close determinism and explicit confirmation lag

**Status:** Accepted

**Decision.** Features are computed only from **closed** bars. The forming bar is available to
the trade manager (for stop/target monitoring) but is structurally unavailable to signal
generation. Structural features that need future bars to confirm (swing points) carry an
explicit `confirm_bars` lag and are only reported at the bar where confirmation completed.

**Why.** Look-ahead bias is the single most common source of fake backtest performance. A
swing high identified with `N` bars on each side is not knowable until `N` bars later; code
that treats it as known at the pivot bar is reading the future.

**Enforcement.** `FeatureFrame.at(i)` is computed from a slice `[0:i+1]` only, and a property
test asserts that appending future bars never changes an already-emitted feature value
(`tests/unit/test_no_lookahead.py`).

---

## ADR-005 — MetaTrader 5 connectivity: dual adapter over one wire protocol

**Status:** Accepted

**Context.** MT5 has no official cross-platform API. The options are:

| Option | Streaming | Platform | Order fidelity | Failure surface |
|---|---|---|---|---|
| `MetaTrader5` Python package | poll only | Windows only | full | terminal must be open, same machine |
| MQL5 EA + ZeroMQ | push (ticks, trade txns) | Windows / Wine | full | EA lifecycle, socket lib |
| MQL5 EA + files/named pipes | poll | Windows | full | latency, FS races |
| Broker REST API | push | any | broker-specific | not MT5 |

**Decision.** Define **one versioned wire protocol** (`docs/PROTOCOL.md`, JSON over ZeroMQ
REQ/REP for commands + PUB/SUB for events) and implement **two servers** for it:

- `mql5/AtlasBridge.mq5` — an EA running inside the terminal. Primary transport. Gives
  genuine push events via `OnTick` and `OnTradeTransaction`, works under Wine.
- `sidecar/atlas_mt5_sidecar.py` — a Windows process using the `MetaTrader5` package.
  Fallback when the user cannot or will not attach an EA; also the reference implementation
  used to validate protocol semantics against a real terminal.

The Python side has exactly one client (`atlas.venues.mt5.BridgeVenue`) that neither knows
nor cares which server answers.

**Why.** Betting the system on one transport is the risk. Two implementations of one contract
means an MT5 outage mode (EA removed from chart, DLL imports disabled, Python package
version drift) has a documented fallback, and the contract itself gets tested twice.

**Consequences.** The protocol must be conservative — no feature that only one side can do.
Conformance suite (`tests/conformance/`) runs the same scenario battery against any server
implementation, including an in-process fake, so protocol regressions are caught on Linux CI.

---

## ADR-006 — Simulated venue is a real matching engine, not a stub

**Status:** Accepted

**Decision.** `atlas.venues.sim.SimulatedVenue` models: bid/ask from mid + modelled spread,
commission per lot per side, slippage as a function of order type and volatility, stop-order
gap fills, swap accrual with triple-swap day, margin and stop-out, and weekend gaps.

**Why.** A simulator that fills at the signal price makes every strategy look profitable. The
purpose of the simulator is to be *pessimistic and honest*, so that a strategy which survives
it has a chance live. Cost modelling is not a refinement to add later — it changes which
strategies pass at all.

**Falsification.** Once live fills exist, `atlas analyse slippage` compares realised fill
deviation against the model and reports the error distribution; the model is re-fitted from
that, not guessed.

---

## ADR-007 — Trading intelligence: transparent evidence scoring, not a black box

**Status:** Accepted

**Options.**
- (a) Classical indicator crossover rules.
- (b) Supervised ML (gradient boosting / LSTM) on engineered features.
- (c) Structured multi-factor evidence model with explicit gates and weights.
- (d) LLM-in-the-loop discretionary reasoning.

**Decision:** (c), with (a) shipped as an intentionally simple **baseline** for comparison.

**Why not (b).** Financial time series give a low signal-to-noise ratio and a non-stationary
target. With the trade counts realistically available (hundreds, not millions), a flexible
learner overfits and, worse, cannot be interrogated: "why was this trade rejected" has no
answer. ML is not excluded forever — the feature pipeline and labelled outcome store are
built so a model can be trained later on real decision records — but shipping an unexplainable
model as v1 contradicts ADR-001.

**Why not (d).** Non-deterministic, unbacktestable, latency-bound, and cost-bound. An LLM has
a legitimate role in *research* over the decision records (see `atlas analyse`), not in the
per-bar hot path.

**Why (c).** Each factor emits a bounded score and a human-readable explanation. Total
conviction is a weighted sum, so any decision decomposes into contributions. Hard vetoes are
separate from soft scoring, so "rejected because spread was 4.2× median" is a distinct,
countable outcome. It is fully deterministic, so it backtests exactly.

**Consequences.** Weights are parameters and therefore overfittable. Mitigation: weights are
coarse (0.5 granularity), the parameter count is deliberately small, and walk-forward
plateau analysis — not peak-picking — is the acceptance criterion (ADR-009).

---

## ADR-008 — Every strategy evaluation produces a DecisionRecord

**Status:** Accepted

**Decision.** The strategy returns a `DecisionRecord` on **every** evaluation, including
no-trade ones. It contains: feature snapshot (named values), each gate with its inputs,
threshold, and pass/fail, the evidence contributions, the outcome (`SIGNAL` / `NO_SETUP` /
`VETOED`) with a machine-readable reason code, and a stable `decision_id`. When a signal
becomes an order and then a closed trade, the outcome (R multiple, MAE, MFE, exit reason) is
linked back to the originating `decision_id`.

**Why.** This is the artefact that makes the system evaluable. It converts "the bot lost
money" into "vetoes on `SPREAD_TOO_WIDE` rose 4× in August and our fill quality degraded",
which is actionable.

**Consequences.** Volume: one record per symbol per bar. At M5 across 3 symbols that is
~250k records/year — trivial for SQLite. Records are written to their own table with the
feature snapshot stored as JSON.

---

## ADR-009 — Validation protocol is part of the product

**Status:** Accepted

**Decision.** `atlas validate` runs a fixed battery and produces a report: in-sample /
out-of-sample split, anchored walk-forward with Walk-Forward Efficiency, Monte Carlo
resampling of the trade sequence for the drawdown distribution, parameter-neighbourhood
plateau check, and cost-sensitivity sweep. A strategy configuration is not "approved" for
live use until it passes documented thresholds recorded in the report.

**Why.** Optimisation without out-of-sample validation reliably produces curve-fitted
parameters. Making the validation a first-class CLI command with a persisted report means the
question "is this configuration allowed to trade real money" has a file-backed answer.

---

## ADR-010 — Risk engine is a hard gate, independent of strategy

**Status:** Accepted

**Decision.** No order reaches a venue without passing `RiskEngine.evaluate()`. The risk
engine owns: position sizing from broker spec, per-trade risk cap, aggregate open-risk cap,
correlation-adjusted exposure, daily-loss and total-drawdown kill switches (equity-based,
prop-firm compatible), margin headroom, and instrument-level enable/disable. It can veto a
strategy signal but never create one.

**Why.** Separation means a bug in a strategy cannot bypass capital protection, and risk
policy can be changed and audited without touching signal logic. The kill switch is a state
machine with persisted state, so a restart cannot silently re-arm a halted system.

---

## ADR-011 — Dashboard reads projections, never guesses

**Status:** Accepted

**Decision.** The API serves only values derived from the journal and store. Anything not
known is rendered as "unknown", never as a plausible default. Every live panel carries the
timestamp and sequence number of the data it is showing, and goes stale-flagged if the
heartbeat is older than its threshold.

**Why.** A dashboard that interpolates or optimistically renders is worse than no dashboard:
it produces confident wrong beliefs during exactly the incidents where you need truth.

---

## ADR-012 — Python 3.11 core, MQL5 terminal bridge, React/TS dashboard

**Status:** Accepted

**Decision.** Core in Python 3.11 (asyncio); terminal-side in MQL5; dashboard in
React + TypeScript + Vite, charts via `lightweight-charts`.

**Why Python.** The decision loop runs at bar close (seconds), not microseconds — latency
budget is ample. In exchange we get numpy/pandas for the research and validation half of the
system, which is the half that determines whether the strategy is real. A C++/Rust core would
buy latency we do not need and cost us the analysis toolchain.

**Why not everything in MQL5.** MQL5 cannot be unit-tested in a meaningful way, has no package
ecosystem, and the Strategy Tester is not a validation framework. Keeping MQL5 to a thin,
dumb, well-tested transport layer minimises the untestable surface.

**Consequences.** Python is not suitable if the strategy evolves toward sub-100 ms execution.
That is a documented boundary, not an oversight.

---

## ADR-013 — Idempotent order submission

**Status:** Accepted

**Decision.** Every order carries a `client_order_id` derived deterministically from
`(decision_id, attempt_group)`. It is written into the MT5 order `comment` field and, together
with the EA `magic`, is used to look up whether a submission already landed before retrying a
timed-out request.

**Why.** The classic double-fill: request times out, retry is sent, both execute. In MT5 there
is no server-side idempotency key, so it must be emulated by look-up-before-retry. Never
retry blind.

**Limitation.** MT5 truncates `comment` (broker-dependent, commonly 31 chars) and some brokers
overwrite it. The client id is therefore short (16 chars) and the reconciler treats
`magic + symbol + volume + open time window` as a fallback identity. Documented as a residual
risk in `docs/RUNBOOK.md`.
