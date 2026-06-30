# contract-resolver

Resolve a **declarative description** of an option leg or structure into a
**concrete, identified option contract** — source-agnostic and backtest-safe.

A bot says *"the 30-delta call, 1 DTE, on /ES"*. That is a wish, not an
instrument. `contract-resolver` turns the wish into a real listed contract and
its TastyTrade order symbol, by:

1. **Description** — a pure, serializable spec of what you want
   (`StrikeSelection` by `delta` / `strike` / `premium` / `atm_offset` /
   `offset_from` / `absolute_price`; `ExpirationSpec` by DTE / date).
2. **Selection** — a pluggable `MarketDataSource` fetches the chain and a pure
   `select_contract` picks the leg; `StructureResolver` composes legs (with
   `offset_from` dependency ordering).
3. **Identity** — the picked contract becomes a
   [`security-universe`](../security-universe) `Security` with a stable
   `security_id` and its native broker symbol.

The pure core does **no I/O**. Market data enters only through an injected
source — Massive (equity/index options), a TastyTrade broker source (futures
options + cold chains), or an MDS/Redis source (warm live path). Swapping the
source — or injecting `as_of` for backtest — changes nothing above it.

## Design

The full design lives in
[`docs/plans/contract-resolver-design.md`](docs/plans/contract-resolver-design.md).

## Latency

End-to-end `BrokerSource.resolve_leg` against live TastyTrade, warm connection
(`examples/benchmark_latency.py`; machine/location dependent):

| selection path | latency (min/median ms) |
|---|---|
| pure REST (strike / offset / absolute_price) | ~60–80 |
| + greeks (delta) | ~95–115 |
| + greeks + quote (`enrich=True`) | ~115–185 |

Measured across stock (SPY), index (SPX), and futures (`/ES`). Pure-REST
resolution needs no market data; delta adds a bounded greeks fetch; `enrich`
completes the picked leg with its price.

## Status

Pre-implementation. The package is scaffolded; the design doc is the
authoritative spec.

## Non-goals

Does not place orders, manage positions, compute portfolio risk, or own a
quote feed. It resolves descriptions into contracts; everything else lives
above or beside it.
