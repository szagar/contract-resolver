"""Resolution errors (programmer errors, not market misses)."""

from __future__ import annotations


class StructureResolutionError(Exception):
    """Raised for malformed structures (missing expiration, circular offset_from).

    A *market miss* (no listed strike, no greeks) is NOT an error — it is a typed
    ``NoMatch`` outcome. This is for caller mistakes only.
    """
