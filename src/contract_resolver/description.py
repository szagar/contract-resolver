"""The declarative description: what structure you want (pure, serializable).

This is what a bot authors and what's stored as config. No I/O, no market data —
just a spec the resolution layer turns into concrete contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

StrikeSelectionMethod = Literal[
    "delta", "strike", "premium", "atm_offset", "offset_from", "absolute_price"
]


@dataclass(frozen=True)
class StrikeSelection:
    """How to pick a strike.

    * ``delta``          — option closest to ``target`` (abs delta, e.g. 0.16)
    * ``strike``         — the exact listed ``target`` strike
    * ``premium``        — option whose mid is closest to ``target`` dollars
    * ``atm_offset``     — the ATM (50Δ) strike + ``offset`` points
    * ``offset_from``    — another leg's (``role``) strike + ``offset`` points
    * ``absolute_price`` — snap to the listed strike nearest ``target`` dollars
    """

    method: StrikeSelectionMethod
    target: float | None = None
    role: str | None = None
    offset: float | None = None

    def __post_init__(self) -> None:
        m = self.method
        if m in ("delta", "strike", "premium", "absolute_price") and self.target is None:
            raise ValueError(f"strike selection {m!r} requires 'target'")
        if m == "atm_offset" and self.offset is None:
            raise ValueError("strike selection 'atm_offset' requires 'offset'")
        if m == "offset_from" and (self.role is None or self.offset is None):
            raise ValueError("strike selection 'offset_from' requires 'role' and 'offset'")


@dataclass(frozen=True)
class ExpirationSpec:
    """Which expiration: by DTE, a DTE window, or an explicit date."""

    dte: int | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    expiration_date: date | None = None

    def __post_init__(self) -> None:
        if (
            self.dte is None
            and self.min_dte is None
            and self.max_dte is None
            and self.expiration_date is None
        ):
            raise ValueError("ExpirationSpec needs dte, min_dte/max_dte, or expiration_date")


@dataclass(frozen=True)
class StrikeListedConstraint:
    """A derived (never authored) constraint on a parent leg's strike pick.

    Attached by the ``StructureResolver`` when an ``offset_from`` child sits in
    a DIFFERENT expiration than its parent (a calendarized structure): the
    parent's strike ``S`` is only pickable if ``S + offset`` is a listed strike
    at ``(expiration, option_type)``. Without it, a parent selected by delta can
    land on a strike the child's expiration doesn't list (weekly chains often
    have finer grids than later ones — e.g. SPXW 5-wide front vs 10/25-wide
    back) and the child fails unresolvable."""

    expiration: ExpirationSpec
    option_type: Literal["call", "put"]
    offset: float = 0.0


@dataclass(frozen=True)
class StructureLeg:
    role: str
    option_type: Literal["call", "put"]
    side: Literal["long", "short"]
    strike_selection: StrikeSelection
    expiration: ExpirationSpec | None = None  # falls back to the structure default
    ratio: int = 1


@dataclass(frozen=True)
class OptionStructure:
    underlying: str
    legs: tuple[StructureLeg, ...]
    expiration: ExpirationSpec | None = None  # default for legs that don't override
