"""MassiveSource — historical/backtest resolution via Massive (Polygon-compatible).

An ``OptionResolver`` over Massive's options snapshot chain
(``/v3/snapshot/options/{asset}``): greeks/IV are real-time even on the delayed
tier, price is delayed (day bar — there is no NBBO on the Options Starter tier).
Equity + index options only — Massive has no futures, so options-on-futures never
route here (``coverage`` excludes them).

Backtest shape: ``resolve_leg`` takes an ``as_of`` and anchors the expiration to
that date; ``select_contract`` then runs over the snapshot rows. The headline
capability of threading ``as_of`` everywhere — the SAME ``OptionStructure``
resolved against (near-)historical data with no Redis, no MDS, no broker.

Caveat: the snapshot endpoint is point-in-time *now*; the Options Starter tier
does not serve historical greeks, so a deep-past ``as_of`` resolves greeks from
the current snapshot. True historical-greek replay is a tier limitation, not a
code one — documented in docs/plans/next-steps.md #1.

Requires the ``massive`` extra: ``contract-resolver[massive]`` (httpx).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import httpx
from security_universe.resolvers import ResolverChain

from ..description import ExpirationSpec, StrikeSelection
from ..identity import REASON_BY_METHOD, to_resolved_leg
from ..models import ContractRow, Coverage, ResolvedLeg
from ..outcomes import LegMatch, LegResolution, NoMatch, NoMatchReason
from ..select import select_contract

_COVERAGE = Coverage(
    instrument_types=frozenset({"Equity Option", "Index Option"}),
    realtime=False,
    requires_as_of=False,
    resolves_chains=True,
)

# Index roots whose snapshot lives under an ``I:`` asset (vs. a bare equity ticker).
_INDEX_ROOTS = frozenset(
    {"SPX", "SPXW", "NDX", "NDXP", "RUT", "RUTW", "VIX", "XSP", "DJX", "OEX"}
)
# Polygon option ticker, e.g. O:SPXW260624P07330000
_TICKER_RE = re.compile(r"^O:([A-Z]+)(\d{6})([CP])(\d{8})$")

_DEFAULT_BASE = "https://api.massive.com"


def map_underlying(underlying: str) -> tuple[str, str, bool]:
    """``(snapshot_asset, reference_ticker, is_index)``. Indices snapshot under
    ``I:SPX`` but reference the bare root; equities use the bare ticker for both."""
    root = underlying.upper().lstrip("$").replace(".X", "")
    is_index = root in _INDEX_ROOTS
    return (f"I:{root}" if is_index else root), root, is_index


def to_occ(polygon_ticker: str) -> str | None:
    """``O:SPXW260624P07330000`` -> OCC ``SPXW  260624P07330000`` (root padded to
    6) so the identity resolver parses it. ``None`` if it isn't a Polygon option."""
    m = _TICKER_RE.match(polygon_ticker)
    if not m:
        return None
    root, yymmdd, cp, strike8 = m.groups()
    return f"{root:<6}{yymmdd}{cp}{strike8}"


class MassiveSource:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE,
        http_client: httpx.AsyncClient | None = None,
        id_resolver: ResolverChain | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._http = http_client
        self._owns_http = http_client is None
        self._id = id_resolver or ResolverChain.default()

    @property
    def name(self) -> str:
        return "massive"

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
        if underlying.startswith("/"):
            return NoMatch(NoMatchReason.UNSUPPORTED_INSTRUMENT, "Massive has no futures")

        asset, ref_ticker, _ = map_underlying(underlying)
        ref_date = (as_of.date() if as_of is not None else _utc_today())
        exp = await self._resolve_expiration(ref_ticker, option_type, expiration, ref_date)
        if exp is None:
            return NoMatch(NoMatchReason.NO_EXPIRATION, "no listed expiration matches spec")

        results = await self._fetch_chain(asset, exp, option_type)
        rows = [r for r in (_to_row(res, option_type, exp, ref_ticker) for res in results) if r]
        if not rows:
            return NoMatch(NoMatchReason.NO_CHAIN, f"empty {option_type} snapshot")

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
        return LegMatch(to_resolved_leg(picked, self._id, self.name, as_of))

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                params={"apiKey": self._api_key},
                timeout=30.0,
            )
        return self._http

    async def _get(self, url: str, params: dict | None = None) -> dict:
        client = await self._client()
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def _resolve_expiration(
        self,
        ref_ticker: str,
        contract_type: Literal["call", "put"],
        spec: ExpirationSpec,
        ref_date: date,
    ) -> date | None:
        data = await self._get(
            "/v3/reference/options/contracts",
            {
                "underlying_ticker": ref_ticker,
                "contract_type": contract_type,
                "expired": "false",
                "sort": "expiration_date",
                "order": "asc",
                "limit": 1000,
            },
        )
        exps = sorted(
            {date.fromisoformat(c["expiration_date"]) for c in data.get("results", [])}
        )
        if not exps:
            return None
        if spec.expiration_date is not None:
            return spec.expiration_date if spec.expiration_date in exps else None
        if spec.dte is not None:
            target = ref_date + timedelta(days=spec.dte)
            return min(exps, key=lambda e: abs((e - target).days))
        lo = spec.min_dte if spec.min_dte is not None else 0
        hi = spec.max_dte if spec.max_dte is not None else 10**9
        window = [e for e in exps if lo <= (e - ref_date).days <= hi]
        return window[0] if window else None  # earliest in range

    async def _fetch_chain(
        self, asset: str, expiration: date, contract_type: Literal["call", "put"]
    ) -> list[dict]:
        url: str | None = f"/v3/snapshot/options/{asset}"
        params: dict | None = {
            "expiration_date": expiration.isoformat(),
            "contract_type": contract_type,
            "limit": 250,
            "order": "asc",
            "sort": "strike_price",
        }
        out: list[dict] = []
        while url:
            data = await self._get(url, params)
            out.extend(data.get("results", []))
            url = data.get("next_url")
            params = None  # next_url carries query state
        return out

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None


def _utc_today() -> date:
    return datetime.now().date()  # noqa: DTZ005 - cache anchor only, not resolution math


def _dec(v: Any) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def _to_row(
    res: dict, option_type: Literal["call", "put"], expiration: date, underlying: str
) -> ContractRow | None:
    details = res.get("details") or {}
    ticker = details.get("ticker")
    if not ticker:
        return None
    occ = to_occ(ticker)
    greeks = res.get("greeks") or {}
    day = res.get("day") or {}
    return ContractRow(
        option_type=option_type,
        strike=Decimal(str(details["strike_price"])),
        symbol=occ or ticker,  # OCC for identity; Polygon ticker as last resort
        streamer_symbol=ticker,
        expiration=expiration,
        underlying=underlying,
        # Delayed price proxy: the day bar close (no NBBO on this tier).
        mid=_dec(day.get("close")),
        delta=_dec(greeks.get("delta")),
    )
