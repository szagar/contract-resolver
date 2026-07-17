"""The ``OptionResolver`` port — the one capability the resolver depends on."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Mapping, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from .description import ExpirationSpec, StrikeListedConstraint, StrikeSelection
    from .models import Coverage, ResolvedLeg
    from .outcomes import LegResolution


@runtime_checkable
class OptionResolver(Protocol):
    """Resolves a single option leg against a source's own chain."""

    @property
    def name(self) -> str:
        """Provenance string stamped into resolved legs."""
        ...

    def coverage(self) -> "Coverage":
        """What this source can resolve / vouch for."""
        ...

    async def resolve_leg(
        self,
        underlying: str,
        expiration: "ExpirationSpec",
        option_type: Literal["call", "put"],
        selection: "StrikeSelection",
        *,
        siblings: Mapping[str, "ResolvedLeg"] | None = None,
        as_of: datetime | None = None,
        constraints: Sequence["StrikeListedConstraint"] | None = None,
    ) -> "LegResolution":
        """Resolve one leg. ``siblings`` carries already-resolved legs by role
        (for ``offset_from``). ``as_of=None`` means "now"; historical sources
        require it, live sources reject a past value. ``constraints`` (derived
        by the StructureResolver for calendarized parents) restrict candidates
        to strikes also listed at each constraint's expiration — see
        ``StrikeListedConstraint``."""
        ...
