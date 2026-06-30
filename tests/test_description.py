"""Validation of the declarative description."""

from __future__ import annotations

from datetime import date

import pytest

from contract_resolver import ExpirationSpec, StrikeSelection


def test_target_methods_require_target() -> None:
    for m in ("delta", "strike", "premium", "absolute_price"):
        with pytest.raises(ValueError, match="requires 'target'"):
            StrikeSelection(m)


def test_atm_offset_requires_offset() -> None:
    with pytest.raises(ValueError, match="requires 'offset'"):
        StrikeSelection("atm_offset")


def test_offset_from_requires_role_and_offset() -> None:
    with pytest.raises(ValueError, match="requires 'role' and 'offset'"):
        StrikeSelection("offset_from", offset=10)
    with pytest.raises(ValueError):
        StrikeSelection("offset_from", role="short_put")


def test_valid_selections_construct() -> None:
    StrikeSelection("delta", target=0.16)
    StrikeSelection("offset_from", role="short_put", offset=-10)
    StrikeSelection("atm_offset", offset=0)


def test_expiration_spec_needs_a_form() -> None:
    with pytest.raises(ValueError):
        ExpirationSpec()
    ExpirationSpec(dte=45)
    ExpirationSpec(min_dte=30, max_dte=60)
    ExpirationSpec(expiration_date=date(2026, 7, 1))
