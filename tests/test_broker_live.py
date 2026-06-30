"""Live BrokerSource integration (gated on TASTYTRADE_* creds).

Confirms the whole stack end to end: description -> TT REST chain -> DXLink greeks
-> select_contract -> security-universe identity -> ResolvedLeg / structure.
Skipped automatically without creds; skips gracefully off-hours. No secrets printed.

Run:  uv run pytest -m tastytrade   (with TASTYTRADE_* in env / .env)
"""

from __future__ import annotations

import pytest
from dxlink_client.config import TastytradeSettings
from dxlink_client.providers.tastytrade import TastytradeTokenProvider

from contract_resolver import (
    ExpirationSpec,
    NoMatch,
    OptionStructure,
    StrikeSelection,
    StructureLeg,
    StructureResolver,
)
from contract_resolver.outcomes import LegMatch
from contract_resolver.sources.broker import BrokerSource

pytestmark = pytest.mark.tastytrade

_s = TastytradeSettings()
skip_no_creds = pytest.mark.skipif(
    not (_s.client_id and _s.client_secret and _s.refresh_token),
    reason="TASTYTRADE_* creds not set",
)


@skip_no_creds
async def test_live_resolve_future_option_delta() -> None:
    provider = TastytradeTokenProvider(_s)
    src = BrokerSource(provider)
    try:
        out = await src.resolve_leg(
            "/ES", ExpirationSpec(min_dte=0, max_dte=7), "call", StrikeSelection("delta", target=0.30)
        )
        if isinstance(out, NoMatch) and out.reason.value in ("no_greeks", "no_expiration", "no_chain"):
            pytest.skip(f"no live data: {out.reason.value} ({out.detail})")
        assert isinstance(out, LegMatch)
        sec = out.option.security
        assert sec.security_id.startswith("future_option:ES:")
        assert sec.order_symbol  # opaque OF order symbol carried
        assert out.option.delta is not None and -1.0 <= float(out.option.delta) <= 1.0
    finally:
        await src.aclose()
        await provider.aclose()


@skip_no_creds
async def test_live_resolve_equity_put_spread_structure() -> None:
    """Full structure: SPY put credit spread (short by delta, long by offset)."""
    provider = TastytradeTokenProvider(_s)
    src = BrokerSource(provider)
    structure = OptionStructure(
        underlying="SPY",
        expiration=ExpirationSpec(min_dte=0, max_dte=10),
        legs=(
            StructureLeg("short_put", "put", "short", StrikeSelection("delta", target=0.25)),
            StructureLeg("long_put", "put", "long", StrikeSelection("offset_from", role="short_put", offset=-5)),
        ),
    )
    try:
        res = await StructureResolver(src).resolve(structure)
        if not res.ok:
            reasons = {f.role: f.reason.value for f in res.failures}
            if any(r in ("no_greeks", "no_expiration", "no_chain", "no_strike_in_tolerance")
                   for r in reasons.values()):
                pytest.skip(f"no live data: {reasons}")
            assert res.ok, res.failures
        legs = res.resolved.legs
        assert set(legs) == {"short_put", "long_put"}
        # the long wing sits 5 points below the short
        assert legs["short_put"].strike - legs["long_put"].strike == 5
        assert all(leg.security_id and leg.security_id.startswith("option:") for leg in legs.values())
    finally:
        await src.aclose()
        await provider.aclose()
