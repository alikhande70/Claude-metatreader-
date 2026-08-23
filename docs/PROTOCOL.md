# ATLAS ⇄ MetaTrader 5 Bridge Protocol v1

The contract between the ATLAS engine and a MetaTrader 5 terminal. It has **two independent
implementations** (ADR-005), and the Python side has exactly one client that cannot tell them
apart:

| Implementation | Where it runs | When to use it |
|---|---|---|
| `mql5/AtlasBridge.mq5` | inside the terminal, as an EA | primary; real push events, no extra process, works under Wine |
| `sidecar/atlas_mt5_sidecar.py` | a Windows process using the `MetaTrader5` package | when an EA cannot be attached; also the reference used to validate protocol semantics |

Two implementations of one contract means an outage mode in either — the EA detached from the
chart, DLL imports disabled, a `MetaTrader5` package version change — has a documented
fallback, and the contract itself gets tested twice.

---

## 1. Transport

**ATLAS listens; MetaTrader connects out.**

This is not arbitrary. MQL5's native socket API (`SocketCreate`, `SocketConnect`,
`SocketSend`, `SocketRead`, available since terminal build 1930) provides **outbound client
sockets only** — an EA cannot listen or accept. Making the terminal the client is therefore
the only design that needs no external library.

That matters more than it sounds: the common alternative is ZeroMQ, which on the MQL5 side
requires a third-party DLL, `libzmq` on the machine, and "Allow DLL imports" enabled — which
grants an EA unrestricted native code execution. Native sockets need none of that. **ADR-005
is amended accordingly: the transport is a raw TCP socket with native MQL5 sockets, not
ZeroMQ.**

- One TCP connection carries both directions.
- Framing: **newline-delimited JSON**, UTF-8, one object per line. No embedded newlines.
- Maximum frame size: 256 KiB. A larger frame is a protocol error and closes the connection.
- ATLAS binds to `127.0.0.1` by default. Binding to a routable address requires a token
  (§6) and is documented as a deliberate choice in `docs/RUNBOOK.md`.

**Terminal setup.** MQL5 requires outbound socket addresses to be permitted in
*Tools → Options → Expert Advisors → Allow WebRequest for listed URL* on most builds. Add the
host there (e.g. `127.0.0.1`). If `SocketConnect` fails, the Experts log names the error —
`4014`/`5270`-range codes point at this permission rather than at the network.

---

## 2. Envelope

Every message is a JSON object with:

```json
{ "v": 1, "t": "<type>", "ts": 1755950400000 }
```

| field | meaning |
|---|---|
| `v` | protocol version. A client sending an unknown major version is rejected at `hello`. |
| `t` | message type (below). |
| `ts` | sender's epoch milliseconds **UTC**. Not server time — see §5. |
| `id` | correlation id, present on `req`/`reply`/`ping`/`pong`. |

Unknown fields are ignored by both sides. Unknown message types are logged and ignored, never
fatal: a newer terminal-side build must be able to talk to an older engine.

---

## 3. Terminal → ATLAS

### `hello` — first message on every connection

```json
{"v":1,"t":"hello","ts":1755950400000,
 "token":"…","impl":"mql5-ea","build":4150,
 "server_time_ms":1755961200000,
 "account":{"login":123456,"server":"Broker-Live","currency":"USD","balance":10000.0,
            "equity":10000.0,"margin":0.0,"free_margin":10000.0,"margin_level":0.0,
            "leverage":100,"trade_allowed":true,"hedging":true},
 "symbols":["XAUUSD"]}
```

ATLAS replies with `welcome` or closes the connection. `server_time_ms` and the envelope `ts`
together give the server/UTC offset (§5).

### `tick` — top of book

```json
{"v":1,"t":"tick","ts":…,"sym":"XAUUSD","bid":2400.10,"ask":2400.40}
```

Sent on change, rate-limited by the terminal side to at most one per `tick_throttle_ms`
(default 200 ms). ATLAS does not make decisions from ticks (ADR-016), so a throttled feed
costs nothing and a flood is a real risk on gold.

### `bar` — a **closed** bar

```json
{"v":1,"t":"bar","ts":…,"sym":"XAUUSD","tf":"M5","open_time":1755950400000,
 "o":2399.0,"h":2401.2,"l":2398.4,"c":2400.6,"vol":1234,"spread":22}
```

Volume is `vol`, **not** `v`: `v` is the protocol version on every envelope, and reusing it
for volume made a bar's volume parse as the version number. That collision existed in the
first draft of this document and was caught by a linter noticing the duplicate key.

`open_time` is the bar's open, converted to UTC by the sender. Only bars that have closed are
sent — the terminal side detects closure by a change in `iTime(sym, tf, 0)`, never by a wall
clock.

### `txn` — trade transaction

Mirrors `OnTradeTransaction`. Carries `type` (`DEAL_ADD`, `ORDER_ADD`, `POSITION`, …), and
whichever of `deal`, `order`, `position`, `sym`, `volume`, `price`, `retcode`, `comment` apply.
ATLAS treats it as a **hint to re-read state**, never as the state itself: transactions can
arrive out of order and can be missed across a reconnect, so the authoritative answer always
comes from a `positions` request.

### `reply` — answer to a `req`

```json
{"v":1,"t":"reply","id":"r-42","ok":true,"data":{…}}
{"v":1,"t":"reply","id":"r-42","ok":false,
 "error":{"code":"INVALID_STOPS","message":"…","retcode":10016}}
```

### `pong`, `log`

`pong` echoes a `ping` id. `log` carries `{level, msg}` and is journalled by ATLAS so terminal
side problems appear in the same timeline as everything else.

---

## 4. ATLAS → Terminal

### `req` — a command

```json
{"v":1,"t":"req","id":"r-42","op":"order_send","args":{…}}
```

| `op` | args | reply `data` |
|---|---|---|
| `specs` | `{symbols:[…]}` | `{SYMBOL: {digits, point, tick_size, tick_value, contract_size, volume_min, volume_max, volume_step, stops_level, freeze_level, filling_modes:[…], swap_long, swap_short, swap_mode, swap_rollover_3days, margin_initial, currencies…}}` |
| `account` | `{}` | account object as in `hello` |
| `quote` | `{sym}` | `{bid, ask, ts}` |
| `positions` | `{magic?}` | `[{ticket, sym, side, volume, open_price, open_time, sl, tp, price_current, profit, swap, commission, magic, comment}]` |
| `orders` | `{magic?}` | pending orders |
| `bars` | `{sym, tf, count}` | `[[open_time,o,h,l,c,v,spread], …]` — closed bars only, oldest first |
| `order_send` | see below | `{retcode, retcode_text, order, deal, position, volume, price, comment}` |
| `position_modify` | `{ticket, sl?, tp?}` | `{retcode, retcode_text}` |
| `position_close` | `{ticket, volume?, deviation?}` | `{retcode, retcode_text, deal, price, volume}` |
| `order_cancel` | `{ticket}` | `{retcode, retcode_text}` |
| `find_by_comment` | `{comment, magic?}` | position object or `null` — **the idempotency lookup** |
| `history_deals` | `{from_ms, to_ms?, magic?}` | closed deals, for reconciliation and trade reconstruction |

**`order_send` args**

```json
{"sym":"XAUUSD","side":"BUY","type":"MARKET","volume":0.12,
 "price":null,"sl":2395.00,"tp":2410.00,
 "deviation":20,"magic":20260823,"comment":"A3c4f6b2e09834e",
 "filling":"IOC","tif":"GTC","expiry_ms":null}
```

`comment` carries the ATLAS `client_order_id` and is the idempotency key (ADR-013).

### `ping`

`{"v":1,"t":"ping","id":"p-7"}` — the terminal replies `pong` with the same id. Missing two
consecutive pongs marks the venue unhealthy, which halts new entries.

---

## 5. Time

Every `ts` in the protocol is **epoch milliseconds UTC**. The terminal side converts
`TimeCurrent()` (server time) to UTC using `TimeGMT()` before sending, so no server-time value
ever crosses the wire in a field that is not explicitly named `server_time_ms`.

The offset is nonetheless carried in `hello` and refreshed on every `account` reply, because
ATLAS needs it for the rollover window, which follows the **broker's** midnight rather than
UTC's, and because a DST shift changes it twice a year — see `atlas.data.calendar`.

---

## 6. Authentication and safety

- `hello.token` must match ATLAS's configured token, from `ATLAS_BRIDGE_TOKEN` or the config.
  A mismatch closes the connection and is journalled. The port accepts orders; an
  unauthenticated one accepts them from anything that can reach it.
- ATLAS accepts **one** terminal connection at a time. A second `hello` displaces the first
  and is journalled as a divergence — two terminals trading one strategy is a double-size
  accident waiting to happen.
- The terminal side refuses `order_send` if `MQLInfoInteger(MQL_TRADE_ALLOWED)` is false or
  algorithmic trading is disabled, and reports it rather than failing silently.
- `magic` is enforced terminal-side on `positions`, `position_modify` and `position_close`:
  the bridge will not touch a position it does not own, so a manual trade in the same terminal
  is safe from the EA.

---

## 7. Failure semantics

| Situation | Behaviour |
|---|---|
| Request times out | ATLAS retries **only** after `find_by_comment` shows the order did not land (ADR-013). |
| Socket drops | Terminal side reconnects with backoff and sends `hello` again. ATLAS marks the venue unhealthy, halting new entries; existing positions keep their server-side stops. |
| `txn` missed across a reconnect | Recovered by the periodic `positions` + `history_deals` reconciliation, which is authoritative. |
| Terminal restarts with positions open | ATLAS adopts them by `magic` + `comment`, flags `stop_reconstructed`, and marks the R-multiples of those trades approximate. |
| Unknown `op` | `reply` with `ok:false`, `error.code = "UNKNOWN_OP"`. Never fatal. |
| Version mismatch | Rejected at `hello` with a clear message, before any order can be sent. |

---

## 8. Conformance

`tests/conformance/` runs one scenario battery against any implementation of this protocol.
It currently runs against an in-process fake terminal, so the **Python half of the bridge and
the protocol design are verified on Linux CI**. Running the same battery against a real
terminal is the remaining step and is described in `docs/RUNBOOK.md`.

Note the honest boundary: `AtlasBridge.mq5` cannot be compiled or executed in this
environment. It is written against well-established standard-library APIs and is reviewed
against the failure catalogue, but it is **Implemented, not Verified** until it has been
through MetaEditor and a demo account.
