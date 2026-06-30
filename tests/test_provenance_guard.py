"""Forcing the live source: allowed_sources gate + require_sources guard."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from contract_resolver import (
    ExpirationSpec,
    OptionStructure,
    StrikeSelection,
    StructureLeg,
    StructureResolutionError,
    StructureResolver,
    require_sources,
)
from contract_resolver.models import ContractRow
from contract_resolver.sources import StaticSource

ROWS = [
    ContractRow("put", Decimal(6175), "SPXW260702P06175000", expiration=date(2026, 7, 2), delta=Decimal("-0.16")),
]
STRUCT = OptionStructure(
    "SPX", expiration=ExpirationSpec(dte=3),
    legs=(StructureLeg("p", "put", "short", StrikeSelection("delta", target=0.16)),),
)


def test_static_source_is_not_realtime() -> None:
    # the signal a live gate keys on
    assert StaticSource(ROWS).coverage().realtime is False


async def test_allowed_sources_rejects_non_broker() -> None:
    resolver = StructureResolver(StaticSource(ROWS), allowed_sources={"broker"})
    with pytest.raises(StructureResolutionError, match="not in allowed_sources"):
        await resolver.resolve(STRUCT)


async def test_allowed_sources_permits_listed_source() -> None:
    # StaticSource.name == "static"
    res = await StructureResolver(StaticSource(ROWS), allowed_sources={"static"}).resolve(STRUCT)
    assert res.ok


async def test_require_sources_blocks_synthetic_legs() -> None:
    res = await StructureResolver(StaticSource(ROWS)).resolve(STRUCT)
    assert res.ok
    # a leg resolved by "static" must not pass a broker-only submit gate
    with pytest.raises(StructureResolutionError, match="disallowed source"):
        require_sources(res, {"broker"})
    # but its own source is fine
    assert require_sources(res, {"static"}) is res


async def test_require_sources_rejects_failed_resolution() -> None:
    empty = OptionStructure(
        "SPX", expiration=ExpirationSpec(dte=3),
        legs=(StructureLeg("c", "call", "short", StrikeSelection("delta", target=0.16)),),
    )
    res = await StructureResolver(StaticSource(ROWS)).resolve(empty)  # no calls -> fails
    assert not res.ok
    with pytest.raises(StructureResolutionError, match="did not succeed"):
        require_sources(res, {"static"})
