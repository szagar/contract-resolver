"""StructureResolver tests via the in-memory StaticSource (offline)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from contract_resolver import (
    Coverage,
    ExpirationSpec,
    OptionStructure,
    StrikeSelection,
    StructureLeg,
    StructureResolutionError,
    StructureResolver,
)
from contract_resolver.models import ContractRow
from contract_resolver.sources import StaticSource

EXP = date(2026, 7, 3)


def _occ(root: str, cp: str, strike: int) -> str:
    return f"{root}260703{cp}{strike * 1000:08d}"


def _row(otype: str, strike: int, delta: str) -> ContractRow:
    cp = "C" if otype == "call" else "P"
    return ContractRow(
        option_type=otype,
        strike=Decimal(strike),
        symbol=_occ("SPXW", cp, strike),
        expiration=EXP,
        delta=Decimal(delta),
    )


ROWS = [
    _row("put", 6300, "0.30"),
    _row("put", 6290, "0.16"),   # short_put (closest 0.16)
    _row("put", 6280, "0.10"),   # long_put = 6290 - 10
    _row("call", 6340, "0.30"),
    _row("call", 6350, "0.16"),  # short_call (closest 0.16)
    _row("call", 6360, "0.10"),  # long_call = 6350 + 10
]


def _iron_condor() -> OptionStructure:
    return OptionStructure(
        underlying="SPX",
        expiration=ExpirationSpec(dte=3),
        legs=(
            StructureLeg("short_put", "put", "short", StrikeSelection("delta", target=0.16)),
            StructureLeg("long_put", "put", "long", StrikeSelection("offset_from", role="short_put", offset=-10)),
            StructureLeg("short_call", "call", "short", StrikeSelection("delta", target=0.16)),
            StructureLeg("long_call", "call", "long", StrikeSelection("offset_from", role="short_call", offset=10)),
        ),
    )


async def test_resolves_iron_condor_with_offset_wings() -> None:
    res = await StructureResolver(StaticSource(ROWS)).resolve(_iron_condor())
    assert res.ok, res.failures
    legs = res.resolved.legs
    assert {r: legs[r].strike for r in legs} == {
        "short_put": Decimal(6290),
        "long_put": Decimal(6280),
        "short_call": Decimal(6350),
        "long_call": Decimal(6360),
    }
    # identity flowed through the security-universe resolver
    assert legs["short_put"].security_id == "option:SPXW:2026-07-03:put:6290"
    assert legs["short_call"].delta == Decimal("0.16")


async def test_offset_from_parent_miss_fails_that_leg() -> None:
    # No puts listed at all -> short_put can't resolve -> long_put can't offset from it.
    rows = [r for r in ROWS if r.option_type == "call"]
    res = await StructureResolver(StaticSource(rows)).resolve(_iron_condor())
    assert not res.ok
    reasons = {f.role: f.reason.value for f in res.failures}
    assert reasons["short_put"] == "no_chain"
    assert reasons["long_put"] == "sibling_unresolved"


FRONT = date(2026, 8, 7)
BACK = date(2026, 8, 14)


def _cal_row(otype: str, strike: int, exp: date, delta: str | None = None) -> ContractRow:
    cp = "C" if otype == "call" else "P"
    return ContractRow(
        option_type=otype,
        strike=Decimal(strike),
        symbol=f"SPXW  {exp:%y%m%d}{cp}{strike * 1000:08d}",
        expiration=exp,
        delta=Decimal(delta) if delta else None,
    )


# Front weekly lists a 5-wide grid; the back weekly is coarser (no 7255) —
# the real SPXW shape that made an unconstrained delta pick unresolvable.
CAL_ROWS = [
    _cal_row("put", 7250, FRONT, "0.30"),
    _cal_row("put", 7255, FRONT, "0.25"),  # exact-delta pick, but not listed at BACK
    _cal_row("put", 7260, FRONT, "0.21"),
    _cal_row("put", 7250, BACK),
    _cal_row("put", 7260, BACK),
]


def _put_calendar() -> OptionStructure:
    return OptionStructure(
        underlying="SPX",
        legs=(
            StructureLeg(
                "short_put", "put", "short", StrikeSelection("delta", target=0.25),
                expiration=ExpirationSpec(expiration_date=FRONT),
            ),
            StructureLeg(
                "long_put_back", "put", "long",
                StrikeSelection("offset_from", role="short_put", offset=0),
                expiration=ExpirationSpec(expiration_date=BACK),
            ),
        ),
    )


async def test_calendar_parent_constrained_to_back_listed_strikes() -> None:
    # Unconstrained, delta would pick 7255 (exact 0.25) — which the back
    # expiration doesn't list, so offset_from would fail. The derived
    # StrikeListedConstraint keeps the parent on {7250, 7260}; 7260 wins
    # (|0.21-0.25| < |0.30-0.25|) and the calendar resolves.
    res = await StructureResolver(StaticSource(CAL_ROWS)).resolve(_put_calendar())
    assert res.ok, res.failures
    legs = res.resolved.legs
    assert legs["short_put"].strike == Decimal(7260)
    assert legs["long_put_back"].strike == Decimal(7260)


async def test_calendar_with_no_common_strikes_fails_the_parent() -> None:
    rows = [r for r in CAL_ROWS if not (r.expiration == BACK)] + [_cal_row("put", 7000, BACK)]
    res = await StructureResolver(StaticSource(rows)).resolve(_put_calendar())
    assert not res.ok
    reasons = {f.role: f.reason.value for f in res.failures}
    assert reasons["short_put"] == "no_strike_in_tolerance"
    assert reasons["long_put_back"] == "sibling_unresolved"


async def test_same_expiration_offset_from_gets_no_constraint() -> None:
    # The iron condor (all legs one expiration) resolves exactly as before —
    # _parent_constraints must not manufacture constraints for same-expiry
    # children (they pick from the same chain by construction).
    res = await StructureResolver(StaticSource(ROWS)).resolve(_iron_condor())
    assert res.ok, res.failures
    assert res.resolved.legs["short_put"].strike == Decimal(6290)


async def test_circular_offset_from_raises() -> None:
    struct = OptionStructure(
        underlying="SPX",
        expiration=ExpirationSpec(dte=3),
        legs=(
            StructureLeg("a", "call", "long", StrikeSelection("offset_from", role="b", offset=5)),
            StructureLeg("b", "call", "long", StrikeSelection("offset_from", role="a", offset=5)),
        ),
    )
    with pytest.raises(StructureResolutionError, match="circular"):
        await StructureResolver(StaticSource(ROWS)).resolve(struct)


async def test_coverage_gate_short_circuits() -> None:
    src = StaticSource(ROWS, coverage=Coverage(instrument_types=frozenset({"Equity Option"}), resolves_chains=False))
    res = await StructureResolver(src).resolve(_iron_condor())
    assert not res.ok and res.failures[0].reason.value == "unsupported_capability"
