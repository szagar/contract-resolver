"""contract-resolver — declarative option-structure descriptions → concrete contracts.

Turns a *description* of an option leg or structure (a strike picked by
delta / premium / offset / absolute price, an expiration by DTE) into a
concrete, identified option contract — source-agnostic, backtest-safe, and
importable with no Redis / MDS / broker dependency in the pure core.

Layering (one direction of dependency):

    security-universe  (identity: Security, security_id, symbology resolvers)
            ▲
    contract-resolver  (description + pure picker + composer + pluggable sources)

The pure core (description, ``select_contract``, ``StructureResolver``) does no
I/O. Market data enters only through an injected ``MarketDataSource`` — Massive
for equity/index options, a TastyTrade broker source for futures options, an
MDS/Redis source for the warm live path.

Status: design / pre-implementation. See ``docs/plans/contract-resolver-design.md``.
"""

__version__ = "0.1.0"
