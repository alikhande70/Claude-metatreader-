# ATLAS

An autonomous trading system for MetaTrader 5 — with the audit trail, risk enforcement and
validation machinery that make an automated strategy something you could actually reason
about, rather than a black box that occasionally places orders.

```
atlas init                    # write a config
atlas doctor atlas.json       # check it for the mistakes that matter
atlas backtest atlas.json     # run, with a baseline to compare against
atlas validate atlas.json     # the full battery, with a pass/fail verdict
atlas serve atlas.json        # the dashboard
atlas bridge-check atlas.json # probe a live MetaTrader 5 terminal
atlas live atlas.json         # trade
```

---

## What it is

Most automated trading projects are a signal generator with an order call bolted on. The
parts that actually decide whether the thing survives contact with a broker — knowing why it
traded, converging back to broker truth after a crash, refusing to size past a limit, and
being able to tell an edge from luck — are usually absent.

ATLAS is built the other way round. The strategy is a replaceable component; the machinery
around it is the product.

**Every evaluation is recorded.** Not just the trades — every time the system looked at the
market and stood aside, with the numbers that made it stand aside. Months later you can ask
why trade #412 happened, what the system saw, which gates it passed, and what it cost.

**One decision path.** The strategy, risk engine and order router are the same objects in a
backtest, a paper session and live. Only the data source and the venue are swapped. A test
fails the build if a mode branch appears anywhere in the decision path — and an integration
test runs the same data through the research and live feature pipelines and requires
byte-identical trades.

**The simulator is pessimistic on purpose.** Bid/ask fills on the *next* quote, commission
both sides, stop-fill slippage that scales with spread, swap with the broker's own
triple-swap day, margin stop-out, and broker stops-level rejections. When a bar contains both
a stop and a target, the stop wins. A simulator that fills at the signal price makes every
strategy look profitable.

**Risk is a hard gate.** Nothing reaches a venue without passing it. Limits are computed on
equity, so a floating loss trips them without a trade closing. The kill switch persists, so a
process that dies while halted comes back halted.

**Validation is a command, not a vibe.** `atlas validate` runs in-sample/out-of-sample,
anchored walk-forward with efficiency, Monte Carlo resampling, bootstrap confidence intervals,
outlier dependence, parameter plateau analysis and cost sensitivity — against thresholds
fixed *before* the run — and writes a report with a verdict.

---

## MetaTrader 5 connection

ATLAS listens; the terminal dials out. MQL5 provides outbound sockets only, and making the
terminal the client is what removes the ZeroMQ DLL and the "Allow DLL imports" permission
that would let an EA run arbitrary native code.

One wire protocol (`docs/PROTOCOL.md`), two terminal-side implementations:

- **`mql5/AtlasBridge.mq5`** — an EA. Real push events, no extra process, works under Wine.
- **`sidecar/atlas_mt5_sidecar.py`** — a Windows process using the `MetaTrader5` package.

The Python side has one client and cannot tell them apart. Two implementations of one
contract means an outage in either has a documented fallback.

---

## Getting started

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

atlas init --out atlas.json
atlas doctor atlas.json
atlas backtest atlas.json --bars 60000 --baseline
atlas serve atlas.json --run-dir runs/backtest/primary   # http://127.0.0.1:8080
```

Building the dashboard bundle (only needed if you change the UI):

```bash
cd dashboard && npm ci && npm run build
```

Connecting a terminal: copy `mql5/Include/Atlas/*.mqh` into `MQL5/Include/Atlas/` and
`mql5/AtlasBridge.mq5` into `MQL5/Experts/`, compile in MetaEditor, permit the ATLAS host
under *Tools → Options → Expert Advisors*, and attach the EA to one chart. Then
`atlas bridge-check atlas.json` — it prints the broker's real symbol specifications, which
are the values your config should hold.

Full operational detail, including the go-live checklist, is in `docs/RUNBOOK.md`.

---

## Read this before trading anything

**Every performance number in this repository comes from a synthetic generator.** It validates
the machinery. It says nothing about whether the strategy makes money, and `atlas validate`
will refuse to approve a configuration whose data source is synthetic, whatever the numbers
say.

`docs/STATUS.md` separates what was *verified here* from what is *implemented but needs a real
terminal*. The MQL5 code is in the second category: it cannot be compiled in this environment,
so treat it as unproven until it has been through MetaEditor and a demo account.

Trading involves risk of loss. This is a system-design and analysis tool, not financial advice,
and the decision to risk capital is yours.

---

## Documentation

| Document | What it covers |
|---|---|
| `docs/ARCHITECTURE.md` | the component map and the temporal model |
| `docs/DECISIONS.md` | 19 ADRs — every significant choice, its alternatives, and how to falsify it |
| `docs/STRATEGY.md` | the full strategy specification, written before the code |
| `docs/PROTOCOL.md` | the MetaTrader 5 bridge wire protocol |
| `docs/RUNBOOK.md` | deployment, the go-live checklist, and what to do when things break |
| `docs/STATUS.md` | Implemented / Verified / Requires-real-environment |
| `docs/VERIFICATION.md` | the ordered gates from here to a verified demo round trip, and what counts as proof for each |
