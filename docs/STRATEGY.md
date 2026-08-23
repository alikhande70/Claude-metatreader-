# ATLAS Strategy Specification

This document is written **before** the strategy code and is the authority on what the code
must do. Where code and this document disagree, one of them is a bug.

Two strategies ship. `SM1` is the reference system. `DC1` is a deliberately trivial baseline
whose only purpose is to answer the question *"is SM1's complexity earning anything?"* — if
SM1 does not beat DC1 net of costs on out-of-sample data, SM1's extra parameters are fitting
noise and should be deleted.

---

## 1. SM1 — Structure Momentum Pullback

### 1.1 Mechanism (why anyone is on the other side)

An edge needs a story about who loses money to it. SM1's story:

During the liquid sessions, a structural break (a close beyond a confirmed swing) marks the
arrival of genuine directional flow. Two groups then reliably supply the other side of a
pullback entry:

1. **Late momentum buyers** who entered at the breakout extreme and placed stops just beneath
   the swing that broke. Their stops cluster in an obvious place.
2. **Short-term profit takers** who fade the first extension, creating the retrace itself.

The imbalance left behind by the impulsive leg (an FVG) marks where two-sided trade did not
occur, and is where resting interest is most likely to be. Entering on the retrace into the
discount half of the new leg is therefore taking the other side of the group that is forced
to exit, not predicting direction.

This is a mechanism, not a guarantee. It fails — see §1.9.

### 1.2 Timeframe ladder (fixed before testing)

| Role | Default | Purpose |
|---|---|---|
| **HTF** — bias | H4 | direction only; nothing else |
| **MTF** — structure | H1 | the leg being traded, the dealing range, the stop level |
| **LTF** — trigger | M15 | entry timing and precise stop |

Ratios are ~4×. Adjacent timeframes (H1→M30) carry nearly the same information and are not
used. The ladder is configurable but **must be fixed before a test run** — evaluating six
timeframes and trading whichever agrees is a search for confirmation, not analysis.

All three are aggregated from one base stream (ADR-014), and all reads are of **closed** bars
only (ADR-004), so there is no shift-1 to remember.

### 1.3 What is knowable when

| Input | Source | Knowable at |
|---|---|---|
| HTF `break_state`, `swing_trend` | confirmed swings, strength `N` | HTF bar close, swing lag `N` bars |
| MTF dealing range, `position_in_range` | confirmed swings | MTF bar close |
| MTF structural event (BOS/CHoCH) | close vs confirmed swing + margin | MTF bar close, no extra lag |
| Unmitigated FVG | 3-bar pattern | close of the third bar, no lag |
| Liquidity sweep | pool + reclaim | LTF bar close |
| LTF trigger event | close vs confirmed LTF swing | LTF bar close |
| Spread | current quote | now |
| Session / rollover | measured server offset | now |

Nothing in the table is knowable earlier than stated, and the feature layer enforces it
(`tests/unit/test_no_lookahead.py`).

### 1.4 Rules

**Direction (HTF).** Both conditions, or no setup:
```
bias = LONG   if HTF.break_state == BULL and HTF.close > HTF.ema_slow
bias = SHORT  if HTF.break_state == BEAR and HTF.close < HTF.ema_slow
else            NO_SETUP  (reason NO_HTF_BIAS)
```
Two independent confirmations — one fast and structural, one slow and price-based.

**Regime (MTF).** SM1 trades trend regimes only:
```
HIGH_VOL_SHOCK  if MTF.atr_pct > atr_pct_max          -> VETO VOL_SHOCK
TREND           if MTF.adx >= adx_trend_min or MTF.er >= er_trend_min
RANGE           otherwise                              -> NO_SETUP (reason RANGE_REGIME)
```

**Setup (MTF).** The leg must be live and price must be retracing into it:
```
MTF.break_state == bias
AND a BOS/CHoCH in the bias direction occurred within setup_lookback MTF bars
AND location gate (retrace DEPTH, see note):
      LONG :  min(LTF.low  over last arm_bars) mapped into the MTF range <= location_max
      SHORT:  max(LTF.high over last arm_bars) mapped into the MTF range >= 1 - location_max
```

> **Note on the location gate.** An earlier version of this spec measured
> `position_in_range` at the trigger bar. That is a self-contradictory question: a momentum
> trigger fires exactly when price is leaving the retrace, so by then it has already climbed
> out of discount. Measured over 12,000 test bars, location at the trigger bar had a median of
> 0.73 while the deepest point in the preceding 12 bars had a median of 0.19 — the original
> rule asked for a condition that essentially never coincides with its own trigger, and the
> strategy produced zero signals. The gate now measures the depth the retrace actually
> reached inside the arming window. `arm_bars = 12` is frozen.

**Trigger (LTF).** A structural break on the trigger timeframe in the bias direction:
```
LONG :  LTF event in {BOS_UP, CHOCH_UP}   and LTF.body_ratio >= body_min
SHORT:  LTF event in {BOS_DOWN, CHOCH_DOWN} and LTF.body_ratio >= body_min
```
Reusing the same break machinery for the trigger costs no new concepts and one parameter.

**Hard vetoes** (evaluated on every signal; each has its own reason code and is counted):

| Code | Condition |
|---|---|
| `SPREAD_TOO_WIDE` | `spread_points > spread_max_atr × ATR_LTF_points` |
| `OUT_OF_SESSION` | not inside any allowed session |
| `ROLLOVER_WINDOW` | inside ±60 min of broker server midnight |
| `NEWS_WINDOW` | high-impact event for either currency within the blackout window |
| `NEWS_CALENDAR_UNAVAILABLE` | calendar missing **and** policy is `block` |
| `VOL_SHOCK` | `MTF.atr_pct > atr_pct_max` |
| `STOP_TOO_TIGHT` | stop distance below broker `stops_level` + safety |
| `STOP_OUT_OF_BOUNDS` | stop distance outside `[min_stop_atr, max_stop_atr] × ATR_MTF` |
| `NOT_READY` | any required feature frame still in warm-up |

**Stop.** Structural, with a volatility buffer and hard bounds:
```
LONG :  raw_sl = MTF.last_confirmed_swing_low  - buffer
SHORT:  raw_sl = MTF.last_confirmed_swing_high + buffer
buffer = max(2 × spread_price, sl_buffer_atr × ATR_LTF)
stop_distance = |entry - raw_sl|
reject unless  min_stop_atr × ATR_MTF <= stop_distance <= max_stop_atr × ATR_MTF
reject unless  stop_distance >= (broker stops_level + safety) × point
```
Structural stops beat fixed-point stops because gold's daily range varies by ~3× across
regimes; a fixed 300-point stop is a different trade in a quiet week than a violent one.

**Target.** `TP = entry ± rr_target × stop_distance`. Fixed R multiple, not a structural
level, so that the R distribution is interpretable.

### 1.5 Conviction (soft scoring — never gates, only sizes)

Six bounded factors, each in [-1, 1], with fixed coarse weights:

| Factor | Weight | Meaning |
|---|---|---|
| `htf_alignment` | 2.0 | HTF ADX strength and EMA separation |
| `location` | 1.5 | how deep into discount/premium the retrace reached |
| `momentum` | 1.5 | LTF displacement in ATR |
| `imbalance` | 1.0 | entry sits inside an unmitigated aligned FVG |
| `liquidity` | 1.0 | an opposing pool was swept and reclaimed recently |
| `vol_fit` | 1.0 | ATR percentile inside the workable band |

`conviction = clip(Σ score×weight / Σ weight, 0, 1)`.

Weights are **chosen, not optimised** — 0.5 granularity, ordered by how much each is believed
to matter. They are excluded from the walk-forward sweep. Conviction scales position size
between 50% and 100% of the base risk (never above), so a low-conviction signal is a smaller
trade, not a rejected one. This keeps the sample size up and makes the conviction/outcome
relationship measurable — the decision store can answer "do high-conviction trades actually
perform better?" and if the answer is no, the whole scoring layer should be deleted.

### 1.6 Trade management (runs on LTF bar close only — ADR-016)

1. **Breakeven** at `+be_at_r` R: SL → entry ± (2 × spread) in the profitable direction.
2. **ATR trail** after `+trail_after_r` R: SL → `extreme ∓ trail_atr × ATR_LTF`, monotonic
   (a trail never loosens).
3. **Time stop**: if the trade has not reached +1R after `time_stop_bars` LTF bars, close it.
4. Protective SL and TP always live **server-side** at the broker. Management only tightens
   them. If the process dies, the trade is still protected.

### 1.7 Conflict and lifecycle rules

| Situation | Behaviour |
|---|---|
| Signal while a position is open on this symbol | `SUPPRESSED`, reason `POSITION_OPEN`. No pyramiding. |
| Opposite signal while in a position | Ignored (not a reversal). Reversal is a separate variant. |
| Two symbols signal on the same bar | Both proceed; the risk engine's aggregate cap decides. |
| Restart with a position already open | Adopt it by magic + client id. `initial_stop` reconstructed from the live SL and flagged `stop_reconstructed`; R-multiples for that trade are marked approximate. |
| Kill switch active | No new orders. Existing positions keep their server-side stops and are managed normally unless the halt reason is `RECONCILIATION_DIVERGENCE`, in which case management is frozen too. |

### 1.8 Parameters — and which ones may be optimised

The sample-size rule of thumb is ~50 trades per *free* parameter. SM1 has 19 parameters in
total, which would demand ~900 trades. That is not realistic, so most are **frozen** by
structural rationale and excluded from every sweep:

| Frozen (rationale-set, never swept) | Value |
|---|---|
| `swing_strength` | 3 |
| `break_margin_atr` | 0.10 |
| `fvg_min_atr` | 0.30 |
| `liquidity_tol_atr` | 0.15 |
| `sweep_penetration_atr` | 0.10 |
| `min_range_atr` | 2.0 |
| `body_min` | 0.45 |
| `atr_pct_max` | 0.95 |
| `min_stop_atr` / `max_stop_atr` | 0.5 / 4.0 |
| evidence weights | as tabulated above |
| `time_stop_bars` | 24 |
| `arm_bars` | 12 |

| Optimisable (swept in walk-forward) | Default | Grid |
|---|---|---|
| `adx_trend_min` | 22 | 18, 22, 26, 30 |
| `location_max` | 0.55 | 0.40, 0.50, 0.55, 0.65 |
| `rr_target` | 2.0 | 1.5, 2.0, 2.5, 3.0 |
| `sl_buffer_atr` | 0.35 | 0.25, 0.35, 0.50 |
| `be_at_r` | 1.0 | 0.75, 1.0, 1.5 |

Five free parameters ⇒ ~250 trades before a walk-forward result carries weight. That is the
number `atlas validate` checks against, and it will say so when the sample is too small.

### 1.9 What kills this strategy

Every system has a market condition that destroys it. Naming it is part of understanding it.

1. **Sustained range-bound chop with frequent false breaks.** The regime filter reduces this
   but cannot eliminate it; expect the worst drawdowns here.
2. **Trend days with no retrace.** The setup requires a pullback into the range; a one-way
   day produces zero trades, and the strategy watches the best move of the month go by. This
   is a deliberate trade-off, not a defect.
3. **Cost regime change.** A broker widening spreads, or a shift to a higher-volatility regime
   with proportionally worse fills, erodes an edge of this size directly. The cost-sensitivity
   sweep in `atlas validate` quantifies the headroom.
4. **Structural regime change in gold's drivers** — e.g. a shift from real-yield-driven to
   flow-driven behaviour changes the retrace statistics.

### 1.10 Validation pass/fail — written before any test runs

A configuration is `APPROVED` only if **all** hold on out-of-sample data:

| Criterion | Threshold |
|---|---|
| Out-of-sample trades | ≥ 250 |
| OOS expectancy | ≥ +0.05 R |
| OOS profit factor (net of costs) | ≥ 1.15 |
| Walk-Forward Efficiency | ≥ 0.5 |
| Monte Carlo p95 max drawdown | ≤ 25% of starting equity |
| Parameter plateau | best parameters' neighbours within ±25% of its expectancy |
| Cost sensitivity | still positive expectancy at 1.5× modelled costs |
| Data source | **not synthetic** |

Note the realistic scale: an expectancy of +0.1R and a profit factor of 1.2–1.4 is what a
genuinely working retail system looks like. A profit factor of 3 on a backtest is evidence of
a bug, not of an edge.

---

## 2. DC1 — Donchian Breakout (baseline)

Three parameters. Long when the close exceeds the `n`-bar Donchian high (excluding the
current bar), short on the mirror. Stop at `k × ATR`, target at `rr × stop`. Same risk
engine, same costs, same everything else.

Its purpose is entirely diagnostic. Reporting SM1's performance without DC1's beside it
invites the reader to attribute to SM1's cleverness what might just be the market's trend.

---

## 3. Open questions

- **Conviction scoring is unproven.** It is measurable by construction (every decision stores
  its evidence and links to its outcome), and if conviction shows no relationship to realised
  R over a few hundred trades, the layer should be removed rather than re-weighted.
- **The FVG factor may be redundant** with the location factor; their correlation is
  computable from the decision store once trades exist.
