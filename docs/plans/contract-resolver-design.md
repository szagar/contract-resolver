# contract-resolver — design

Status: design (pre-implementation)
Last updated: 2026-06-30

`contract-resolver` is a standalone, importable Python package that turns a
**declarative description** of an option leg or structure into a **concrete,
identified option contract** — source-agnostic, backtest-safe, and free of any
Redis / MDS / broker dependency in its pure core.

It is **part one of a larger refactor**. The systems ledger and the data
interfaces are being reviewed and likely replaced with their own standalone
modules; this document covers the resolution + identity layer only. Operational
migration risk is explicitly out of scope for this effort (greenfield cutover
assumed).

Related prior art (in `zts-massive`):
`docs/plans/standalone-market-data-source.md`,
`standalone-option-resolver.md`, `standalone-structure-resolver.md`, and the
sibling identity package [`security-universe`](../../../security-universe).

---

## 1. Problem

A bot declares a structure as a wish — *"sell the 16-delta put and call 45 DTE,
buy wings 25 points out"*, or *"the 30-delta /ES call, 1 DTE"*. Those are
descriptions, not instruments. Turning a description into a tradeable contract
requires three distinct things, today tangled together and coupled to Redis/MDS:

1. **Selection** — pick a strike from a chain (by delta, premium, offset, …).
2. **Market data** — the chain + greeks the selection reads.
3. **Identity** — the canonical id + the broker order symbol of the picked
   contract.

The goal: encapsulate (1) and (3) as **pure, importable** logic, and make (2) a
**pluggable port** so the same code resolves live, cold (no MDS), and in
backtest (`as_of`).

---

## 2. Three layers — and two different meanings of "resolver"

The word "resolver" means two unrelated things; keeping them distinct is the
key to the design.

| | **identity resolver** (security-universe) | **selection resolver** (this package) |
|---|---|---|
| Input | a **symbol string** (`SPXW260619C06100000`) | a **description** (`StrikeSelection(delta, 0.16)`) |
| Job | *what instrument is this?* | *which listed contract satisfies this?* |
| Needs market data? | No — pure, offline, deterministic | Yes — needs a chain snapshot + greeks |
| Output | enriched `Security` + `security_id` | a concrete strike (`ResolvedLeg`) |

They **compose sequentially** — selection produces a concrete contract, identity
names it:

```
StrikeSelection ──► [chain + select_contract] ──► concrete contract ──► Security(...).resolve() ──► security_id
   (description)        (selection layer)            (a listed option)     (identity layer)
```

`Security` is the **output** of selection, never the thing performing it. A
description is not yet a `Security`; you can't ask a `Security` to resolve a
strike. This is why market-data I/O must **not** live inside `Security` /
security-universe (whose Non-Goals and resolver contract forbid I/O), but inside
an injected source one level up.

---

## 3. Package boundaries

Three importable units, one direction of dependency:

```
┌─────────────────────────────────────────────────────────────┐
│ security-universe        (sibling package, already standalone)│
│   Security, SecurityType, identity resolvers (OCC/future/…)   │
│   PURE. no I/O. "what instrument is this?"                    │
└─────────────────────────────────────────────────────────────┘
        ▲ imports                          ▲ imports
        │                                  │
┌───────────────────────┐      ┌────────────────────────────────────────┐
│ contract_resolver      │      │ contract_resolver.sources (I/O adapters) │
│   description.py        │◄─────│   MassiveSource     [extra: massive]    │
│   outcomes.py           │      │   BrokerSource      [extra: broker]     │
│   models.py (ResolvedLeg)│     │   MdsRedisSource    (lives in zts shared)│
│   protocol.py           │      │   RoutingSource / CompositeSource       │
│   select.py  (PURE)     │      │   ─► emit Security objects via the      │
│   structure_resolver.py │      │      injected identity ChainResolver     │
│   PURE kernel           │      └────────────────────────────────────────┘
└───────────────────────┘
```

- **`security-universe`** — identity. `pydantic`, `pyyaml` only.
- **`contract_resolver` (pure kernel)** — description + `select_contract` +
  `StructureResolver` + the `MarketDataSource` port + `ResolvedLeg`. Deps:
  `pydantic`, `security-universe`. **No** redis / httpx / proto. Importable from
  a notebook with nothing running.
- **`contract_resolver.sources`** — the only I/O. Each source is an adapter
  behind the port; vendor deps (`httpx`, `websockets`) are optional extras
  (`[massive]`, `[broker]`), so the kernel installs without them.

Dependencies point one way (down-and-out). The kernel never imports a vendor.

---

## 4. The description vocabulary

A leg's strike is picked by one of **six** selection methods (the existing
`StrikeSelection` model, ported into `description.py`):

| Method | Picks the strike by… | Required fields |
|---|---|---|
| `delta` | option closest to a **target delta** (abs) | `target` |
| `strike` | an **exact listed strike** | `target` |
| `premium` | option whose **mid is closest to a $ target** | `target` |
| `atm_offset` | the **ATM (50Δ) strike + N points** | `offset` |
| `offset_from` | another resolved leg's strike **+ N points** (wings link to shorts) | `role` + `offset` |
| `absolute_price` | **snap to the listed strike nearest a computed $ price** | `target` |

- `StructureLeg` — `role`, `option_type`, `side`, `strike_selection`, optional
  per-leg `expiration`, `ratio`.
- `OptionStructure` — `underlying`, `legs`, default `ExpirationSpec`.
- `ExpirationSpec` — `dte` | `min_dte`/`max_dte` | `expiration_date`.

All frozen, serializable (`from_dict`/`to_dict`) — this is what bots author and
what is stored as JSONB. Pure; no I/O.

Worked structure examples (from real zts bots):

- **Iron condor** — shorts `delta`, wings `offset_from` short by `±wing_width`.
- **Iron butterfly** — short put `atm_offset 0`, short call `offset_from
  short_put 0` (co-located), wings `offset_from`.
- **MEIC** — shorts by `premium` (target $), wings `offset_from`.
- **Expected-move IC** — shorts by `absolute_price` (computed box edges), wings
  `offset_from`.

---

## 5. Pure selection core

### 5.1 `select_contract` — the extracted picker

The delta / premium / strike math currently lives as methods on
`OptionChainPopulator` (Redis-cache class) and is re-ported, "byte-identical,"
into `MdsRedisSource`. **Extract it once** into a pure function:

```python
def select_contract(candidates, selection, *, siblings) -> Contract | None:
    # delta:          min |abs(delta) - abs(target)|
    # strike:         exact listed strike
    # premium:        min |mid - target|
    # atm_offset:     find 50Δ ATM, then strike = atm.strike + offset
    # offset_from:    strike = siblings[role].strike + offset
    # absolute_price: min |strike - target|   (snap-to-nearest)
```

Both `MassiveSource` and `BrokerSource` (and the legacy `MdsRedisSource`) call
this — they **cannot diverge** on selection semantics.

### 5.2 `StructureResolver` — the composer

Stateless; holds one injected `OptionResolver`. Owns:

- **Two-pass dependency ordering** — pass 1 resolves non-`offset_from` legs
  (parents); pass 2 resolves `offset_from` legs with parents available as
  `siblings`.
- **Cycle detection** — DFS over `offset_from` references; a cycle is a
  programmer error, not a market miss.
- **Coverage short-circuit** — one check per structure against the source's
  `Coverage` (`UNSUPPORTED_INSTRUMENT` / `UNSUPPORTED_CAPABILITY`).
- **Failure aggregation** — one typed `NoMatch(reason, role)` per leg.

It returns `role → ResolvedLeg`. It does **not** build broker `OrderLeg`s (that
pulls proto/symbology) — `OrderLeg` assembly moves to the consuming edge so the
kernel stays free of `zts_protos`.

---

## 6. Identity layer — `Security` replaces the canonical string

`security-universe`'s `Security` is the identity value object: a validated
pydantic model carrying `security_id`, `underlying`, `expiry`, `strike`,
`option_type`, plus richer fields the bare canonical string can't —
`expiration_session` (AM/PM), `settlement_type` (cash/physical),
`contract_multiplier`, `deliverable`.

`security_id` formats (`:`-delimited, vendor-neutral):

| Instrument | `security_id` |
|---|---|
| Stock | `equity:AAPL` |
| Future (dated) | `future:ES:U6` |
| Option on stock/index | `option:SPXW:2026-06-19:put:6810` |
| **Option on future** | `future_option:ES:U6:2026-07-01:call:6325` *(new — §7)* |
| Crypto | `crypto:BTC-USD` |

`ResolvedLeg` carries a `Security` instead of loose identity fields:

```python
@dataclass(frozen=True)
class ResolvedLeg:
    security: Security          # identity (security_id, strike, settlement, …)
    bid: Decimal | None = None
    ask: Decimal | None = None
    mid: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega:  Decimal | None = None
    iv:    Decimal | None = None
    source: str = ""            # "massive" | "broker" | "mds_redis" | "backtest"
    as_of: datetime | None = None

    @property
    def strike(self) -> Decimal: return self.security.strike
    @property
    def canonical_symbol(self) -> str: return self.security.security_id
```

---

## 7. Future-option resolver (the gating new piece)

`security-universe` resolves equity/index options (OCC) but **not options on
futures** — futures options aren't OCC-format. Without this, an `OF`
`security_id` would be `None`, and `OF` can't be a primary key. This is the one
hard prerequisite for the big-bang (§13).

### 7.1 Symbology

TT-native futures-option symbol:

```
./ESU6 E1CN6 260701C6325
  │     │     └────────── option expiry 26-07-01, Call, strike 6325
  │     └──────────────── option product/series code (CME)   ← canonical.py DROPS this
  └────────────────────── underlying future contract ESU6
```

The product code (`E1CN6`) is **not computable** from
`(ESU6, 2026-07-01, call, 6325)` — it's CME taxonomy. Today's
`_parse_future_option` discards it, which is exactly why `to_tt(OF)` needs the
registry.

### 7.2 `security_id` format

```
future_option:{root}:{contract}:{YYYY-MM-DD}:{call|put}:{strike}
future_option:ES:U6:2026-07-01:call:6325
```

Mirrors the dated-future id (`future:ES:U6`) then appends the option quartet.
**Omits the product code** — `(underlying_future, expiry, right, strike)` is
already unique (matching zts's `OF|ESU6|20260701|6325|C`). Optional `:am`/`:pm`
segment only if a product lists both on one date.

### 7.3 The resolver (pure, in `security-universe`)

- Add `SecurityType.FUTURE_OPTION = "future_option"`.
- `parse_future_option_symbol` — port `FUTURE_OPTION_PATTERN`
  (`^\./([A-Z0-9]+[FGHJKMNQUVXZ]\d{1,2})\s+.*$`) and `_FUT_OPT_TRAILING`
  (`(\d{6})([CP])(\d+(?:\.\d+)?)$`), **and additionally capture the product
  code** (the token between head and trailing) that canonical.py drops.
- `FutureOptionSecurityIdResolver.resolve(security)` — pure; builds
  `security_id`, `short_name`, sets `underlying=ESU6`, `root_symbol=ES`,
  `option_root=E1CN6`, enriches `settlement_type`/`contract_multiplier` from a
  packaged `future_option_rules.yaml` (keyed by product root, e.g.
  `ES: {settlement_type: physical, contract_multiplier: 50}`), and stores
  `metadata = {native_symbol, product_code, underlying_future}` so the object is
  **non-lossy**.
- Wire first in `ChainResolver` (most specific: `./`-prefixed, spaced).

### 7.4 Required tests

Parse round-trip; product code captured (not dropped); `security_id` exact;
call/put; decimal strike normalization; whitespace/case; quarterly where product
== future code; physical settlement + multiplier from rule; unknown-root
fallback; micros (`MES`) / non-equity (`CL`); explicit fields preserved; original
not mutated.

---

## 8. Sources behind the port

`MarketDataSource` (= `OptionResolver` + a quote surface). Each source declares
`Coverage` and implements `resolve_leg(...)`.

| Source | Instruments | Greeks/quotes | `as_of`/history | Role |
|---|---|---|---|---|
| **MassiveSource** | `OS`, `OI` (equity/index) | greeks **real-time**, price **delayed bar** (no NBBO on Starter) | live + historical | equity/index selection; backtest of OS/OI |
| **BrokerSource** | `OF` (+ `OS`/`OI` cold) | TT REST (structure+symbol) ⊕ DXLink (greeks/quotes) | **live only** | futures options; cold/standalone (no MDS) |
| **MdsRedisSource** | all that MDS streams (incl. `OF`) | reads `md:quote:*` / `md:greeks:*` | live | the **warm** path; already in production |
| (backtest) | OS/OI | from `as_of` snapshot | historical | replay |

Routing dispatches by instrument type:

```python
source = RoutingSource(by_instrument={
    "Future Option": broker_or_mds,
    "Equity Option": massive_or_mds,
    "Index Option":  massive_or_mds,
})
```

### 8.1 MassiveSource

Massive (massive.com, Polygon-compatible) `Coverage`:

```python
Coverage(instrument_types=frozenset({"Equity Option", "Index Option"}),
         realtime=False, requires_as_of=False, resolves_chains=True)
```

**Mixed freshness** — each contract row merges the chain snapshot (greeks/IV/OI,
real-time) with an aggregate bar (price, delayed). Delta/IV selection is
**correct**; only the bar-derived `mid` is stale. No NBBO on the Starter tier →
not a live fill source; DXLink stays the fill source. Massive **does not cover
options on futures** (verified against the live API — see §11), so `OF` never
routes here.

### 8.2 BrokerSource — TT REST (structure) ⊕ DXLink (greeks)

`resolve_leg("/ES", dte=1, call, selection)`:

```
1. /ES → ESU6                  /instruments/futures?product-code[]=ES (active)   [or ActiveContractLookup]
2. dte=1 → 2026-07-01          resolve_expiration_for_dte
3. chain = /futures-option-chains/ES/nested        ← REST: full chain
     each strike row: {strike-price, call, call-streamer-symbol, put, put-streamer-symbol}
     ── .call / .put ARE the TT order symbols; .*-streamer-symbol are the dxfeed symbols
     pick expiration 2026-07-01, type=call → candidate rows {strike, symbol=row.call, streamer=row.call-streamer-symbol}

  ── selection by strike / offset / atm_offset / absolute_price ──
4a. select_contract(rows, selection) → row;  TT symbol = row.call           ← NO market data needed

  ── selection by delta / premium ──
4b. dx.subscribe(candidate streamer-symbols) → GreeksEvent.delta / QuoteEvent.mid   ← DXLink ONLY here
    select_contract(rows_with_greeks, selection) → row;  TT symbol = row.call

5. Security via FutureOptionSecurityIdResolver(symbol=row.call)
     → security_id + metadata.native_symbol = row.call
```

Key facts (verified against TT docs + zts code):

- The **chain structure and the TT order symbol come from one REST call**
  (`/futures-option-chains/{product}/nested`) — fields `call` / `put` (order
  symbols), `call-streamer-symbol` / `put-streamer-symbol`, `strike-price`,
  `root-symbol`, `exercise-style`, `settlement-type`, `expiration-date`. So
  **getting the TT symbol needs no registry and no DXLink.**
- **Greeks/quotes are not in TT REST** — only via the DXLink (dxfeed) streamer
  (`GreeksEvent.delta`, `QuoteEvent`), auth'd by `/api-quote-tokens`. DXLink is
  therefore needed **only for `delta`/`premium` selection**. Strike/offset
  selection is pure REST.
- **No historical greeks** → `OF` is **live-only**; a past `as_of` →
  `NoMatch(STALE_OR_NO_AS_OF)`.

`Coverage`:

```python
Coverage(instrument_types=frozenset({"Future Option","Future","Equity Option","Index Option"}),
         realtime=True, requires_as_of=False, resolves_chains=True)
```

Reuse / net-new:

- **Reuse:** `TastyTradeClient.get_futures_option_chain_nested / get_future_option
  / get_futures_by_product / get_quote_token`; MDS `dxlink/` event parsing;
  `TastytradeActiveContractLookup` (security-universe).
- **Net-new:** lift `services/mds/mds/dxlink/` → `shared/dxlink/` (shared by MDS
  and BrokerSource); a `collect_greeks(streamer_symbols, timeout)` helper; the
  `BrokerSource` class.

Caveats: keep DXLink **warm** if resolving repeatedly (that's what MDS
amortizes — warm processes should prefer `MdsRedisSource`); **bound** the
subscription to a strike window; **cache** REST structure per `(product,
expiration)` for the session.

---

## 9. `to_tt` and the native-symbol story (no registry in the hot path)

`Security → TT order symbol`:

| id | → TT symbol | pure? |
|---|---|---|
| `equity:AAPL` | `AAPL` | ✅ algorithmic |
| `future:ES:U6` | `/ESU6` | ✅ |
| `option:SPXW:2026-06-19:put:6810` | OCC build | ✅ |
| **`future_option:ES:U6:2026-07-01:call:6325`** | needs `E1CN6` | ❌ **lookup** |

`OF` is the only lossy direction (the id omits the product code by design —
redundant for *identity*, required for *naming*). Resolution:

```python
# Normal case — the Security carries it (captured during selection, §8.2 step 3/5):
to_tt(sec) -> sec.metadata["native_symbol"]      # "./ESU6 E1CN6 260701C6325"
```

`native_symbol` is **the resolution chain-read, retained** — it falls out of the
`/futures-option-chains/.../nested` response (`.call`/`.put`) that selection
already makes. It is *not* a second data source.

The injected `FutureOptionSymbolLookup` "fallback" is the **same chain read,
repeated** for a bare id that was never resolved here (a hand-typed id, an
external import, a pre-migration row):

```python
to_tt_from_id("future_option:ES:U6:2026-07-01:call:6325")
    -> FutureOptionSymbolLookup.tt_symbol(id)   # re-query /futures-option-chains/ES/nested → .call
```

So: **one authoritative source** (the nested chain endpoint), read once during
selection. "Carry `native_symbol`" = keep that read; "fallback" = re-resolve.
Persisting `native_symbol` alongside the id (exactly as zts already stores the TT
`symbol` column next to `canonical_symbol`) makes the fallback near-dead code.
The Redis registry (`reg:c2tt`) demotes from authority to an **optional cache**
of the chain endpoint — its cold-window concern disappears.

---

## 10. Vendor capability findings (verified 2026-06-30)

**Massive — no options on futures.** The Options product is OPRA only
(`/v3/snapshot/options/{underlyingAsset}` keyed on an equity/index ticker). The
Futures product (`/futures/v1/{snapshot,contracts,quotes,trades,aggs}`) covers
the **futures contracts themselves**, not options on them. There is no
options-on-futures chain endpoint. → `OF` selection cannot use Massive.

**TastyTrade — full futures-option chain incl. order symbol.**
`GET /futures-option-chains/{product}/nested` returns, per strike: `strike-price`,
`call`, `call-streamer-symbol`, `put`, `put-streamer-symbol`, plus chain-level
`root-symbol`, `exercise-style`, `settlement-type`, expiration-level
`expiration-date`/`days-to-expiration`. `call`/`put` are the TT **order
symbols**. Greeks/quotes are **not** in REST — only via DXLink. No historical
greeks. (Sources: TastyTrade developer docs / Postman collection; cross-checked
against `zts-massive` `TastyTradeClient` + MDS `dxlink`.)

---

## 11. Worked example — `/ES, 1 DTE, 30-delta call` → TT symbol

Today 2026-06-30 (Tue). ES ≈ 6300.

| Wish | Resolver | Pure? | Output |
|---|---|---|---|
| `/ES` front-month | `ActiveContractLookup` (TT) | injected | `ESU6` (Jun expired 06-19 → Sep) |
| `1 DTE` | `resolve_expiration_for_dte` | pure (given `as_of`) | `2026-07-01` |
| `30 delta` | `BrokerSource` REST + DXLink + `select_contract` | I/O + pure picker | strike `6325`, native `./ESU6 E1CN6 260701C6325` |
| identity | `FutureOptionSecurityIdResolver` | pure | `future_option:ES:U6:2026-07-01:call:6325` |
| → TT symbol | `to_tt(security)` | pure (field read) | `./ESU6 E1CN6 260701C6325` |

The TT symbol is **discovered from the chain at selection time**, not computed
from the description — the new design carries that discovered symbol forward on
the `Security` instead of round-tripping it through a registry.

---

## 12. Migration — big-bang (clean cut, no shims)

Operational risk is out of scope; optimize for the cleanest final code. Big-bang
deletes the strangler's residue that would otherwise become permanent:

| Strangler residue | Big-bang outcome |
|---|---|
| `legacy_canonical(sec)` bridge | gone — id is `security_id` |
| `ResolvedLeg` `@property` compat shims | gone |
| `CanonicalSymbol` legacy `OS\|…` format | gone |
| dual-read in DB/proto | gone |
| both ids coexisting in Redis/DB | gone |

Bonus only big-bang unlocks: **`CanonicalSymbol` dissolves** — its `from_tt`
parsing is what the security-universe resolvers already do; it collapses to a
thin `to_tt(security)` + the registry (now an optional cache).

Order:

1. Vendor `security-universe`; build the **future-option resolver** (§7) — the
   hard gate (without it `OF` can't be a primary key).
2. Extract `select_contract`; repoint sources at it (parity test vs the existing
   `StructureBuilder` oracle).
3. `ResolvedLeg` carries `Security`; sources build `Security` via an injected
   `ChainResolver`. No compat shims.
4. Move `OrderLeg` assembly to the OMS edge; collapse `CanonicalSymbol` to
   `to_tt(security)`.
5. Add `MassiveSource` (`[massive]`) and `BrokerSource` (`[broker]`,
   `shared/dxlink/` lift).
6. Single cutover: `security_id` as the platform-wide primary key (DB + proto;
   keep the proto field *named* `canonical_symbol`, change only its contents to
   minimize codegen churn).

You can keep the proto field name and just change its value semantics — no
`.proto` schema change.

---

## 13. Non-goals

- Placing orders / managing positions / portfolio risk (lives above).
- Owning a quote feed (sources adapt existing feeds; they don't replace them).
- The systems **ledger** and **data interfaces** — separate standalone modules
  in the same refactor, specified elsewhere.
- `OF` backtest — no historical futures-option greeks from TT or Massive;
  live-only.

---

## 14. Open questions

- **Index `SecurityType`** — security-universe has no `INDEX` type; SPX-the-index
  (vs its options) is currently typeless. Recoverable from option-root rules;
  decide whether to add it.
- **Warm BrokerSource** — for repeated cold resolution, hold a persistent DXLink
  connection vs. transient subscribe/unsubscribe. Likely defer to "use MDS when
  warm."
- **RoutingSource vs CompositeSource** — dispatch strictly by instrument type, or
  primary+fallback on `NO_CHAIN`? Leaning routing-by-type for determinism.
- **`security_id` for combos/futures spreads** — TT `contracts.type = combo`;
  out of initial scope.
