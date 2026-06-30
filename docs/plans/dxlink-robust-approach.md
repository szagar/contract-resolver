# DXLink — the robust streaming approach (`shared/dxlink/`)

Status: design, grounded in live testing
Last updated: 2026-06-30

`BrokerSource` (and MDS) get greeks/quotes from TastyTrade's DXLink (dxfeed)
WebSocket feed. The current MDS client works but has known robustness gaps (the
SPY/VIX 44-minute silent-stall incident). This doc defines the robust approach
for the shared `shared/dxlink/` client both consumers will use, and is grounded
in (a) TastyTrade / dxfeed documentation, (b) the existing MDS implementation,
and (c) **live protocol testing against the dxfeed demo server** (see Appendix).

It serves two usage profiles with different needs:

- **BrokerSource** — *transient, bounded*: connect → subscribe a small candidate
  set → collect greeks until covered or timeout → tear down.
- **MDS** — *persistent*: a long-lived streamer over thousands of subscriptions
  that must self-heal silent stalls.

---

## 1. The protocol (empirically verified)

### Endpoint + auth

- **Production:** `GET /api-quote-tokens` → `{ token, dxlink-url, level, expires-at }`.
  The token has a finite life — **fetch fresh per connect** (BrokerSource) or
  **refresh before expiry** (MDS). `dxlink-url` is the `wss://` to dial.
- **Demo (no credentials, for offline tests):**
  `wss://demo.dxfeed.com/market-data/dxlink-ws` — note the `/market-data/` path;
  the older `/dxlink-ws` now returns **HTTP 400**. The demo authorizes
  anonymously (no token) and serves a small delayed symbol set (AAPL, EUR/USD…)
  with **no option greeks**.

### Handshake (drive it reactively — see Finding F2)

```
client                              server
  SETUP {channel:0, version, keepaliveTimeout:60, acceptKeepaliveTimeout:60}
                              <--  SETUP {version:"1.0-2.6.0", keepaliveTimeout:60}
                              <--  AUTH_STATE {state: UNAUTHORIZED}      (prod; demo → AUTHORIZED)
  AUTH {channel:0, token}
                              <--  AUTH_STATE {state: AUTHORIZED}
  CHANNEL_REQUEST {channel:1, service:"FEED", parameters:{contract:"AUTO"}}
                              <--  CHANNEL_OPENED {channel:1, parameters:{subFormat:"LIST"}}
  FEED_SETUP {channel:1, acceptDataFormat:"COMPACT", acceptEventFields:{...}}
                              <--  FEED_CONFIG {channel:1, dataFormat:"COMPACT", eventFields:{...}}
  FEED_SUBSCRIPTION {channel:1, add:[{type:"Greeks", symbol:".../...:..."}, ...]}
                              <--  FEED_CONFIG {... eventFields:{Greeks:[...]}}   (per type, as it configures)
                              <--  FEED_DATA  ["Greeks", [ ...row... ], ...]
```

### Keepalive

`SETUP` response declares `keepaliveTimeout: 60`. The client **must** send
`KEEPALIVE {channel:0}` at least every 60s or the server drops the socket. Use a
**30s** interval (margin). Keep `ping_interval=None` on the WS — DXLink has its
own app-level keepalive; WS-level pings cause spurious `1006` closes under a
large resubscribe burst (already learned in MDS).

### COMPACT data format

`FEED_DATA.data` is a flat list: `[eventType, row, eventType, row, …]` where each
`row` is the field **values in the order declared by `FEED_CONFIG.eventFields`
for that type**. Verified Quote row (fields `eventType, eventSymbol, bidPrice,
askPrice, bidSize, askSize`):

```
["Quote", "AAPL", 285.88, 285.91, 400.0, 1000.0]
```

---

## 2. Robustness findings (what "not robust" means, and the fixes)

### F1 — Parse by `FEED_CONFIG.eventFields`, never a hardcoded field map  ⚑ correctness

The server echoes the negotiated field order in `FEED_CONFIG.eventFields` **per
event type**, as it configures that type (verified: a separate `FEED_CONFIG`
arrived for `Quote` and for `Trade`). It honors the requested order in testing,
but it is free to subset/reorder, and the order is **authoritative**. The MDS
parser uses a **fixed** field map and ignores `FEED_CONFIG` after the handshake —
so a server-side reorder would silently misparse (e.g. read `delta` from the
wrong index). **Fix:** capture `eventFields` from every `FEED_CONFIG` and parse
each COMPACT row positionally against the captured order for that type. This is
the single most important correctness fix for greeks (BrokerSource selects on
`delta` by position).

### F2 — Reactive handshake (a state machine over inbound frames)  ⚑ robustness

The MDS handshake is a fixed sequence of blocking `recv(); assert type == X`
calls. Any interleaved frame — a `KEEPALIVE`, an extra `FEED_CONFIG` — throws
`Expected X, got Y`. Testing showed the server bursts `SETUP`+`AUTH_STATE`
together and emits `FEED_CONFIG` repeatedly. **Fix:** run **one** receive loop
from connect; drive the handshake as a state machine that reacts to frame
*types* and tolerates interleaved `KEEPALIVE`/`FEED_CONFIG`/`FEED_DATA`. Signal
"ready" via an `asyncio.Event` set when `AUTH_STATE=AUTHORIZED` +
`CHANNEL_OPENED` + first `FEED_CONFIG` have all been seen.

### F3 — Silent per-subscription staleness watchdog  ⚑ the headline gap

The SPY/VIX incident: socket healthy, keepalives flowing, **zero `FEED_DATA` for
some symbols** → frozen canonical keys, undetected, unrecovered. MDS only *logs*
aggregate `since_last_feed_s` (`dxlink.heartbeat`); it never acts. **Fix:** a
watchdog that escalates:

1. Track `last_feed_at` **per streamer symbol** (not just globally).
2. If a subscribed symbol has no `FEED_DATA` within `stale_threshold`
   (quotes 60s / greeks 120s, matching the subscription models), **force-resend**
   its `FEED_SUBSCRIPTION` (a no-op for healthy symbols; re-arms a silently
   dropped one).
3. If still stale after a grace window, **force a full reconnect** (new token →
   handshake → resubscribe-all). Reconnect already exists in MDS; the watchdog is
   what *triggers* it on a silent stall rather than only on a hard socket close.

For **BrokerSource** the same principle appears as the bounded-collection timeout
(§3): if a target symbol's greeks never arrive within the budget, that's a
per-symbol stall — resolve it as `NoMatch(NO_GREEKS)` rather than hang.

### F4 — Act on `ERROR` frames  ⚑ robustness

`FEED_SUBSCRIPTION` with a bad/unknown symbol can yield an `ERROR`; MDS only logs
it. **Fix:** route `ERROR` to the waiting operation — fail the affected symbols
(BrokerSource) / surface to the watchdog (MDS) — instead of silently waiting out
the timeout.

### F5 — Fresh token per session  ⚑ robustness

`/api-quote-tokens` expires. BrokerSource fetches one per connect (cache with an
expiry check across resolves); MDS refreshes proactively before `expires-at`.

### F6 — `ping_interval=None` (keep)

Already correct in MDS; carry it into the shared client. DXLink's own keepalive
governs liveness; WS pings cause spurious `1006` under resubscribe load.

---

## 3. BrokerSource usage — bounded greeks collection

BrokerSource needs greeks only for `delta`/`premium` selection (strike/offset
selection is pure REST — no DXLink). The robust shape:

```python
async def collect_greeks(conn, streamer_symbols, *, timeout=3.0, need="delta"):
    """Subscribe, gather one usable greeks event per symbol, then unsubscribe.

    Returns {symbol: GreeksEvent} for symbols that delivered a non-NaN `need`
    field before `timeout`. Missing symbols are simply absent — the caller maps
    them to NoMatch(NO_GREEKS) if the selected strike is among them.
    """
    await conn.subscribe("Greeks", streamer_symbols)
    out, deadline = {}, time.monotonic() + timeout
    try:
        while streamer_symbols - out.keys() and time.monotonic() < deadline:
            ev = await conn.next_event("Greeks", timeout=deadline - time.monotonic())
            if ev and ev.delta is not None:        # non-NaN delta = usable
                out[ev.event_symbol] = ev
    finally:
        await conn.unsubscribe("Greeks", streamer_symbols)
    return out
```

Notes, each grounded in testing or the findings:

- **Latency budget is generous.** First event arrived **~150 ms** after connect
  in testing, so a 2–3 s budget covers handshake + first greeks comfortably; it
  exists to bound *stalls* (F3/F4), not normal flow.
- **Wait for a non-NaN `delta`, not just any Greeks frame.** Early greeks frames
  can carry nulls; selecting on a null delta is the bug F1 guards the parse side
  of.
- **Bound the subscription to a strike window.** Don't subscribe a whole chain to
  find one 30Δ strike — estimate ATM from the underlying future's price (a TT
  `Quote`/`Trade` on `/ESU6` or `/futures/v1`), subscribe a window around it.
- **Keep the connection warm if resolving repeatedly.** Connect→subscribe→
  unsubscribe→close per leg is wasteful (the handshake is ~120 ms but the token
  fetch + TLS is more). Pool one `DXLinkConnection` per BrokerSource instance;
  this is exactly the amortization MDS provides, which is why warm processes
  prefer `MdsRedisSource`.

---

## 4. `shared/dxlink/` module shape

Lift `services/mds/mds/dxlink/` → `packages/shared/shared/dxlink/`, refactored
around the findings:

- `DXLinkConnection` — owns the WS, the **reactive handshake** (F2), the 30s
  keepalive (F6), token-aware (re)connect (F5), and the per-symbol
  `last_feed_at` map (F3). Exposes `subscribe/unsubscribe`, an async
  `next_event(type, timeout)` queue, and a `ready` event.
- `EventParser` — driven by the **captured `FEED_CONFIG.eventFields`** (F1), not a
  constant. One method per event type building `QuoteEvent` / `GreeksEvent` /
  `TradeEvent` from a positional row.
- `collect_greeks(...)` — the bounded BrokerSource helper above.
- `StalenessWatchdog` — the F3 escalation (resubscribe → reconnect), used by MDS;
  BrokerSource relies on the collection timeout instead.
- `ERROR` routing (F4).

MDS keeps its persistent wiring (callbacks, heartbeat, instrumentation) on top of
`DXLinkConnection`; BrokerSource uses `DXLinkConnection` + `collect_greeks`.

---

## 5. Testing strategy

- **Offline / CI — the demo server** (`wss://demo.dxfeed.com/market-data/dxlink-ws`,
  no creds): covers handshake (F2), COMPACT parse against `FEED_CONFIG.eventFields`
  (F1), keepalive, reconnect, and `ERROR` handling. **Limitation:** the demo
  serves **no option greeks**, so `Greeks` field order / `delta` extraction
  cannot be asserted here.
- **Live smoke (gated)** — a TT-credentialed test (market hours) that subscribes
  real option streamer symbols and asserts the `Greeks` `delta` is parsed
  correctly from `FEED_CONFIG.eventFields`. This is the one assertion the demo
  can't cover; pin it with a recorded fixture from a real session for CI replay.

---

## Appendix — captured frames (dxfeed demo, 2026-06-30)

```
--> SETUP {channel:0, version:"0.1-py/1.0.0", keepaliveTimeout:60, acceptKeepaliveTimeout:60}
<-- SETUP {channel:0, version:"1.0-2.6.0", keepaliveTimeout:60, acceptKeepaliveTimeout:60}
<-- AUTH_STATE {state:"AUTHORIZED"}                       # demo: immediate, no token
--> CHANNEL_REQUEST {channel:1, service:"FEED", parameters:{contract:"AUTO"}}
<-- CHANNEL_OPENED  {channel:1, parameters:{contract:"AUTO", subFormat:"LIST"}}
--> FEED_SETUP {channel:1, acceptDataFormat:"COMPACT", acceptEventFields:{Quote:[...], Greeks:[...]}}
<-- FEED_CONFIG {channel:1, aggregationPeriod:0.1, dataFormat:"COMPACT"}
--> FEED_SUBSCRIPTION {channel:1, add:[{type:"Quote", symbol:"AAPL"}, ...]}
<-- FEED_CONFIG {channel:1, eventFields:{Quote:["eventType","eventSymbol","bidPrice","askPrice","bidSize","askSize"]}}
<-- FEED_DATA ["Quote", ["Quote","AAPL",285.88,285.91,400.0,1000.0]]    # t = 0.13s
<-- FEED_DATA ["Trade", ["Trade","AAPL",285.91,40.0]]
```

Findings observed directly: AUTH_STATE bursts with SETUP (→ F2); `FEED_CONFIG`
recurs per type carrying the authoritative `eventFields` (→ F1); first event at
~150 ms (→ §3 budget); the old `/dxlink-ws` path 400s, `/market-data/dxlink-ws`
gives `101`.

Sources: [dxFeed dxLink KB](https://kb.dxfeed.com/en/market-data-api/dxlink.html),
[dxLink WebSocket debug console](https://demo.dxfeed.com/market-data/dxlink-ws/debug/),
[TastyTrade streaming-market-data](https://developer.tastytrade.com/streaming-market-data/),
plus the live demo probe above and `services/mds/mds/dxlink/`.
