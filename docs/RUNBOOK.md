# ATLAS — Runbook

Operating the system: getting it connected, taking it live, and what to do when it breaks.

---

## 1. Connecting MetaTrader 5

### 1.1 Install the bridge

Copy into your terminal's data folder (*File → Open Data Folder* in MT5):

```
mql5/Include/Atlas/Json.mqh     ->  MQL5/Include/Atlas/Json.mqh
mql5/Include/Atlas/Spec.mqh     ->  MQL5/Include/Atlas/Spec.mqh
mql5/Include/Atlas/Orders.mqh   ->  MQL5/Include/Atlas/Orders.mqh
mql5/AtlasBridge.mq5            ->  MQL5/Experts/AtlasBridge.mq5
```

Compile `AtlasBridge.mq5` in MetaEditor (F7). **This code has never been through a compiler**
— it was written without access to one (see `docs/STATUS.md`). If MetaEditor reports errors,
they are real and need fixing before anything else.

### 1.2 Permit the socket

*Tools → Options → Expert Advisors* → add the ATLAS host (e.g. `127.0.0.1`) to the allowed
addresses. Without this, `SocketConnect` fails and the Experts log says so.

### 1.3 Enable trading and attach

Enable **Algo Trading** in the toolbar. Attach `AtlasBridge` to **one** chart — any symbol;
the EA streams whatever `InpSymbols` lists, not the chart's symbol. Set `InpToken` to match
`ATLAS_BRIDGE_TOKEN`.

Attaching it to two charts is a real hazard: ATLAS accepts one terminal connection and the
newcomer displaces the incumbent, but two bridges with the same magic can both act on the
same positions. One chart.

### 1.4 Prove the round trip

```bash
export ATLAS_BRIDGE_TOKEN='...'
atlas bridge-check atlas.json
```

This prints connectivity, the measured server clock offset, the account, and **the broker's
real symbol specification**. Copy those values into your config — do not trust the example
ones. Gold is 2 digits on some brokers and 3 on others, and the symbol may be `XAUUSD.m`,
`GOLD` or `XAUUSD_i`.

If a symbol is missing, the suffix is almost always the reason.

---

## 2. Going live — the checklist

Work down it. Each item exists because skipping it has a specific consequence.

- [ ] **`atlas doctor atlas.json` is clean.** It checks the things that silently change every
      position size.
- [ ] **Symbol specs came from `bridge-check`**, not from the example config.
- [ ] **The strategy was validated on real history**, not synthetic data. `atlas validate`
      wrote a report and its verdict is APPROVED. If the sample was too small it will say so
      — that is a finding, not an obstacle to route around.
- [ ] **Risk limits are set inside any external limit.** Prop firm daily limit 5%? Halt at 4%.
      The last percent is slippage, spread, swap and the trade already open.
- [ ] **`consecutive_losses_to_daily_breach` is at least 3.** `doctor` prints it. At 1% risk
      against a 5% daily limit, five losers ends the day, and a run of five is normal for a
      40% win rate.
- [ ] **The daily reset boundary is the firm's timezone**, reconciled with the broker's server
      time. They are rarely the same.
- [ ] **A news calendar is loaded, or `news_policy` is `block`.** Live with neither, the
      system trades through NFP.
- [ ] **Paper-traded for at least a week.** This does **not** validate the edge — the sample
      is far too small — but it reliably reveals that live spreads are double what the model
      assumed, which is the thing that actually kills the edge.
- [ ] **Realised slippage compared with the model**: `atlas analyse runs/paper`. A persistent
      gap means every backtest built on that model is optimistic.
- [ ] **The kill switch was tested deliberately.** Set a tiny daily limit, let it trip, confirm
      trading stops, restart the process, confirm it comes back halted.
- [ ] **VPS or always-on machine**, with the terminal set to reconnect.

Then start with the smallest size the broker allows, not the size the maths says.

---

## 3. Running

```bash
atlas live atlas.json                 # confirms before placing real orders
atlas live atlas.json --paper         # same engine, simulator venue
atlas serve atlas.json --run-dir runs/live
```

The dashboard attaches to the run directory. Health, positions, the decision funnel, risk
usage and the raw event journal are all there, and every panel shows how old its data is.

---

## 4. When things break

### The engine halted

Look at the Risk page. The halt reason says which limit and by how much.

| Reason | Meaning | Clearing it |
|---|---|---|
| `DAILY_LOSS_LIMIT` | today's equity loss hit the threshold | clears itself at the next daily reset |
| `CONSECUTIVE_LOSSES` | a losing run reached the limit | clears at the next daily reset |
| `TOTAL_DRAWDOWN_LIMIT` | drawdown from the high-water mark (or initial balance) | **operator only** — understand why before clearing |
| `RECONCILIATION_DIVERGENCE` | our state and the broker's disagree | **operator only** — reconcile manually first |
| `VENUE_UNAVAILABLE` / `STALE_MARKET_DATA` | health, not risk | clears when the condition does |
| `MANUAL` | someone halted it | operator |

Open positions keep their **broker-side** stops through a halt and continue to be managed —
except under `RECONCILIATION_DIVERGENCE`, where management is frozen because acting on state
we do not trust is worse than doing nothing.

### Reconciliation divergence

The engine adopts broker state as authoritative and journals what differed. Read
`reconcile.divergence` in the event log. Common causes: a manual close in the terminal, a
broker-side stop-out, a partial fill, or a second bridge running.

If a position exists at the broker that ATLAS has no record of, it is adopted with
`stop_reconstructed: true` and its R multiples are marked approximate — the entry stop is not
recoverable from a stop that may already have been trailed.

### Orders rejected

The retcode is in the journal, verbatim, with its meaning.

| Retcode | Usual cause |
|---|---|
| 10014 `INVALID_VOLUME` | lot step or min/max — check the broker's spec against the config |
| 10016 `INVALID_STOPS` | inside the stops level, or on the wrong side |
| 10019 `NO_MONEY` | free margin; check the margin headroom limit |
| 10030 `INVALID_FILL` | the filling mode is not supported for this symbol |
| 10027 `CLIENT_DISABLES_AT` | Algo Trading is off in the terminal |
| 10018 `MARKET_CLOSED` | session or holiday |

### The terminal disconnected

The EA reconnects with backoff and re-sends `hello`. ATLAS marks the venue unhealthy, which
halts new entries; existing positions keep their server-side stops. When the connection
returns, reconciliation re-establishes truth.

### Live results differ from the backtest

Diagnose in this order — it is ranked by how often each turns out to be the cause:

1. **Spread.** Compare realised spread at your fill times with what the model assumed.
2. **Commission.** Present in the backtest?
3. **Stop-fill slippage.** `atlas analyse` prints the realised distribution.
4. **Symbol contract.** Tested `XAUUSD`, trading `XAUUSD.m` with a different spec?
5. **Parameters fitted to the tested period.** What did walk-forward say?
6. **Rejects and requotes** silently skipping trades live.

`atlas analyse runs/live` gives you the first three directly.

---

## 5. Routine maintenance

| When | What |
|---|---|
| daily | check the halt state and the decision funnel — a sudden shift in *why* it stands aside is the earliest signal something changed |
| weekly | compare realised slippage with the model; re-fit if it has drifted |
| monthly | re-run `atlas validate` on the accumulated real trades |
| after a broker change | `atlas bridge-check` and update the specs; re-run the backtest |
| after a DST transition | confirm the measured server offset moved and sessions still line up |

Back up the run directory. `events.jsonl` is the source of truth; the SQLite file is a
rebuildable projection (`Journal.rebuild_sqlite()`).

---

## 6. Security

- The bridge port accepts orders. Bind it to `127.0.0.1` unless you have a specific reason
  not to, and always set `ATLAS_BRIDGE_TOKEN`.
- The token is read from the environment so it never has to sit in a config file, and the API
  never serves it to a browser.
- The dashboard's control endpoints work only when it is attached to a running engine, and a
  drawdown or reconciliation halt cannot be cleared from a browser at all.
- Exposing the dashboard beyond localhost means putting real authentication in front of it.
  There is none built in.
