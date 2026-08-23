# ATLAS — Architecture

An end-to-end automated trading system for MetaTrader 5. This document is the map; the
*reasons* live in `docs/DECISIONS.md`, and where the two disagree the ADR log wins.

---

## 1. The shape of the thing

```
                    ┌──────────────────────────────────────────────┐
                    │            MetaTrader 5 terminal             │
                    │   AtlasBridge.mq5  ──or──  Windows sidecar   │
                    └───────────────────┬──────────────────────────┘
                                        │  TCP, newline-JSON
                                        │  (the terminal dials out)
┌───────────────────────────────────────▼──────────────────────────────────────┐
│                                  ATLAS engine                                │
│                                                                              │
│   MarketDataSource ──► FeatureProvider ──► Strategy ──► RiskEngine ──►        │
│         │                    │                │            │      OrderRouter │
│         │                    │                │            │           │      │
│         └────────────────────┴────────────────┴────────────┴───────────┘      │
│                                        │                                     │
│                                   Event Journal  (append-only, the record)   │
│                                        │                                     │
│                                  ProjectionStore                             │
│                                        │                                     │
│                                  FastAPI + WebSocket ──► React dashboard     │
└──────────────────────────────────────────────────────────────────────────────┘
```

Two things about this picture matter more than the boxes:

**The journal is not a log.** It is the system's memory. State is derived from it, crash
recovery replays it, the dashboard projects it, and "why did trade #412 happen" is answered
from it. Everything else is a view.

**The centre never knows what mode it is in.** `Strategy`, `RiskEngine`, `TradeManager` and
`OrderRouter` are the same objects in a backtest, a paper session and a live session. Only
the `MarketDataSource`, the `ExecutionVenue` and the `FeatureProvider` are swapped, and all
three are injected at the edge. A structural test fails the build if a mode branch appears
in the decision path.

---

## 2. Package map

| Package | Responsibility | Depends on |
|---|---|---|
| `atlas.core` | value objects, enums, clocks, errors, instrument maths | nothing |
| `atlas.bus` | event taxonomy, append-only journal | core |
| `atlas.data` | bars, aggregation, sessions/DST, CSV, synthetic generator | core |
| `atlas.features` | indicators, market structure, feature frames and views | core, data |
| `atlas.strategy` | strategy protocol, regime, SM1, DC1 baseline, registry | core, data, features |
| `atlas.risk` | sizing, limits, kill-switch state machine | core |
| `atlas.execution` | venue contract, idempotent order router | core |
| `atlas.venues` | simulator (a real matching engine) and the MT5 bridge client | core, execution |
| `atlas.runtime` | the engine loop, feature providers, position tracking, live wiring | everything |
| `atlas.backtest` | replay runner and the validation battery | runtime, analytics |
| `atlas.analytics` | metrics in R, run analysis, decision analytics | core |
| `atlas.store` | journal projections for the API | bus, analytics |
| `atlas.api` | REST + WebSocket, serves the built dashboard | store |
| `atlas.cli` | `atlas` command | everything |

Dependencies point one way. `core` imports nothing from ATLAS, and nothing below `runtime`
knows the runtime exists.

---

## 3. The temporal model

This is the part most worth understanding, because everything else inherits from it.

A bar with open time `T` and period `P` **becomes knowable at `T + P`**, not at `T`. So:

* The data source stamps that bar's update `ts = T + P`.
* The engine clock is set from the update's timestamp.
* A market order issued from that evaluation fills at the first quote **at or after** `T + P`
  — in replay, the next bar's open, plus spread and slippage.
* Fills are asynchronous everywhere, because in replay the fill price does not exist yet when
  `submit()` returns, and live MT5 delivers outcomes through `OnTradeTransaction` anyway.

Higher timeframes are all aggregated from one base stream, and a bucket closes when a base bar
**reaches its boundary** — not when a bar from the next bucket shows up, which would delay
every HTF bar by one base bar. When several timeframes close at the same instant they are
emitted longest-first, so the trigger timeframe is always evaluated against current
higher-timeframe state.

Every feature has **bounded memory**. That is a hard requirement, not a preference: research
computes features once over a full history and indexes into the result, while live recomputes
over a rolling window. Those two agree only if no feature depends on unbounded history.

---

## 4. The decision path, in order

1. **Bar closes** on a strategy's trigger timeframe.
2. **Features** are positioned at that instant — the last closed bar of every timeframe.
3. **Strategy** evaluates and returns a `DecisionRecord` — always, including no-trade ones.
   It carries the feature snapshot, every gate with its numbers, the evidence contributions,
   and a machine-readable reason code.
4. **Risk engine** sizes and vetoes. It can refuse a signal; it cannot create one. Nothing
   reaches a venue without passing it.
5. **Order router** submits with an idempotency key derived from the decision. A timed-out
   request is resolved by *looking up* whether it landed, never by blind resubmission.
6. **Position tracker** links the resulting position back to the decision, so the trade's
   R multiple, MAE and MFE attach to the reasoning that produced it.
7. **Reconciliation** periodically compares belief with broker truth. The broker wins.

Every step appends to the journal. The chain decision → order → fill → trade → outcome is
navigable in both directions, which is what makes the system evaluable rather than merely
operable.

---

## 5. Where each concern lives

**Look-ahead** is prevented structurally, not by discipline: `BarSeries` holds closed bars
only, features are computed from `data[0:i+1]` and verified by recomputation on truncated
input, and the simulator refuses to fill from a quote older than the order.

**Broker portability** lives in `SymbolSpec`. Digits, point, tick size, tick value, contract
size, lot step, stops level and filling modes are all *read* from the venue. Nothing is
hardcoded, because gold quotes at 2 or 3 digits depending on broker and symbol names carry
suffixes.

**Time** is epoch milliseconds UTC everywhere. Server time is converted at the edge. Sessions
are defined in their market's own timezone so DST is handled by the tz database, and the
rollover window follows the broker's midnight rather than UTC's.

**Costs** are modelled in the simulator: spread, commission per side, adverse-only entry and
stop slippage scaled by spread, swap with the broker's own triple-swap weekday, margin and
stop-out. The simulator's job is to be pessimistic enough that a strategy which survives it
has a chance live.

**Capital protection** is a hard gate with persisted state. A halt survives a restart, daily
limits clear at a configurable boundary in the *firm's* timezone, and drawdown limits are
computed on equity so a floating loss trips them without a trade closing.

---

## 6. What runs where

| Component | Runs on | Verified how |
|---|---|---|
| engine, strategies, risk, simulator, API | any OS with Python 3.11+ | unit + integration tests |
| `AtlasBridge.mq5` | inside the MT5 terminal (Windows, or Wine) | **needs a real terminal** |
| `atlas_mt5_sidecar.py` | Windows, beside the terminal | **needs a real terminal** |
| dashboard | any browser | built, rendered and screenshotted |

See `docs/STATUS.md` for the full Implemented / Verified / Requires-real-environment matrix.

---

## 7. Extending it

**A new strategy**: subclass `Strategy`, return a `DecisionRecord` from `evaluate`, register
it in `atlas.strategy.registry`. Everything else — sizing, execution, journalling, the
dashboard's decision explorer — works with no further changes, because they consume the
record rather than the strategy.

**A new venue**: implement `ExecutionVenue` and declare its `VenueCapabilities` honestly. If
it cannot look up an order by client id, say so, and the router will refuse to retry rather
than risk a double fill.

**A new feature**: add it to `FeatureFrame.compute` and to `values()`. It must be causal and
bounded-memory; `tests/unit/test_no_lookahead.py` will tell you if it is not.
