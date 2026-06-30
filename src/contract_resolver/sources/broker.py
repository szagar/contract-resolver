"""BrokerSource — live TastyTrade resolution: REST (chain + order symbol)
⊕ DXLink (greeks/quotes).

Built on the current dxlink-client core (DXLinkConnection + collect_greeks); no
staleness watchdog / reconnect needed — a resolution is a few-second, bounded
operation. strike/offset/absolute_price selection is pure REST (no market data);
delta/atm_offset add a bounded greeks subscription; premium adds quotes.

Requires the ``broker`` extra: ``contract-resolver[broker]`` (httpx, dxlink-client).
Live-only — a past ``as_of`` returns ``NoMatch(STALE_OR_NO_AS_OF)``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx
from dxlink_client import DXLinkConnection, GreeksEvent, QuoteEvent
from dxlink_client.providers.tastytrade import TastytradeTokenProvider
from security_universe.resolvers import ResolverChain

from ..description import ExpirationSpec, StrikeSelection
from ..identity import REASON_BY_METHOD, to_resolved_leg
from ..models import ContractRow, Coverage, ResolvedLeg
from ..outcomes import LegMatch, LegResolution, NoMatch, NoMatchReason
from ..select import select_contract

_COVERAGE = Coverage(
    instrument_types=frozenset({"Future Option", "Future", "Equity Option", "Index Option"}),
    realtime=True,
    requires_as_of=False,
    resolves_chains=True,
)
_MONTHS = "FGHJKMNQUVXZ"


def _future_product(underlying: str) -> str:
    """``/ES`` or ``/ESU6`` -> ``ES`` (the product code for the chain endpoint)."""
    m = re.match(rf"/?([A-Z0-9]+?)(?:[{_MONTHS}]\d{{1,2}})?$", underlying.upper())
    return m.group(1) if m else underlying.lstrip("/")


def _dec(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


class BrokerSource:
    def __init__(
        self,
        token_provider: TastytradeTokenProvider,
        *,
        id_resolver: ResolverChain | None = None,
        api_base: str = "https://api.tastytrade.com",
        connection: DXLinkConnection | None = None,
        http_client: httpx.AsyncClient | None = None,
        greeks_timeout: float = 3.0,
        quote_timeout: float = 3.0,
        enrich: bool = False,
    ) -> None:
        # enrich=True completes the *selected* leg with both price and delta
        # (one extra quote/greeks fetch for the single picked contract) so a
        # resolved leg carries everything an order needs. Default False keeps
        # strike/offset selection pure-REST and delta-selection greeks-only.
        self._provider = token_provider
        self._id = id_resolver or ResolverChain.default()
        self._api_base = api_base
        self._conn = connection          # warm connection (optional); else one-per-resolve
        self._http = http_client
        self._owns_http = http_client is None
        self._greeks_timeout = greeks_timeout
        self._quote_timeout = quote_timeout
        self._enrich = enrich

    @property
    def name(self) -> str:
        return "broker"

    def coverage(self) -> Coverage:
        return _COVERAGE

    async def resolve_leg(
        self,
        underlying: str,
        expiration: ExpirationSpec,
        option_type: Literal["call", "put"],
        selection: StrikeSelection,
        *,
        siblings: Mapping[str, ResolvedLeg] | None = None,
        as_of: datetime | None = None,
    ) -> LegResolution:
        if as_of is not None:
            return NoMatch(NoMatchReason.STALE_OR_NO_AS_OF, "broker source is live-only")

        expirations = await self._fetch_chain(underlying)
        exp = self._pick_expiration(expirations, expiration)
        if exp is None:
            return NoMatch(NoMatchReason.NO_EXPIRATION, "no listed expiration matches spec")
        rows = self._rows_for(exp, option_type, underlying)
        if not rows:
            return NoMatch(NoMatchReason.NO_CHAIN, f"no {option_type} candidates")

        if selection.method in ("delta", "atm_offset"):
            rows = await self._attach_greeks(rows)
            if not any(r.delta is not None for r in rows):
                return NoMatch(NoMatchReason.NO_GREEKS, "no greeks delivered for candidates")
        elif selection.method == "premium":
            rows = await self._attach_quotes(rows)

        sib_strikes = {
            role: leg.security.strike
            for role, leg in (siblings or {}).items()
            if leg.security.strike is not None
        }
        picked = select_contract(rows, selection, siblings=sib_strikes)
        if picked is None:
            return NoMatch(
                REASON_BY_METHOD.get(selection.method, NoMatchReason.NO_CHAIN),
                f"no match for {selection.method}",
            )
        if self._enrich:
            picked = await self._complete_leg(picked)
        return LegMatch(to_resolved_leg(picked, self._id, self.name, None))

    # ------------------------------------------------------------------ #
    # REST chain (structure + order symbol)
    # ------------------------------------------------------------------ #
    async def _http_get(self, path: str) -> dict:
        access = await self._provider.access_token()
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self._api_base, timeout=25.0)
        r = await self._http.get(path, headers={"Authorization": f"Bearer {access}"})
        r.raise_for_status()
        return r.json()

    async def _fetch_chain(self, underlying: str) -> list[dict]:
        if underlying.startswith("/"):
            data = await self._http_get(f"/futures-option-chains/{_future_product(underlying)}/nested")
            chains = data["data"].get("option-chains") or []
            return [e for ch in chains for e in self._expirations(ch)]
        data = await self._http_get(f"/option-chains/{underlying}/nested")
        return [e for item in data["data"]["items"] for e in self._expirations(item)]

    @staticmethod
    def _expirations(chain: dict) -> list[dict]:
        out = []
        for e in chain.get("expirations", []):
            d = e.get("expiration-date")
            out.append({
                "date": date.fromisoformat(d) if d else None,
                "dte": e.get("days-to-expiration"),
                "strikes": e.get("strikes", []),
            })
        return out

    @staticmethod
    def _pick_expiration(expirations: list[dict], spec: ExpirationSpec) -> dict | None:
        usable = [e for e in expirations if e.get("strikes")]
        if not usable:
            return None
        if spec.expiration_date is not None:
            return next((e for e in usable if e["date"] == spec.expiration_date), None)
        if spec.min_dte is not None or spec.max_dte is not None:
            lo = spec.min_dte if spec.min_dte is not None else -10**9
            hi = spec.max_dte if spec.max_dte is not None else 10**9
            window = [e for e in usable if e["dte"] is not None and lo <= e["dte"] <= hi]
            return min(window, key=lambda e: e["dte"]) if window else None
        if spec.dte is not None:
            scored = [e for e in usable if e["dte"] is not None]
            return min(scored, key=lambda e: abs(e["dte"] - spec.dte)) if scored else None
        return None

    @staticmethod
    def _rows_for(exp: dict, option_type: Literal["call", "put"], underlying: str) -> list[ContractRow]:
        sym_key = "call" if option_type == "call" else "put"
        str_key = f"{sym_key}-streamer-symbol"
        rows = []
        for s in exp["strikes"]:
            symbol = s.get(sym_key)
            if not symbol:
                continue
            rows.append(ContractRow(
                option_type=option_type,
                strike=Decimal(str(s["strike-price"])),
                symbol=symbol,
                streamer_symbol=s.get(str_key),
                expiration=exp["date"],
                underlying=underlying,
            ))
        return rows

    # ------------------------------------------------------------------ #
    # DXLink (greeks / quotes) — bounded
    # ------------------------------------------------------------------ #
    async def _attach_greeks(self, rows: Sequence[ContractRow]) -> list[ContractRow]:
        syms = [r.streamer_symbol for r in rows if r.streamer_symbol]
        conn, owned = await self._connection()
        try:
            greeks = await conn.collect_greeks(syms, timeout=self._greeks_timeout)
        finally:
            if owned:
                await conn.close()
        return [
            replace(r, delta=_dec(greeks[r.streamer_symbol].delta))
            if r.streamer_symbol in greeks
            else r
            for r in rows
        ]

    async def _attach_quotes(self, rows: Sequence[ContractRow]) -> list[ContractRow]:
        syms = {r.streamer_symbol for r in rows if r.streamer_symbol}
        conn, owned = await self._connection()
        mids: dict[str, tuple[Decimal | None, Decimal | None, Decimal | None]] = {}
        try:
            await conn.subscribe("Quote", list(syms))
            import time

            deadline = time.monotonic() + self._quote_timeout
            while syms - mids.keys() and time.monotonic() < deadline:
                ev = await conn.next_event(timeout=deadline - time.monotonic())
                if isinstance(ev, QuoteEvent) and ev.event_symbol in syms:
                    bid, ask = _dec(ev.bid_price), _dec(ev.ask_price)
                    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
                    mids[ev.event_symbol] = (bid, ask, mid)
            await conn.unsubscribe("Quote", list(syms))
        finally:
            if owned:
                await conn.close()
        out = []
        for r in rows:
            if r.streamer_symbol in mids:
                bid, ask, mid = mids[r.streamer_symbol]
                out.append(replace(r, bid=bid, ask=ask, mid=mid))
            else:
                out.append(r)
        return out

    async def _complete_leg(self, picked: ContractRow) -> ContractRow:
        """Fetch the missing price and/or delta for the single selected contract,
        so the resolved leg carries both (for order pricing + risk)."""
        sym = picked.streamer_symbol
        delta, bid, ask, mid = picked.delta, picked.bid, picked.ask, picked.mid
        need_greeks, need_quote = delta is None, mid is None
        if not sym or not (need_greeks or need_quote):
            return picked
        import time

        conn, owned = await self._connection()
        try:
            if need_greeks:
                await conn.subscribe("Greeks", [sym])
            if need_quote:
                await conn.subscribe("Quote", [sym])
            deadline = time.monotonic() + self._quote_timeout
            while (need_greeks and delta is None) or (need_quote and mid is None):
                if time.monotonic() >= deadline:
                    break
                ev = await conn.next_event(timeout=deadline - time.monotonic())
                if ev is None:
                    break
                if isinstance(ev, GreeksEvent) and ev.event_symbol == sym and ev.delta is not None:
                    delta = _dec(ev.delta)
                elif isinstance(ev, QuoteEvent) and ev.event_symbol == sym:
                    bid, ask = _dec(ev.bid_price), _dec(ev.ask_price)
                    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
            if need_greeks:
                await conn.unsubscribe("Greeks", [sym])
            if need_quote:
                await conn.unsubscribe("Quote", [sym])
        finally:
            if owned:
                await conn.close()
        return replace(picked, delta=delta, bid=bid, ask=ask, mid=mid)

    async def _connection(self) -> tuple[DXLinkConnection, bool]:
        if self._conn is not None:
            return self._conn, False
        conn = DXLinkConnection(self._provider)
        await conn.connect()
        return conn, True

    async def aclose(self) -> None:
        """Release the owned HTTP client (no-op for an injected one)."""
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None
