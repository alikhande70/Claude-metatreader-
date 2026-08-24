# ATLAS — Real-environment verification

`docs/STATUS.md` says what is verified. This file says **how the rest gets verified**, and
what counts as proof.

The rule it exists to enforce: *nothing moves into the Verified column of `STATUS.md` without
an artifact in `evidence/`.* Not a report that it worked. The artifact.

Everything below the MetaTrader boundary can only be settled on a Windows machine with a
terminal and a broker. So the gates are split: the ones that can be closed anywhere are
closed here and stay closed by CI; the ones that need a terminal name **one action** and
**one artifact**, so the round trip carries as much as a round trip can.

---

## Gate status

| Gate | What it settles | Where | State |
|---|---|---|---|
| G0 | The Python system is internally correct | anywhere | **CLOSED** — 370 tests, CI |
| G1 | The MQL5 sources contain no compiler-visible error | anywhere | **CLOSED** — `make mql5check`, 8 mutation tests |
| G2 | The sidecar's protocol half is correct | anywhere | **CLOSED** — 20 conformance tests against a fake `MetaTrader5` |
| G3 | `AtlasBridge.mq5` compiles in MetaEditor | **your terminal** | OPEN |
| G4 | The terminal connects to ATLAS and the handshake is accepted | **your terminal** | OPEN |
| G5 | The broker's real specifications are known and the config agrees with them | **your terminal** → checked here | OPEN |
| G6 | The engine sees a live bar stream with the correct clock | **your terminal** | OPEN |
| G7 | One order round-trips on a **demo** account: send, fill, modify, close | **your demo account** | OPEN |
| G8 | Idempotency holds against a real broker | **your demo account** | OPEN |
| G9 | The kill switch halts a live run and stays halted across a restart | **your demo account** | OPEN |
| G10 | Realised costs are compared with the model | **demo, after a week** | OPEN |

G3 through G10 are ordered. Each depends on the one before it; there is no useful way to
skip ahead, and a gate that is skipped will be discovered later at a worse moment.

---

## What "closed" requires

A gate closes when an artifact exists in `evidence/` that a third party could inspect and
reach the same conclusion from. Concretely:

- a log or a screenshot with the **whole** output, not the successful line
- a captured JSON artifact, where the tooling produces one
- for anything involving money, the broker's own record — the ticket, the retcode, the deal

An artifact that only shows success is weaker than one that shows the attempt. When
something fails, that output is *also* evidence and is worth keeping: it is what the fix has
to be checked against.

---

## G3 — MetaEditor compile

**Why this is first:** nothing else about the EA can be known while this is unknown.
`make mql5check` has removed the errors a C++ compiler can see — names, arity, argument
types, enum families — but it cannot model MQL5's object system, its overload resolution, or
`#property`. A PASS there is a necessary condition, never a sufficient one.

**Your action**

1. In MT5: *File → Open Data Folder*. Copy:
   ```
   mql5/Include/Atlas/Json.mqh    ->  MQL5/Include/Atlas/Json.mqh
   mql5/Include/Atlas/Spec.mqh    ->  MQL5/Include/Atlas/Spec.mqh
   mql5/Include/Atlas/Orders.mqh  ->  MQL5/Include/Atlas/Orders.mqh
   mql5/AtlasBridge.mq5           ->  MQL5/Experts/AtlasBridge.mq5
   ```
2. Open `AtlasBridge.mq5` in MetaEditor and press **F7**.
3. In the **Errors** tab, right-click → *Copy All*.

**What to send back:** the entire Errors tab, verbatim — errors *and* warnings, even if it
says `0 errors, 0 warnings`. Paste it as text rather than a screenshot; line numbers matter.

**Pass criterion:** `0 errors`. Warnings are read individually, not ignored.

**Saved as:** `evidence/G3-metaeditor-compile.txt`

> Every error that this produces is also a gap in `tools/mql5check/`. Each one gets fixed in
> the source *and* reflected in the shim, with a mutation test, so the same class cannot
> return.

---

## G4 — Handshake

**Your action**

1. *Tools → Options → Expert Advisors* → add `127.0.0.1` to the allowed addresses. Without
   this, `SocketCreate` fails with error 4014.
2. Enable **Algo Trading**.
3. Start ATLAS first — it listens; the terminal connects out:
   ```
   set ATLAS_BRIDGE_TOKEN=<a secret you choose>
   atlas bridge-check atlas.json
   ```
4. Attach `AtlasBridge` to **one** chart, any symbol. Set `InpToken` to the same secret and
   `InpVerboseLog` to `true` for this gate only.

**What to send back:** the terminal's **Experts** tab, from attach onward, and whatever
`bridge-check` printed.

**Pass criterion:** the Experts log shows `connected to ATLAS` followed by
`handshake accepted by ATLAS`, and `bridge-check` reports `connected: True`.

**Saved as:** `evidence/G4-experts-log.txt`, `evidence/G4-bridge-check.txt`

> Attach it to exactly one chart. ATLAS accepts one terminal and the newcomer displaces the
> incumbent, but two bridges sharing a magic can both act on the same position.

---

## G5 — The broker's real specifications

This is the gate that most often changes the code, because gold is 2 digits at some brokers
and 3 at others, contract size is 100 oz or 10, and the symbol may be `XAUUSD.m`, `GOLD` or
`XAUUSD_i`. Every one of those silently changes position size.

**Your action**

```
atlas bridge-check atlas.json --json evidence/G5-bridge-check.json
```

**What to send back:** `evidence/G5-bridge-check.json`. That single file carries the
specifications, the account, the measured server clock offset, the live spread, and which
symbols the broker did not recognise.

**Then, here, with no terminal:**

```
atlas preflight atlas.json evidence/G5-bridge-check.json
atlas preflight atlas.json evidence/G5-bridge-check.json --adopt-specs
```

`preflight` names the consequence of every disagreement rather than printing a diff, and
`--adopt-specs` writes the broker's values into the config, because a specification that is
retyped is a specification that can be retyped wrong.

**Pass criterion:** `atlas preflight` exits zero.

**Saved as:** `evidence/G5-bridge-check.json`, `evidence/G5-preflight.txt`

---

## G6 — Live bars with the right clock

**Your action:** leave the bridge attached through at least two closed M5 bars during an
open session, with ATLAS running:

```
atlas live atlas.json --paper
```

**What to send back:** the `bar` events from the run's `events.jsonl`:

```
findstr "\"t\":\"bar\"" runs\paper\events.jsonl
```

**Pass criterion, checked here:**
- `open_time` is a multiple of the timeframe in **UTC** — if it is offset by the broker's
  server offset, the conversion is inverted somewhere
- a bar appears only *after* its period ends, never during
- `vol` is a plausible tick count, not `1` — `1` means the volume field collided with the
  protocol version, which is a bug this codebase has already had once

**Saved as:** `evidence/G6-bars.jsonl`

---

## G7 — One order, on a demo account

**Do this on a demo account.** Not a small live account.

**Your action:** with the smallest volume the broker allows, let ATLAS place, modify and
close one position — or drive it from the dashboard's controls.

**What to send back:**
- the run's `events.jsonl`
- the broker's own record: the ticket, the retcodes, the deal, from the terminal's *Trade*
  and *History* tabs

**Pass criterion:**
- the fill price is a real price, and `requested_price` differs from it by a plausible slippage
- the stop and target arrive at the broker and appear in the *Trade* tab
- the retcode is 10009, and if it is not, the retcode text explains why in terms of the spec
- ATLAS's view and the terminal's view agree on volume, stop and ticket

**Saved as:** `evidence/G7-events.jsonl`, `evidence/G7-broker-history.txt`

---

## G8 — Idempotency against a real broker

The one property that cannot be checked by inspection, and the one whose failure costs the
most: a double fill.

**Your action:** while one order is in flight, kill the ATLAS process (Ctrl-C, hard). Restart
it. It will look the order up by `client_order_id` before doing anything else.

**What to send back:** the journal across both runs, and the broker's position list.

**Pass criterion:** exactly one position exists at the broker, and the journal shows
`resolved by lookup` rather than a second submission.

**Saved as:** `evidence/G8-events.jsonl`, `evidence/G8-positions.txt`

> If the broker discards or overwrites order comments, this is where that shows up. The
> comment is the idempotency key. A broker that does not preserve it degrades retry safety
> and needs to be known about before it matters.

---

## G9 — The kill switch, deliberately

**Your action:** set `daily_loss_limit_pct` to something a single small loss will breach.
Let it breach. Then restart the process.

**Pass criterion:** trading stops, the dashboard's Risk page names the reason, and after the
restart it comes back **still halted**. A kill switch that forgets across a restart is not a
kill switch.

**Saved as:** `evidence/G9-halt.jsonl`

---

## G10 — Costs against the model

After at least a week of paper or demo trading:

```
atlas analyse runs/paper
```

**Pass criterion:** there is no *persistent* gap between realised and modelled slippage and
spread. A persistent gap means every backtest built on that model is optimistic, and the
model gets re-fitted before any number from it is believed.

**Saved as:** `evidence/G10-analysis.txt`

---

## After all ten

Ten closed gates prove the **machinery** works against a real broker. They prove nothing
about whether SM1 makes money — that needs real history through `atlas validate`, and
`atlas validate` structurally refuses to approve synthetic data. The two questions are
independent and should not be allowed to borrow credibility from each other.
