"""BrokerSource tests with mocked TT REST + a fake DXLink connection (offline)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import httpx
from dxlink_client import GreeksEvent, QuoteEvent
from dxlink_client.config import TastytradeSettings
from dxlink_client.providers.tastytrade import TastytradeTokenProvider

from contract_resolver import ExpirationSpec, NoMatch, StrikeSelection
from contract_resolver.outcomes import LegMatch
from contract_resolver.sources.broker import BrokerSource

API = "https://api.tastytrade.com"


def _occ_call(strike: int) -> str:
    return f"SPY260703C{strike * 1000:08d}"


def _equity_chain() -> dict:
    strikes = [
        {
            "strike-price": str(s),
            "call": _occ_call(s),
            "call-streamer-symbol": f".SPY260703C{s}",
            "put": f"SPY260703P{s * 1000:08d}",
            "put-streamer-symbol": f".SPY260703P{s}",
        }
        for s in (650, 653, 656)
    ]
    return {"data": {"items": [
        {"expirations": [{"expiration-date": "2026-07-03", "days-to-expiration": 3, "strikes": strikes}]}
    ]}}


def _future_chain() -> dict:
    strikes = [
        {
            "strike-price": str(s),
            "call": f"./ESU6 E1AN6  260701C{s}",
            "call-streamer-symbol": f"./E1AN6C{s}:XCME",
        }
        for s in (6290, 6300, 6310)
    ]
    return {"data": {"option-chains": [
        {"root-symbol": "ES", "expirations": [
            {"expiration-date": "2026-07-01", "days-to-expiration": 1, "strikes": strikes}
        ]}
    ]}}


def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 900})
        if p == "/option-chains/SPY/nested":
            return httpx.Response(200, json=_equity_chain())
        if p == "/futures-option-chains/ES/nested":
            return httpx.Response(200, json=_future_chain())
        return httpx.Response(404, json={"path": p})

    return httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(handler))


def _provider(client: httpx.AsyncClient) -> TastytradeTokenProvider:
    s = TastytradeSettings(client_id="cid", client_secret="csec", refresh_token="rt", api_base=API)
    return TastytradeTokenProvider(s, http_client=client)


class _FakeConn:
    """Stands in for DXLinkConnection: canned greeks per streamer symbol."""

    def __init__(self, deltas: dict[str, float]) -> None:
        self._deltas = deltas

    async def collect_greeks(self, symbols, *, timeout: float = 3.0):
        return {s: GreeksEvent(s, delta=self._deltas[s]) for s in symbols if s in self._deltas}

    async def close(self) -> None:
        pass


async def test_equity_strike_selection_no_market_data() -> None:
    client = _client()
    src = BrokerSource(_provider(client), http_client=client)  # no connection needed for strike
    out = await src.resolve_leg("SPY", ExpirationSpec(dte=3), "call", StrikeSelection("strike", target=653))
    assert isinstance(out, LegMatch)
    assert out.option.security_id == "option:SPY:2026-07-03:call:653"
    assert out.option.strike == 653
    await client.aclose()


class _EnrichConn(_FakeConn):
    """Adds the subscribe/next_event surface so enrich can fetch one quote."""

    def __init__(self, deltas: dict[str, float], quote: QuoteEvent) -> None:
        super().__init__(deltas)
        self._quote = quote
        self._sent = False

    async def subscribe(self, event_type, symbols, **kw) -> None:
        pass

    async def unsubscribe(self, event_type, symbols) -> None:
        pass

    async def next_event(self, timeout=None):
        if not self._sent:
            self._sent = True
            return self._quote
        return None


async def test_enrich_completes_selected_leg_with_price() -> None:
    client = _client()
    deltas = {".SPY260703C650": 0.55, ".SPY260703C653": 0.30, ".SPY260703C656": 0.12}
    quote = QuoteEvent(".SPY260703C653", bid_price=5.0, ask_price=5.2)  # the picked strike
    src = BrokerSource(
        _provider(client), http_client=client,
        connection=_EnrichConn(deltas, quote), enrich=True,
    )
    out = await src.resolve_leg("SPY", ExpirationSpec(dte=3), "call", StrikeSelection("delta", target=0.30))
    assert isinstance(out, LegMatch)
    assert out.option.strike == 653
    assert out.option.delta is not None          # delta from candidate greeks
    assert out.option.mid == Decimal("5.1")      # price from the enrich quote
    await client.aclose()


async def test_equity_delta_selection_uses_greeks() -> None:
    client = _client()
    deltas = {".SPY260703C650": 0.55, ".SPY260703C653": 0.30, ".SPY260703C656": 0.12}
    src = BrokerSource(_provider(client), http_client=client, connection=_FakeConn(deltas))
    out = await src.resolve_leg("SPY", ExpirationSpec(dte=3), "call", StrikeSelection("delta", target=0.30))
    assert isinstance(out, LegMatch)
    assert out.option.strike == 653 and out.option.delta is not None
    await client.aclose()


async def test_future_option_strike_selection_identity() -> None:
    client = _client()
    src = BrokerSource(_provider(client), http_client=client)
    out = await src.resolve_leg("/ES", ExpirationSpec(dte=1), "call", StrikeSelection("strike", target=6300))
    assert isinstance(out, LegMatch)
    sec = out.option.security
    assert sec.security_id == "future_option:ES:U6:2026-07-01:call:6300"
    assert sec.order_symbol == "./ESU6 E1AN6  260701C6300"  # opaque order symbol carried
    await client.aclose()


async def test_as_of_is_rejected_live_only() -> None:
    client = _client()
    src = BrokerSource(_provider(client), http_client=client)
    out = await src.resolve_leg(
        "SPY", ExpirationSpec(dte=3), "call", StrikeSelection("strike", target=653),
        as_of=datetime(2020, 1, 1),
    )
    assert isinstance(out, NoMatch) and out.reason.value == "stale_or_no_as_of"
    await client.aclose()
