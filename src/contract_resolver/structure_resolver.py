"""StructureResolver — composes per-leg resolution into a whole structure.

Source-agnostic: owns dependency ordering (parents before ``offset_from``
children), cycle detection, the coverage short-circuit, and per-leg failure
aggregation. Contains zero strike/chain logic — that lives behind the injected
``OptionResolver``. Returns role -> ``ResolvedLeg`` (no broker OrderLeg assembly;
that's the consuming edge's job).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .description import OptionStructure, StructureLeg
from .errors import StructureResolutionError
from .models import ResolvedLeg
from .outcomes import (
    LegMatch,
    LegResolution,
    NoMatch,
    NoMatchReason,
    ResolvedStructure,
    StructureResolution,
)
from .protocol import OptionResolver


class StructureResolver:
    def __init__(self, resolver: OptionResolver) -> None:
        self._resolver = resolver

    async def resolve(
        self, structure: OptionStructure, *, as_of: datetime | None = None
    ) -> StructureResolution:
        cov = self._resolver.coverage()
        if not cov.resolves_chains:
            return self._fail(NoMatchReason.UNSUPPORTED_CAPABILITY, "source does not resolve chains")
        instrument = "Future Option" if structure.underlying.startswith("/") else "Equity Option"
        if instrument not in cov.instrument_types:
            return self._fail(
                NoMatchReason.UNSUPPORTED_INSTRUMENT, f"{instrument} not covered for {structure.underlying}"
            )

        self._validate_graph(structure)  # circular offset_from is a programmer error

        resolved: dict[str, ResolvedLeg] = {}
        failures: list[NoMatch] = []

        # Pass 1: parents (everything that isn't offset_from).
        for leg in structure.legs:
            if leg.strike_selection.method == "offset_from":
                continue
            self._absorb(await self._one(structure, leg, resolved, as_of), leg, resolved, failures)

        # Pass 2: offset_from legs — parents are now resolved.
        for leg in structure.legs:
            if leg.strike_selection.method != "offset_from":
                continue
            parent = leg.strike_selection.role
            if parent not in resolved:
                failures.append(
                    NoMatch(NoMatchReason.SIBLING_UNRESOLVED, f"parent {parent!r} unresolved", leg.role)
                )
                continue
            self._absorb(await self._one(structure, leg, resolved, as_of), leg, resolved, failures)

        if failures:
            return StructureResolution(False, None, tuple(failures))
        return StructureResolution(True, ResolvedStructure(dict(resolved), structure), ())

    async def _one(
        self,
        structure: OptionStructure,
        leg: StructureLeg,
        resolved: dict[str, ResolvedLeg],
        as_of: datetime | None,
    ) -> LegResolution:
        exp = leg.expiration or structure.expiration
        if exp is None:
            raise StructureResolutionError(f"leg {leg.role!r} has no expiration and structure has no default")
        return await self._resolver.resolve_leg(
            structure.underlying, exp, leg.option_type, leg.strike_selection,
            siblings=dict(resolved), as_of=as_of,
        )

    @staticmethod
    def _absorb(
        outcome: LegResolution,
        leg: StructureLeg,
        resolved: dict[str, ResolvedLeg],
        failures: list[NoMatch],
    ) -> None:
        if isinstance(outcome, LegMatch):
            resolved[leg.role] = outcome.option
        else:
            failures.append(outcome if outcome.role else replace(outcome, role=leg.role))

    @staticmethod
    def _fail(reason: NoMatchReason, detail: str) -> StructureResolution:
        return StructureResolution(False, None, (NoMatch(reason, detail),))

    @staticmethod
    def _validate_graph(structure: OptionStructure) -> None:
        deps: dict[str, set[str]] = {leg.role: set() for leg in structure.legs}
        for leg in structure.legs:
            if leg.strike_selection.method == "offset_from":
                deps[leg.role].add(leg.strike_selection.role)  # type: ignore[arg-type]
        visited: set[str] = set()
        stack: set[str] = set()

        def dfs(node: str) -> None:
            visited.add(node)
            stack.add(node)
            for dep in deps.get(node, set()):
                if dep not in visited:
                    dfs(dep)
                elif dep in stack:
                    raise StructureResolutionError(f"circular offset_from involving {dep!r}")
            stack.discard(node)

        for role in deps:
            if role not in visited:
                dfs(role)
