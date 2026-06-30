"""MassiveSource — backtest resolution over a mocked Massive (Polygon) API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from contract_resolver.description import ExpirationSpec, StrikeSelection
from contract_resolver.outcomes import LegMatch, NoMatch, NoMatchReason
from contract_resolver.sources.massive import MassiveSource, map_underlying, to_occ


# --------------------------------------------------------------------------- #
# symbology helpers
# --------------------------------------------------------------------------- #
def test_map_underlying_equity_vs_index():
    assert map_underlying("SPY") == ("SPY", "SPY", False)
    assert map_underlying("SPX") == ("I:SPX", "SPX", True)
    assert map_underlying("$SPX.X") == ("I:SPX", "SPX", True)


def test_to_occ_pads_root():
    assert to_occ("O:SPY260117C00500000") == "SPY   260117C00500000"
    assert to_occ("O:SPXW260117P05000000") == "SPXW  260117P05000000"
    assert to_occ("not-a-ticker") is None


# --------------------------------------------------------------------------- #
# fake transport
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _contract(ticker, strike, delta, close):
    return {
        "details": {"ticker": ticker, "strike_price": strike},
        "greeks": {"delta": delta},
        "day": {"close": close},
    }


SPY_CHAIN = [
    _contract("O:SPY260117C00500000", 500, 0.55, 3.0),
    _contract("O:SPY260117C00505000", 505, 0.45, 2.0),
    _contract("O:SPY260117C00510000", 510, 0.35, 1.0),
]


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if url.startswith("/v3/reference/options/contracts"):
            return FakeResp(
                {"results": [{"expiration_date": d} for d in ("2026-01-16", "2026-01-17", "2026-01-20")]}
            )
        if url.startswith("/v3/snapshot/options/"):
            return FakeResp({"results": SPY_CHAIN})
        return FakeResp({"results": []})


def make_source() -> MassiveSource:
    return MassiveSource("test-key", http_client=FakeClient())


CALL = "call"


def test_coverage_is_equity_index_only_and_not_realtime():
    cov = make_source().coverage()
    assert cov.instrument_types == frozenset({"Equity Option", "Index Option"})
    assert cov.realtime is False
    assert cov.resolves_chains is True


async def test_rejects_futures():
    out = await make_source().resolve_leg(
        "/ES", ExpirationSpec(dte=1), CALL, StrikeSelection("delta", target=0.3)
    )
    assert isinstance(out, NoMatch) and out.reason == NoMatchReason.UNSUPPORTED_INSTRUMENT


async def test_resolve_by_delta_picks_closest_and_stamps_as_of():
    as_of = datetime(2026, 1, 1)
    out = await make_source().resolve_leg(
        "SPY", ExpirationSpec(dte=16), CALL, StrikeSelection("delta", target=0.46), as_of=as_of
    )
    assert isinstance(out, LegMatch)
    leg = out.option
    assert leg.security.strike == Decimal("505")  # 0.45 closest to 0.46
    assert leg.security.security_id == "option:SPY:2026-01-17:call:505"
    assert leg.delta == Decimal("0.45")
    assert leg.mid == Decimal("2.0")  # delayed day-close proxy
    assert leg.source == "massive"
    assert leg.as_of == as_of


async def test_resolve_by_strike_exact():
    out = await make_source().resolve_leg(
        "SPY", ExpirationSpec(expiration_date=date(2026, 1, 17)), CALL, StrikeSelection("strike", target=510)
    )
    assert isinstance(out, LegMatch)
    assert out.option.security.strike == Decimal("510")


async def test_dte_snaps_to_nearest_listed_expiration():
    # dte=16 from 2026-01-01 -> target 2026-01-17, which is listed.
    src = make_source()
    out = await src.resolve_leg(
        "SPY", ExpirationSpec(dte=16), CALL, StrikeSelection("strike", target=500),
        as_of=datetime(2026, 1, 1),
    )
    assert isinstance(out, LegMatch)
    # the snapshot fetch used the snapped expiration
    snap_call = next(c for c in src._http.calls if c[0].startswith("/v3/snapshot"))
    assert snap_call[1]["expiration_date"] == "2026-01-17"


async def test_no_expiration_when_none_listed():
    class EmptyClient(FakeClient):
        async def get(self, url, params=None):
            return FakeResp({"results": []})

    src = MassiveSource("k", http_client=EmptyClient())
    out = await src.resolve_leg("SPY", ExpirationSpec(dte=1), CALL, StrikeSelection("strike", target=500))
    assert isinstance(out, NoMatch) and out.reason == NoMatchReason.NO_EXPIRATION
