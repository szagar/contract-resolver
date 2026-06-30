"""Pure select_contract tests — the six selection methods."""

from __future__ import annotations

from decimal import Decimal

from contract_resolver import StrikeSelection, select_contract
from contract_resolver.models import ContractRow


def _call(strike, delta=None, mid=None):
    return ContractRow("call", Decimal(str(strike)), f"C{strike}", delta=delta, mid=mid)


CALLS = [
    _call(6300, delta=Decimal("0.55"), mid=Decimal("30")),
    _call(6325, delta=Decimal("0.30"), mid=Decimal("18")),
    _call(6350, delta=Decimal("0.16"), mid=Decimal("9")),
    _call(6375, delta=Decimal("0.08"), mid=Decimal("4")),
]


def test_delta_picks_closest_abs_delta() -> None:
    assert select_contract(CALLS, StrikeSelection("delta", target=0.30)).strike == 6325
    assert select_contract(CALLS, StrikeSelection("delta", target=0.16)).strike == 6350


def test_strike_is_exact() -> None:
    assert select_contract(CALLS, StrikeSelection("strike", target=6350)).strike == 6350
    assert select_contract(CALLS, StrikeSelection("strike", target=6360)) is None


def test_premium_picks_closest_mid() -> None:
    assert select_contract(CALLS, StrikeSelection("premium", target=10)).strike == 6350


def test_absolute_price_snaps_to_nearest_strike() -> None:
    assert select_contract(CALLS, StrikeSelection("absolute_price", target=6360)).strike == 6350
    assert select_contract(CALLS, StrikeSelection("absolute_price", target=6364)).strike == 6375


def test_atm_offset_finds_50delta_then_offsets() -> None:
    # closest to 50Δ is 6300 (0.55); +50 -> 6350
    assert select_contract(CALLS, StrikeSelection("atm_offset", offset=50)).strike == 6350
    assert select_contract(CALLS, StrikeSelection("atm_offset", offset=0)).strike == 6300


def test_offset_from_uses_sibling_strike() -> None:
    sel = StrikeSelection("offset_from", role="short_call", offset=25)
    got = select_contract(CALLS, sel, siblings={"short_call": Decimal("6325")})
    assert got.strike == 6350
    # missing sibling -> None
    assert select_contract(CALLS, sel, siblings={}) is None


def test_delta_with_no_greeks_returns_none() -> None:
    bare = [ContractRow("call", Decimal("6300"), "C")]
    assert select_contract(bare, StrikeSelection("delta", target=0.3)) is None
