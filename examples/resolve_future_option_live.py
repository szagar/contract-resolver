"""Realtime (broker-based) resolution of a futures option.

Resolves a live `/ES` 30-delta call via BrokerSource and prints expiry, strike,
type, price (bid/ask/mid), and delta — all from the live TastyTrade chain ⊕
DXLink. Requires the `broker` extra and `TASTYTRADE_*` creds in `.env`:

    uv sync --extra broker
    uv run python examples/resolve_future_option_live.py
"""

import asyncio

from dxlink_client import DXLinkConnection
from dxlink_client.config import TastytradeSettings
from dxlink_client.providers.tastytrade import TastytradeTokenProvider

from contract_resolver import ExpirationSpec, StrikeSelection
from contract_resolver.outcomes import NoMatch
from contract_resolver.sources.broker import BrokerSource


async def main() -> None:
    settings = TastytradeSettings()
    if not (settings.client_id and settings.client_secret and settings.refresh_token):
        raise SystemExit("Set TASTYTRADE_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN in .env first.")

    provider = TastytradeTokenProvider(settings)
    conn = DXLinkConnection(provider)
    await conn.connect()                      # one warm connection (reused for greeks + quote)
    source = BrokerSource(provider, connection=conn, enrich=True)
    try:
        out = await source.resolve_leg(
            "/ES",
            ExpirationSpec(min_dte=0, max_dte=7),
            "call",
            StrikeSelection("delta", target=0.30),
        )
        if isinstance(out, NoMatch):
            print(f"NoMatch: {out.reason.value} ({out.detail})")
            return

        leg, sec = out.option, out.option.security
        print("Realtime /ES futures-option resolution — 30Δ call:\n")
        print(f"  expiry       : {sec.expiry}")
        print(f"  strike       : {sec.strike}")
        print(f"  type         : {sec.option_type.value}")
        print(f"  price (mid)  : {leg.mid}    (bid {leg.bid} / ask {leg.ask})")
        print(f"  delta        : {leg.delta}")
        print(f"  settlement   : {sec.settlement_type} · x{sec.contract_multiplier}")
        print(f"  security_id  : {sec.security_id}")
        print(f"  order_symbol : {sec.order_symbol}")
    finally:
        await conn.close()
        await source.aclose()
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
