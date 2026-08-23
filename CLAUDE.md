# ATLAS — notes for whoever works on this next

Read `docs/ARCHITECTURE.md` for the map and `docs/DECISIONS.md` for why. This file is the
short version: the things that are easy to break without noticing.

## Commands

```bash
pip install -e ".[dev]"
pytest                              # full suite
pytest -m "not slow"                # skip the engine-equivalence run
ruff check src tests sidecar
cd dashboard && npm ci && npm run build
```

## Invariants — breaking any of these is a defect, not a design choice

1. **No mode branching in the decision path.** `strategy/`, `risk/`, `features/`,
   `execution/` and `analytics/` must behave identically in backtest, paper and live.
   `tests/unit/test_no_mode_branching.py` parses the AST and fails the build.

2. **No wall-clock reads.** Every component takes a `Clock`. `time.time()` and
   `datetime.now()` are banned in the decision path — same test enforces it. `perf_counter`
   for measuring elapsed wall time is fine.

3. **Every feature is causal and bounded-memory.** Causal because research computes over a
   full history and indexes into it; bounded because live recomputes over a rolling window.
   `tests/unit/test_no_lookahead.py` recomputes on truncated inputs and demands identical
   values. It has caught real leaks twice.

4. **Timestamps are epoch milliseconds UTC.** Broker server time is converted at the edge and
   never leaks inward. Sessions are defined in their market's timezone so the tz database
   handles DST.

5. **The venue is the authority on positions.** Local state is a belief; reconciliation
   adopts broker truth.

6. **Never retry an order blind.** Look it up by `client_order_id` first. If the venue cannot
   look up, do not retry — an unfilled order is a missed trade, a double fill is a loss, and
   those are not symmetric.

7. **Round volume down, never to nearest.** Rounding up exceeds the risk budget that produced
   the number.

8. **Unknown is not zero.** In the API and the dashboard, a value that was never observed is
   `null` with `known: false`. Rendering it as `0` turns "no data" into a confident claim.

## Traps this codebase has already fallen into

Recorded because they were all subtle, all found by testing, and all easy to reintroduce.

| Trap | What it looked like |
|---|---|
| HTF bars one bar late | The aggregator waited for a bar from the *next* bucket instead of closing when a bar reached the boundary. Correct in research, late in live. |
| Stale HTF at the trigger | Bars emitted base-timeframe-first, so when M5 and M15 closed together M5 was evaluated against last period's M15 — only on the bars where the bias had just changed. |
| Unbounded structure state | The BOS/CHoCH machine remembered forever, so a rolling window computed different state from a whole-history pass. |
| Linear travel time | The time stop assumed price advances one ATR per bar. It random-walks: distance grows with the square root of time. Closed 88% of trades prematurely. |
| Halt downgrade | Limits checked cheapest-first, so one big loss halted with the *auto-clearing* daily reason and would have re-armed overnight with the total limit still breached. |
| Threadpool double-fold | FastAPI runs sync endpoints in a threadpool; parallel dashboard polls folded the same events twice and showed 61 trades from a 41-trade journal. |
| Duplicate protocol key | The bar message used `v` for both protocol version and volume. Every bar's volume parsed as `1`. |
| Decimation phase seam | `[::2]` on a strided buffer leaves survivors half a step out of phase; `[1::2]` is correct. |

## Where the honesty rules live

- Synthetic data can never produce an approval. `BacktestResult.approved_for_live` and
  `ValidationReport.approved` both hard-fail on it.
- Metrics attach their own caveats (small sample, implausible profit factor, cost-constrained).
- `docs/STATUS.md` separates Implemented / Verified / Requires-real-environment. The MQL5 code
  is in the third column and must stay there until someone compiles and runs it.

## Adding things

- **Strategy**: subclass `Strategy`, return a `DecisionRecord` from every `evaluate`, register
  it. Sizing, execution, journalling and the dashboard's explorer all work unchanged.
- **Venue**: implement `ExecutionVenue` and declare `VenueCapabilities` honestly.
- **Feature**: add to `FeatureFrame.compute` and `values()`; the look-ahead test will judge it.
