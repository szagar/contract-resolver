"""StaticSource — resolves legs against an in-memory list of ContractRows.

Pure and offline: the canonical way to unit-test the StructureResolver, and a
seed for backtest/replay. Filters candidates by underlying + option type, then
delegates the pick to ``select_contract``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal

from security_universe.resolvers import ResolverChain

from ..description import ExpirationSpec, StrikeSelection
from ..identity import REASON_BY_METHOD, to_resolved_leg
from ..models import ContractRow, Coverage, ResolvedLeg
from ..outcomes import LegMatch, LegResolution, NoMatch, NoMatchReason
from ..select import select_contract

_DEFAULT_COVERAGE = Coverage(
    instrument_types=frozenset({"Equity Option", "Index Option", "Future Option"}),
    # In-memory / synthetic: NOT a live exchange chain. Marking this lets a live
    # order path refuse it (it can fabricate non-listed contracts; a broker chain
    # cannot). See StructureResolver(allowed_sources=...) and guards.require_sources.
    realtime=False,
)


class StaticSource:
    def __init__(
        self,
        rows: Iterable[ContractRow],
        *,
        id_resolver: ResolverChain | None = None,
        coverage: Coverage | None = None,
    ) -> None:
        self._rows = list(rows)
        self._id = id_resolver or ResolverChain.default()
        self._coverage = coverage or _DEFAULT_COVERAGE

    @property
    def name(self) -> str:
        return "static"

    def coverage(self) -> Coverage:
        return self._coverage

    async def resolve_leg(
        self,
        underlying: str,
        expiration: ExpirationSpec,
        option_type: Literal["call", "put"],
        selection: StrikeSelection,
        *,
        siblings: Mapping[str, ResolvedLeg] | None = None,
        as_of: datetime | None = None,
    ) -> LegResolution:
        candidates = [
            r
            for r in self._rows
            if r.option_type == option_type and r.underlying in (None, underlying)
        ]
        if not candidates:
            return NoMatch(NoMatchReason.NO_CHAIN, f"no {option_type} candidates for {underlying}")
        sib_strikes = {
            role: leg.security.strike
            for role, leg in (siblings or {}).items()
            if leg.security.strike is not None
        }
        picked = select_contract(candidates, selection, siblings=sib_strikes)
        if picked is None:
            reason = REASON_BY_METHOD.get(selection.method, NoMatchReason.NO_CHAIN)
            return NoMatch(reason, f"no match for {selection.method}")
        return LegMatch(to_resolved_leg(picked, self._id, self.name, as_of))
