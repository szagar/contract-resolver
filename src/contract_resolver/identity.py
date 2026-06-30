"""Turn a picked ``ContractRow`` into an identified ``ResolvedLeg``.

Builds a ``security-universe`` ``Security`` from the row (the resolver chain
fills ``security_id`` + ``order_symbol`` from the symbol; the known strike /
expiry / type are passed through and preserved).
"""

from __future__ import annotations

from datetime import datetime

from security_universe import Security
from security_universe.models import OptionType
from security_universe.resolvers import ResolverChain

from .models import ContractRow, ResolvedLeg
from .outcomes import NoMatchReason

# generic miss reason per selection method, when select_contract returns None
REASON_BY_METHOD = {
    "delta": NoMatchReason.NO_DELTA_MATCH,
    "premium": NoMatchReason.NO_PREMIUM_MATCH,
    "strike": NoMatchReason.NO_STRIKE_IN_TOLERANCE,
    "offset_from": NoMatchReason.NO_STRIKE_IN_TOLERANCE,
    "atm_offset": NoMatchReason.NO_STRIKE_IN_TOLERANCE,
    "absolute_price": NoMatchReason.NO_CHAIN,
}


def build_security(row: ContractRow, id_resolver: ResolverChain) -> Security:
    return id_resolver.resolve(
        Security(
            symbol=row.symbol,
            strike=row.strike,
            expiry=row.expiration,
            option_type=OptionType(row.option_type),
        )
    )


def to_resolved_leg(
    row: ContractRow, id_resolver: ResolverChain, source: str, as_of: datetime | None
) -> ResolvedLeg:
    return ResolvedLeg(
        security=build_security(row, id_resolver),
        bid=row.bid,
        ask=row.ask,
        mid=row.mid,
        delta=row.delta,
        source=source,
        as_of=as_of,
    )
