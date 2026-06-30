"""Typed resolution outcomes — a miss carries *why*, never a bare None."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .description import OptionStructure
    from .models import ResolvedLeg


class NoMatchReason(str, Enum):
    NO_CHAIN = "no_chain"
    NO_EXPIRATION = "no_expiration"
    NO_STRIKE_IN_TOLERANCE = "no_strike_in_tolerance"
    NO_DELTA_MATCH = "no_delta_match"
    NO_PREMIUM_MATCH = "no_premium_match"
    SIBLING_UNRESOLVED = "sibling_unresolved"
    UNSUPPORTED_INSTRUMENT = "unsupported_instrument"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    STALE_OR_NO_AS_OF = "stale_or_no_as_of"
    NO_GREEKS = "no_greeks"


@dataclass(frozen=True)
class NoMatch:
    reason: NoMatchReason
    detail: str = ""
    role: str | None = None  # filled in by the structure resolver


@dataclass(frozen=True)
class LegMatch:
    option: "ResolvedLeg"


LegResolution = Union[LegMatch, NoMatch]


@dataclass(frozen=True)
class ResolvedStructure:
    legs: dict[str, "ResolvedLeg"]  # role -> resolved leg
    structure: "OptionStructure"


@dataclass(frozen=True)
class StructureResolution:
    ok: bool
    resolved: "ResolvedStructure | None"
    failures: tuple[NoMatch, ...] = ()
