"""contract-resolver — declarative option-structure descriptions → concrete contracts.

Turns a *description* of an option leg or structure (a strike picked by
delta / premium / offset / absolute price, an expiration by DTE) into a
concrete, identified option contract — source-agnostic, backtest-safe, and
importable with no Redis / MDS / broker dependency in the pure core.

    from contract_resolver import (
        OptionStructure, StructureLeg, StrikeSelection, ExpirationSpec,
        StructureResolver, select_contract,
    )
    from contract_resolver.sources import StaticSource          # offline / backtest
    from contract_resolver.sources.broker import BrokerSource   # live TT [broker extra]

    resolution = await StructureResolver(source).resolve(structure)

Design: docs/plans/contract-resolver-design.md.
"""

from .description import ExpirationSpec, OptionStructure, StrikeSelection, StructureLeg
from .errors import StructureResolutionError
from .models import ContractRow, Coverage, ResolvedLeg
from .outcomes import (
    LegMatch,
    LegResolution,
    NoMatch,
    NoMatchReason,
    ResolvedStructure,
    StructureResolution,
)
from .protocol import OptionResolver
from .select import select_contract
from .structure_resolver import StructureResolver

__version__ = "0.1.0"

__all__ = [
    # description
    "StrikeSelection",
    "ExpirationSpec",
    "StructureLeg",
    "OptionStructure",
    # resolution
    "select_contract",
    "StructureResolver",
    "OptionResolver",
    # types
    "ContractRow",
    "ResolvedLeg",
    "Coverage",
    # outcomes
    "LegMatch",
    "NoMatch",
    "NoMatchReason",
    "LegResolution",
    "ResolvedStructure",
    "StructureResolution",
    "StructureResolutionError",
]
