# Next steps

Continuing the standalone decomposition (security-universe · dxlink-client ·
contract-resolver). Ordered roughly by leverage. Each builds on what's shipped;
none touches the live order path.

**Explicitly out of scope for now:** anything involving MDS (the persistent
streamer lift, the `F3` staleness watchdog / `F5` reconnect that exist for the
MDS profile), and ops/hardening (CI, publishing to an index, calendar guards).

---

## 1. Backtest the stack — `MassiveSource` + `as_of`

The design's headline capability that isn't built yet. A historical source
behind the `OptionResolver` port, driven by an injected `as_of`, so the **same**
`OptionStructure`s resolve against past data.

- `MassiveSource` (Polygon-compatible) — chain snapshot + greeks/IV at `as_of`,
  delayed price from aggregate bars. Behind the `[massive]` extra.
- A small **backtest harness** — resolve a bot's structure as-of a range of past
  dates and record what it would have selected.
- **Value:** validates contract selection offline against history — the payoff of
  threading `as_of` everywhere. **Risk:** none (new source, no live path).
- **Caveat:** Massive covers equity/index options only (no futures), so
  options-on-futures backtest stays unavailable; that's a known coverage limit.

## 2. Polish `BrokerSource`

- **Strike windowing** — bound the greeks subscription to a window around an ATM
  estimate instead of the whole expiration (the design caveat; benchmark shows
  it's not timeout-bound today, but it scales badly on wide chains).
- **`get_quotes` fill surface** — implement the `MarketDataSource` quote method so
  the source can price fills, not just resolve legs.
- **Warm-connection pool** — hold one `DXLinkConnection` across resolves to
  amortize the ~490 ms cold start (already supported via injection; make it the
  default ergonomic path).
- **Value:** efficiency + completeness. **Effort:** small–medium.

## 3. Source routing — `RoutingSource` / `CompositeSource`

Dispatch by instrument behind the port: `OS`/`OI` → Massive (or broker), `OF` →
broker; primary + fallback on `NO_CHAIN`. The multi-source composition the design
calls for, so a process picks the right backend per leg with zero caller change.

- **Value:** lets backtest (Massive) and live (broker) coexist cleanly.
  **Effort:** small.

## 4. Symbology inverse — `to_tt(security)`

The inverse of the identity resolvers: `Security` → broker order symbol.
Algorithmic for `S`/`I`/`F`/`OS`/`OI`/`CR`; `OF` reads `order_symbol`. This is the
prep that lets `CanonicalSymbol` collapse to a thin helper at the eventual
migration.

- **Value:** completes the identity round-trip (resolve ↔ name) as a standalone,
  registry-free path. **Effort:** small–medium. Lives in security-universe.

## 5. security-universe gaps

- **`INDEX` SecurityType** — SPX-the-index (vs. its options) currently has no
  type; decide whether to add one (recoverable from option-root rules today).
- **Combos / futures spreads** — TT `contracts.type = combo`; out of initial
  scope, a later identity family.

## 6. The broader decomposition (part two of the refactor)

You framed this stack as **part one**. The next standalone modules:

- **`ledger`** — positions / PnL / trade-groups as a clean, importable package
  (the same pattern as the three packages just built).
- **Data-interface modules** — the data interfaces decomposed into their own
  standalone packages.

These are larger tracks; sequence them after the resolution stack stabilizes.
