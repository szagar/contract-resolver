"""Benchmark live BrokerSource.resolve_leg latency (broker data, end-to-end).

Measures the wall-clock to resolve a leg via BrokerSource across stock (SPY),
index (SPX), and futures (/ES) underlyings, for three selection paths:

  * pure REST     — strike/offset/absolute_price: chain fetch + select, NO market data
  * + greeks      — delta selection: REST + collect_greeks(candidates) + select
  * + greeks+quote — delta selection with enrich=True (completes the picked leg)

Complements dxlink-client's benchmark_latency.py, which measures the raw REST /
DXLink primitives; this measures the full resolution built on them.

Requires the `broker` extra + TASTYTRADE_* creds in `.env`. Run on a production
host for location-accurate numbers (latency depends on distance to TT/dxfeed):

    uv sync --extra broker
    uv run python examples/benchmark_latency.py
"""

from __future__ import annotations

import asyncio
import statistics
import time

from dxlink_client import DXLinkConnection
from dxlink_client.config import TastytradeSettings
from dxlink_client.providers.tastytrade import TastytradeTokenProvider

from contract_resolver import ExpirationSpec, StrikeSelection
from contract_resolver.outcomes import NoMatch
from contract_resolver.sources.broker import BrokerSource

EXP = ExpirationSpec(min_dte=0, max_dte=7)          # near expiry (liquid)
# (label, underlying, rough level for the pure-REST absolute_price snap)
TARGETS = [("stock  (SPY)", "SPY", 660), ("index  (SPX)", "SPX", 6300), ("futures (/ES)", "/ES", 7560)]


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


async def _timed(src: BrokerSource, underlying: str, sel: StrikeSelection, *, reps: int) -> dict | None:
    samples: list[float] = []
    for _ in range(reps):
        t = time.monotonic()
        out = await src.resolve_leg(underlying, EXP, "call", sel)
        dt = time.monotonic() - t
        if not isinstance(out, NoMatch):
            samples.append(dt)
    if not samples:
        return None
    return {"min": _ms(min(samples)), "med": _ms(statistics.median(samples)), "n": len(samples)}


async def main() -> None:
    s = TastytradeSettings()
    if not (s.client_id and s.client_secret and s.refresh_token):
        raise SystemExit("Set TASTYTRADE_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN in .env first.")

    provider = TastytradeTokenProvider(s)
    conn = DXLinkConnection(provider)
    await conn.connect()                                 # one warm connection, reused
    src = BrokerSource(provider, connection=conn, greeks_timeout=2.0)
    src_enrich = BrokerSource(provider, connection=conn, greeks_timeout=2.0, enrich=True)

    try:
        print("=== BrokerSource.resolve_leg latency (ms, min/median; warm connection) ===")
        print(f"{'underlying':16}{'pure REST':>14}{'+ greeks (Δ)':>16}{'+ greeks+quote':>18}")
        for label, underlying, level in TARGETS:
            rest = await _timed(src, underlying, StrikeSelection("absolute_price", target=level), reps=3)
            dlta = await _timed(src, underlying, StrikeSelection("delta", target=0.30), reps=2)
            enr = await _timed(src_enrich, underlying, StrikeSelection("delta", target=0.30), reps=2)

            def cell(r: dict | None) -> str:
                return f"{r['min']}/{r['med']}" if r else "—"

            print(f"{label:16}{cell(rest):>14}{cell(dlta):>16}{cell(enr):>18}")
    finally:
        await conn.close()
        await src.aclose()
        await src_enrich.aclose()
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
