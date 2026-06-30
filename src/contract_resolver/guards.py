"""Provenance guards for the live order path.

Contract selection is critical: a leg resolved by a synthetic / backtest /
delayed source must never reach a real order. Every ``ResolvedLeg`` records the
source that produced it (``leg.source``); these guards assert that provenance at
the irreversible boundary (order submission) — defense in depth on top of
``StructureResolver(allowed_sources=...)``.
"""

from __future__ import annotations

from collections.abc import Collection

from .errors import StructureResolutionError
from .outcomes import StructureResolution


def require_sources(
    resolution: StructureResolution, allowed: Collection[str]
) -> StructureResolution:
    """Assert every leg was produced by an allowed source, else raise.

    Use at the order edge:  ``require_sources(res, {"broker"})`` before submit.
    Raises on a failed resolution too — you can't submit what didn't resolve.
    Returns the resolution unchanged so it can be chained.
    """
    allowed = set(allowed)
    if not resolution.ok or resolution.resolved is None:
        raise StructureResolutionError("cannot submit: resolution did not succeed")
    offenders = {
        role: leg.source
        for role, leg in resolution.resolved.legs.items()
        if leg.source not in allowed
    }
    if offenders:
        raise StructureResolutionError(
            f"legs resolved by disallowed source(s) {offenders}; "
            f"allowed={sorted(allowed)} — refusing to submit"
        )
    return resolution
