"""StaticSource — resolves legs against an in-memory list of ContractRows.

Pure and offline: the canonical way to unit-test the StructureResolver, and a
seed for backtest/replay. Filters candidates by underlying + option type, then
delegates the pick to ``select_contract``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from security_universe.resolvers import ResolverChain

from ..description import ExpirationSpec, StrikeListedConstraint, StrikeSelection
from ..identity import REASON_BY_METHOD, to_resolved_leg
from ..models import ContractRow, Coverage, ResolvedLeg
from ..outcomes import LegMatch, LegResolution, NoMatch, NoMatchReason
from ..select import filter_listed, no_match_detail, select_contract

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
        constraints: Sequence[StrikeListedConstraint] | None = None,
    ) -> LegResolution:
        ref_date = as_of.date() if as_of is not None else date.today()  # noqa: DTZ011
        candidates = self._typed_rows(underlying, option_type)
        if not candidates:
            return NoMatch(NoMatchReason.NO_CHAIN, f"no {option_type} candidates for {underlying}")

        # Expiration-aware when rows carry dates: pick from the leg's resolved
        # expiration only. Rows with expiration=None (date-less fixtures) always
        # stay candidates.
        exp = self._resolve_expiration(candidates, expiration, ref_date)
        if exp is not None:
            candidates = [r for r in candidates if r.expiration in (None, exp)]

        for c in constraints or ():
            listed = self._listed_strikes(underlying, c, ref_date)
            if listed is None:
                continue  # constraint expiration unresolvable — the child leg reports it
            candidates = filter_listed(candidates, listed, c.offset)
            if not candidates:
                return NoMatch(
                    NoMatchReason.NO_STRIKE_IN_TOLERANCE,
                    f"no {option_type} strike also listed at the offset_from sibling's expiration",
                )

        sib_strikes = {
            role: leg.security.strike
            for role, leg in (siblings or {}).items()
            if leg.security.strike is not None
        }
        picked = select_contract(candidates, selection, siblings=sib_strikes)
        if picked is None:
            reason = REASON_BY_METHOD.get(selection.method, NoMatchReason.NO_CHAIN)
            return NoMatch(reason, no_match_detail(selection, sib_strikes))
        return LegMatch(to_resolved_leg(picked, self._id, self.name, as_of))

    def _typed_rows(
        self, underlying: str, option_type: Literal["call", "put"]
    ) -> list[ContractRow]:
        return [
            r
            for r in self._rows
            if r.option_type == option_type and r.underlying in (None, underlying)
        ]

    @staticmethod
    def _resolve_expiration(
        rows: list[ContractRow], spec: ExpirationSpec, ref_date: date
    ) -> date | None:
        """The expiration ``spec`` resolves to among ``rows``' dates. Mirrors
        MassiveSource: exact date if given, else nearest listed to
        ``ref_date + dte``, else the earliest in the min/max window. ``None``
        when unresolvable (or no row carries a date)."""
        exps = sorted({r.expiration for r in rows if r.expiration is not None})
        if not exps:
            return None
        if spec.expiration_date is not None:
            return spec.expiration_date if spec.expiration_date in exps else None
        if spec.dte is not None:
            target = ref_date + timedelta(days=spec.dte)
            return min(exps, key=lambda e: abs((e - target).days))
        lo = spec.min_dte if spec.min_dte is not None else 0
        hi = spec.max_dte if spec.max_dte is not None else 10**9
        window = [e for e in exps if lo <= (e - ref_date).days <= hi]
        return window[0] if window else None

    def _listed_strikes(
        self, underlying: str, constraint: StrikeListedConstraint, ref_date: date
    ) -> set[Decimal] | None:
        """Strikes listed for the constraint's (expiration, option_type), from
        this source's rows. ``None`` when the expiration is unresolvable."""
        rows = [
            r
            for r in self._typed_rows(underlying, constraint.option_type)
            if r.expiration is not None
        ]
        exp = self._resolve_expiration(rows, constraint.expiration, ref_date)
        if exp is None:
            return None
        return {r.strike for r in rows if r.expiration == exp}
