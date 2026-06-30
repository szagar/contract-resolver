"""Source-neutral resolution types: candidate rows, resolved legs, coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from security_universe import Security


@dataclass(frozen=True)
class ContractRow:
    """One listed contract a source offers to ``select_contract``.

    Pricing/greeks are optional — only the selection method that needs them
    requires a source to populate them (delta/atm_offset need ``delta``; premium
    needs ``mid``; strike/offset/absolute_price need neither).
    """

    option_type: Literal["call", "put"]
    strike: Decimal
    symbol: str  # native / broker order symbol
    streamer_symbol: str | None = None
    expiration: date | None = None
    underlying: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    mid: Decimal | None = None
    delta: Decimal | None = None


@dataclass(frozen=True)
class ResolvedLeg:
    """A concrete, identified option leg. Identity is a ``security-universe``
    ``Security`` (carries ``security_id`` + ``order_symbol``); pricing/greeks
    carry the point-in-time values + provenance."""

    security: Security
    bid: Decimal | None = None
    ask: Decimal | None = None
    mid: Decimal | None = None
    delta: Decimal | None = None
    source: str = ""
    as_of: datetime | None = None

    @property
    def strike(self) -> Decimal | None:
        return self.security.strike

    @property
    def security_id(self) -> str | None:
        return self.security.security_id

    @property
    def order_symbol(self) -> str | None:
        return self.security.order_symbol


@dataclass(frozen=True)
class Coverage:
    """What a source can resolve, so the structure resolver can short-circuit."""

    instrument_types: frozenset[str]
    realtime: bool = True
    requires_as_of: bool = False
    resolves_chains: bool = True
