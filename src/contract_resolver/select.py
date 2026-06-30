"""select_contract — the pure strike picker.

The one place the six selection methods are implemented. Source-agnostic: it
picks from whatever ``ContractRow`` candidates a source supplies. Both the live
broker source and any in-memory/backtest source share this, so selection can
never diverge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from .description import StrikeSelection
from .models import ContractRow


def _find_strike(candidates: Sequence[ContractRow], strike: Decimal) -> ContractRow | None:
    return next((c for c in candidates if c.strike == strike), None)


def select_contract(
    candidates: Sequence[ContractRow],
    selection: StrikeSelection,
    *,
    siblings: Mapping[str, Decimal] | None = None,
) -> ContractRow | None:
    """Pick the contract matching ``selection`` from ``candidates`` (one
    expiration + option type). ``siblings`` maps role -> resolved strike, for
    ``offset_from``. Returns ``None`` if nothing matches."""
    siblings = siblings or {}
    m = selection.method

    if m == "delta":
        scored = [c for c in candidates if c.delta is not None]
        if not scored:
            return None
        t_delta = abs(float(selection.target))  # type: ignore[arg-type]
        return min(scored, key=lambda c: abs(abs(float(c.delta)) - t_delta))  # type: ignore[arg-type]

    if m == "strike":
        return _find_strike(candidates, Decimal(str(selection.target)))

    if m == "premium":
        priced = [c for c in candidates if c.mid is not None]
        if not priced:
            return None
        t_mid = Decimal(str(selection.target))
        return min(priced, key=lambda c: abs(c.mid - t_mid))  # type: ignore[operator]

    if m == "absolute_price":
        if not candidates:
            return None
        t_price = Decimal(str(selection.target))
        return min(candidates, key=lambda c: abs(c.strike - t_price))

    if m == "atm_offset":
        scored = [c for c in candidates if c.delta is not None]
        if not scored:
            return None
        atm = min(scored, key=lambda c: abs(abs(float(c.delta)) - 0.5))  # type: ignore[arg-type]
        return _find_strike(candidates, atm.strike + Decimal(str(selection.offset)))

    if m == "offset_from":
        ref = siblings.get(selection.role)  # type: ignore[arg-type]
        if ref is None:
            return None
        return _find_strike(candidates, ref + Decimal(str(selection.offset)))

    return None
