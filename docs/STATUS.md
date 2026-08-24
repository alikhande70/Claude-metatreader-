# ATLAS — Status

Three columns, kept honestly separate:

- **Implemented** — the code exists and is complete for its stated purpose.
- **Verified** — it was executed here, against real assertions, and passed. A test that only
  proves a mock agrees with itself does not count.
- **Requires real-environment verification** — it cannot be executed in this environment and
  is therefore a *hypothesis about behaviour* until it runs against a real MetaTrader 5
  terminal and a broker.

Nothing in the third column should be treated as working. That is the point of the column.
`docs/VERIFICATION.md` is the ordered plan for emptying it, gate by gate, and says what
counts as proof for each.

---

## Verified here

| Area | What was actually run |
|---|---|
| Instrument maths | Hypothesis property tests: realised risk never exceeds the budget, volume rounds down, prices land on the tick grid, below-minimum returns zero rather than the minimum |
| Indicators | RSI matches Wilder's **published reference series** to 2 dp; ATR/ADX/EMA/RMA checked against hand-computed values; edge cases (flat, monotonic) return finite values |
| No look-ahead | Every indicator, structure state, FVG and liquidity pool recomputed on truncated inputs and required to be identical. **Caught two real leaks** in this codebase |
| Market structure | BOS/CHoCH sequencing, break margins, confirmation lag, dealing-range invariant, FVG mitigation lifecycle, sweep detection — on hand-built series with known answers |
| Sessions and DST | London/NY session boundaries across DST transitions; rollover following the broker's midnight; a full year of W1 anchoring |
| Risk engine | Floating-loss daily breach with no closed trade; trailing vs static drawdown; halt persistence across restart; corrupt state file fails safe; **check ordering so a large loss halts with the non-clearing reason** |
| Execution economics | Fill on the next quote (not the signal bar's close); stop-before-target within a bar; slipped stops losing more than 1R; commission both sides; triple-swap day; margin stop-out; broker stops-level and lot-step rejections |
| Order idempotency | A venue that lands the order then loses the response produces **exactly one position**, resolved by lookup — over an in-process venue and over a real TCP socket |
| Engine equivalence | The research feature path and the live rolling path produce **byte-identical trades** on the same data. **Caught four real defects** |
| Bridge protocol | 33 conformance tests: `BridgeVenue` over a real socket against a fake terminal backed by the real matching engine — handshake, auth, version rejection, specs, orders, retcodes, streaming, timeouts, disconnects |
| Backtest pipeline | End-to-end runs on 90,000 bars through the CLI; determinism; costs reduce profit monotonically; kill switch halts mid-run; every trade links back to its decision |
| Position tracking | Open risk follows the **live** stop, so a position at breakeven stops consuming its share of the aggregate cap; an unprotected position falls back to full risk rather than zero; R stays measured against the entry stop |
| Projections & API | Concurrency: 48 simultaneous refreshes produce no duplicates. Unknown ≠ zero. Controls refuse when not attached; a drawdown halt cannot be cleared from the dashboard. Static route provably contained |
| Sidecar protocol half | 20 conformance tests run the **real** `sidecar/atlas_mt5_sidecar.py` — its own dispatch, framing and payload builders — against a fake `MetaTrader5` package backed by the matching engine, over a real socket. Handshake, auth refusal, specs round trip, server-offset inference, closed-bar-only history, order send, stops-level and volume refusals, idempotency lookup, magic isolation, pending round trip, deal pairing, tick and bar streaming |
| MQL5 sources | Type-checked under a C++ shim of the MQL5 API (`make mql5check`): names, arity, argument types and property-enum families. **This is not a compile** — see the row below and `tools/mql5check/README.md`. Eight mutation tests prove the checker can fail |
| Preflight | The capture→check loop: a `bridge-check` artifact taken over a real socket survives a JSON round trip and is then evaluated against a config with no venue present. Every sizing field disagreement fails; a minimum lot that cannot fit the risk budget fails; `--adopt-specs` clears it. 25 tests |
| Dashboard | Built, served, rendered in light and dark, screenshotted across all five pages, zero console errors |

**370 tests.** `ruff` clean, `mypy` clean across all 68 modules, TypeScript strict.

---

## Implemented, NOT verified — needs a real MetaTrader 5 terminal

| Component | Why it cannot be verified here | What to do about it |
|---|---|---|
| `mql5/AtlasBridge.mq5` | **MQL5 cannot be compiled or executed in this environment.** There is no MetaEditor and no terminal. `make mql5check` removes the typo-class errors but cannot model MQL5's object system, overload rules or `#property` semantics | Compile in MetaEditor; fix any compiler errors; run against a **demo** account first |
| `mql5/Include/Atlas/*.mqh` | same | same |
| `sidecar/atlas_mt5_sidecar.py` — its MT5 calls | Its protocol half is now verified (above). What remains unverified is whether the real `MetaTrader5` package behaves as `tests/conformance/fake_mt5.py` says it does: attribute names and call shapes there are transcribed from documentation, and a transcription can be wrong | Run on Windows beside a terminal; `atlas bridge-check` proves the round trip |
| Real fills, slippage, requotes | no broker | Paper-trade, then compare realised slippage against the model with `atlas analyse` |
| Broker symbol specs | no broker | `atlas bridge-check --json` captures them; `atlas preflight … --adopt-specs` writes them into the config, so nothing is retyped |
| MQL5 socket permission | terminal setting | Add the ATLAS host under Tools → Options → Expert Advisors |
| Server clock offset / DST | no broker clock | Measured at connect and re-measured; verify in `atlas bridge-check` output |

The protocol these implement **is** verified, on both sides, over a real socket. What is
unverified is specifically the MQL5 code that speaks it.

---

## Implemented but deliberately unproven

| Thing | Status |
|---|---|
| **SM1's edge** | Unknown, and nothing here suggests otherwise. All numbers in this repository come from a synthetic generator, which validates the machinery and says nothing about the market. `atlas validate` refuses to approve a configuration whose data source is synthetic |
| **Conviction scoring** | A hypothesis with a built-in test. The decision store links every conviction score to its realised R; if there is no gradient after a few hundred trades, delete the layer rather than re-weight it |
| **Cost model parameters** | Assumptions, labelled as such. Re-fit from realised fills once live trades exist |
| **Walk-forward on real data** | The battery is implemented and tested; it has never been run on real history because there is none here |

---

## Known limitations

1. **SM1 is selective** — roughly 0.3 signals per trading day on the tested ladder. Reaching
   the 250-trade out-of-sample floor needs years of history or a portfolio of symbols. This
   is a real constraint on validating it, and `atlas validate` will say so rather than
   approving a small sample.
2. **The sidecar polls.** The `MetaTrader5` package has no push API. The EA is preferred.
3. **Python is not a low-latency core.** The decision loop runs at bar close, so this is
   ample — but a strategy needing sub-100 ms execution would need a different core. Stated in
   ADR-012 as a boundary, not an oversight.
4. **MT5 order comments are truncated** by many brokers and overwritten by some. The
   idempotency key is 15 characters for this reason, and reconciliation has a fallback
   identity, but a broker that discards comments entirely degrades retry safety. Check yours.
5. **Netting accounts are untested.** The design assumes hedging semantics (one position per
   ticket). A netting account merges positions and would need explicit handling.
6. **Single-process.** No horizontal scaling, no leader election. One engine, one terminal.
7. **The journal does not rotate.** It grows at roughly one record per symbol per bar, so a
   year of live M5 trading on three symbols is on the order of a million events and a few
   hundred megabytes. That is well within SQLite's comfort zone and easily archived by hand,
   but segment rotation is not implemented and will be wanted eventually.
8. **No authentication on the dashboard.** Bind it to localhost, or put a reverse proxy with
   real auth in front of it. Documented in the runbook rather than half-solved here.

---

## The honest summary

What exists is a complete, tested trading *system*: the data handling, the feature pipeline,
the audit trail, the risk enforcement, the execution machinery, the validation battery and
the operations dashboard are all real and all exercised.

What does not exist is evidence that the shipped strategy makes money. That evidence can only
come from real data and a real broker, and the system is built to produce it honestly rather
than to flatter itself while producing it.
