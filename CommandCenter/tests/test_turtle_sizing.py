"""TurtleSue pool-share sizing: does it scale, degrade safely, and stay Turtle?

Runs the real TurtleEngine methods with a stubbed portfolio client so no
network or live pool is involved.
"""
import os
import sys

sys.path.insert(0, r'D:\TurtleSue')
sys.path.insert(0, r'D:\CommandCenter')
os.chdir(r'D:\TurtleSue')

import turtlebot as tb

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class StubClient:
    """Stands in for PortfolioClient."""
    def __init__(self, total):
        self._total = total
        self.calls = 0

    def pool_total(self):
        self.calls += 1
        return self._total

    def confirm_reservations(self, rids):
        return None


def fresh_engine(total, share=10.0):
    """Build an engine without touching disk, network or Kraken."""
    e = tb.TurtleEngine.__new__(tb.TurtleEngine)
    # Zero-based realized-P/L ledgers. There is no starting_equity any more:
    # this bot holds no capital, and CONFIG no longer carries a balance.
    e.equity = 0.0
    e.starting_equity = 0.0
    e.peak_equity = 0.0
    e.positions = {}
    e.errors = []
    e._pool_basis_cached = None
    e._pool_basis_warned = False
    e._portfolio_client = StubClient(total) if total is not None else None
    tb.CONFIG["equity_pool_share_pct"] = share
    e._refresh_pool_basis()
    return e


print("=== CASE 1: pool reachable — basis is a share of the pool ===")
e = fresh_engine(1_000_000.0, share=10.0)
basis = e._sizing_basis()
check("basis is 10% of $1M", basis == 100_000.0, f"got ${basis:,.2f}")
check("local ledger untouched", e.equity == 0.0, f"equity=${e.equity:,.2f}")
check("no error logged", not e.errors)

risk = tb.CONFIG["risk_per_unit_pct"]
n_uni = 0.2403
px_uni = 4.11
coins = tb.TurtleMath.unit_size(e._adjusted_equity(), n_uni, risk)
notional = coins * px_uni
print(f"     UNI/USD unit: {coins:.2f} coins  ${notional:,.2f} notional  "
      f"(risk ${basis * risk / 100:,.2f})")
check("risk per unit is 0.5% of basis", abs(basis * risk / 100 - 500.0) < 0.01)
check("unit notional in the $8k-20k band", 8_000 < notional < 20_000,
      f"${notional:,.2f}")

print()
print("=== CASE 2: pool unreadable — REFUSE to size, loudly ===")
# This case used to assert `_sizing_basis() == 10_000.0` under the heading
# "degrade to local equity". It was green the whole time and encoded the
# defect as the specification: that $10,000 came from a fixed
# CONFIG["starting_equity"], so against the real ~$210 pool it sized ~476x
# too large. Degrading toward a LARGER basis than reality is not degrading
# safely. One pool, one mind: unknown must refuse.
e = fresh_engine(None)
e._portfolio_client = StubClient(None)   # client present, returns None
e._refresh_pool_basis()
check("refuses to size", e._sizing_basis() is None,
      f"got {e._sizing_basis()!r} — must be None, not a guessed basis")
check("drawdown adjustment propagates the refusal",
      e._adjusted_equity() is None, f"got {e._adjusted_equity()!r}")
check("warns once", len(e.errors) == 1, f"errors={e.errors}")
e._refresh_pool_basis()
check("does not spam the warning", len(e.errors) == 1, f"errors={len(e.errors)}")

print()
print("=== CASE 3: no portfolio client at all (standalone) ===")
e = fresh_engine(None)
check("standalone refuses to size", e._sizing_basis() is None,
      f"got {e._sizing_basis()!r}")
check("standalone logs nothing", not e.errors)

print()
print("=== CASE 4: drawdown rule still applies, now to the basis ===")
e = fresh_engine(1_000_000.0, share=10.0)
# Drawdown is now measured against the POOL, not a fake $10k base. equity is
# a zero-based realized-P/L ledger, so -$200,000 on a $1,000,000 pool is a
# 20% drawdown => 2 reductions at the 10% threshold.
e.equity = -200_000.0
adj = e._adjusted_equity()
expected = 100_000.0 * (0.8 ** 2)
check("drawdown reduces the basis", abs(adj - expected) < 0.01,
      f"got ${adj:,.2f}, expected ${expected:,.2f}")
check("de-risking is real", adj < e._sizing_basis(),
      f"${adj:,.2f} < ${e._sizing_basis():,.2f}")

print()
print("=== CASE 5: pool resize flows through without a code change ===")
e = fresh_engine(2_000_000.0, share=10.0)
check("basis tracks a resized pool", e._sizing_basis() == 200_000.0,
      f"got ${e._sizing_basis():,.2f}")

print()
print("=== CASE 6: one pool read per scan, not per unit-size call ===")
e = fresh_engine(1_000_000.0, share=10.0)
before = e._portfolio_client.calls
for _ in range(5):
    e._sizing_basis()
    e._adjusted_equity()
check("sizing calls do not re-fetch", e._portfolio_client.calls == before,
      f"{e._portfolio_client.calls - before} extra fetches")

print()
print("=== CASE 7: share of 0 or missing disables the feature ===")
e = fresh_engine(1_000_000.0, share=0)
check("share=0 refuses to size", e._sizing_basis() is None,
      f"got {e._sizing_basis()!r} — a disabled share is not a licence to "
      f"size off a stand-in")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — pool-share sizing scales, degrades safely, stays Turtle")
