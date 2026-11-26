"""How fast is contract-resolver's selection at sweep scale? Synthetic SPXW-sized chain, in memory."""
import asyncio
import math
import statistics
import time
from datetime import date
from decimal import Decimal

from contract_resolver.description import ExpirationSpec, OptionStructure, StrikeSelection, StructureLeg
from contract_resolver.models import ContractRow
from contract_resolver.select import select_contract
from contract_resolver.sources.static import StaticSource
from contract_resolver.structure_resolver import StructureResolver

S, VOL, T = 6700.0, 0.15, 1 / 365  # spot, vol, 0DTE-ish horizon
EXP = date(2026, 10, 16)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def chain():
    rows = []
    for k in range(5600, 7805, 5):  # 441 strikes per right, SPXW-like
        d1 = (math.log(S / k) + 0.5 * VOL * VOL * T) / (VOL * math.sqrt(T))
        for right in ("call", "put"):
            delta = ncdf(d1) if right == "call" else ncdf(d1) - 1
            intrinsic = max(0.0, S - k) if right == "call" else max(0.0, k - S)
            mid = round(intrinsic + 8 * math.exp(-abs(S - k) / 60), 2) + 0.05
            sym = f"SPXW  261016{'C' if right == 'call' else 'P'}{k * 1000:08d}"
            rows.append(ContractRow(option_type=right, strike=Decimal(k), symbol=sym, expiration=EXP,
                                    underlying="SPX", bid=Decimal(str(mid - 0.05)), ask=Decimal(str(mid + 0.05)),
                                    mid=Decimal(str(mid)), delta=Decimal(str(round(delta, 4)))))
    return rows


rows = chain()
puts = [r for r in rows if r.option_type == "put"]
print("chain rows:", len(rows))


def bench(label, fn, n):
    fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = (time.perf_counter() - t0) / n
    print(f"{label:52} {dt * 1e6:9.1f} µs/call  ({1 / dt:,.0f}/s)")
    return dt


bench("select_contract delta, 441 candidates", lambda: select_contract(puts, StrikeSelection(method="delta", target=0.16)), 2000)
bench("select_contract premium", lambda: select_contract(puts, StrikeSelection(method="premium", target=1.5)), 2000)
bench("select_contract offset_from", lambda: select_contract(puts, StrikeSelection(method="offset_from", role="SP", offset=-50), siblings={"SP": Decimal(6600)}), 2000)

ic = OptionStructure(underlying="SPX", expiration=ExpirationSpec(dte=0), legs=(
    StructureLeg("SP", "put", "short", StrikeSelection(method="delta", target=0.16)),
    StructureLeg("LP", "put", "long", StrikeSelection(method="offset_from", role="SP", offset=-50)),
    StructureLeg("SC", "call", "short", StrikeSelection(method="delta", target=0.16)),
    StructureLeg("LC", "call", "long", StrikeSelection(method="offset_from", role="SC", offset=50)),
))
src = StaticSource(rows)
res = StructureResolver(src)
out = asyncio.run(res.resolve(ic))
print("iron condor resolves:", type(out).__name__, getattr(out, "detail", "") or "")


async def many(n):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        await res.resolve(ic)
        ts.append(time.perf_counter() - t0)
    return ts


ts = asyncio.run(many(500))
med = statistics.median(ts)
print(f"{'StructureResolver iron condor (4 legs, identity incl.)':52} {med * 1e3:9.2f} ms median  p95 {sorted(ts)[int(.95 * len(ts))] * 1e3:.2f} ms")
for label, n in [("40-cell sweep x 4 yrs x 252 sessions x 1 entry", 40 * 4 * 252), ("same, 6 entries a day (MEIC)", 40 * 4 * 252 * 6)]:
    print(f"  {label:50} {n:>9,} resolutions ≈ {n * med / 60:6.1f} min single-threaded")
