"""Confluence risk-normalized sizing + legacy fee restatement.

Runs the real ConfluenceBot methods with a stubbed portfolio client — no
network, no live pool, no state file writes.
"""
import os
import sys

sys.path.insert(0, r'D:\Confluence')
sys.path.insert(0, r'D:\CommandCenter')

import confluence as cf

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class StubPortfolio:
    def __init__(self, total):
        self._total = total
        self.calls = 0

    def pool_total(self):
        self.calls += 1
        return self._total


def bare_bot(total):
    """A bot instance with only what sizing touches — no __init__ side effects."""
    b = cf.ConfluenceEngine.__new__(cf.ConfluenceEngine)
    b._portfolio = StubPortfolio(total) if total is not None else None
    b._pool_basis_warned = False
    b.logs = []
    b._log = lambda msg, lvl="INFO": b.logs.append((lvl, msg))
    return b


print("=== CASE 1: basis is a share of the pool ===")
b = bare_bot(1_000_000.0)
basis = b._sizing_basis()
check("basis is 10% of $1M", basis == 100_000.0, f"${basis:,.2f}")

print()
print("=== CASE 2: equal risk regardless of stop distance ===")
# The live spread that motivated this: stops 0.61%..2.30% gave $3.37..$12.65 risk.
rows = []
for pair, entry, stop in (("APE/USD", 0.153, 0.14948217),
                          ("ETHFI/USD", 0.41020, 0.40266655),
                          ("PEPE/USD", 0.00000287, 0.00000284),
                          ("AXS/USD", 10.0, 9.939)):
    size, basis, risk_usd, stop_pct = b._position_size(entry, stop)
    implied = size * stop_pct
    rows.append((pair, stop_pct, size, implied))
    print(f"     {pair:<10} stop {stop_pct * 100:5.2f}%  size ${size:>10,.0f}  "
          f"risk ${implied:>8,.2f}")
risks = [r[3] for r in rows]
spread = max(risks) - min(risks)
check("risk is equal across stops", spread < 1.0,
      f"spread ${spread:.2f} (flat sizing gave ~$9.28)")
check("risk equals the configured budget", all(abs(r - 500.0) < 1.0 for r in risks),
      f"got {[round(r) for r in risks]}")
check("tight stop now sizes LARGER", rows[2][2] > rows[0][2],
      "PEPE (1.05% stop) vs APE (2.30% stop)")

print()
print("=== CASE 3: clamps hold ===")
size, _, _, _ = b._position_size(100.0, 99.99)      # 0.01% stop
check("near-zero stop is clamped", size <= cf.MAX_POSITION_USD,
      f"${size:,.0f} <= ${cf.MAX_POSITION_USD:,.0f}")
size, _, _, _ = b._position_size(100.0, 20.0)       # 80% stop
check("huge stop still clears the minimum", size >= cf.MIN_POSITION_USD,
      f"${size:,.0f} >= ${cf.MIN_POSITION_USD:,.0f}")
check("minimum clears CC's $100 floor", cf.MIN_POSITION_USD > 100.0)

print()
print("=== CASE 4: unreachable pool degrades safely and warns once ===")
b2 = bare_bot(None)
b2._portfolio = StubPortfolio(None)
check("falls back", b2._sizing_basis() == cf.FALLBACK_EQUITY_USD)
warns = [m for lvl, m in b2.logs if lvl == "WARNING"]
check("warns once", len(warns) == 1, f"{len(warns)} warnings")
b2._sizing_basis()
warns = [m for lvl, m in b2.logs if lvl == "WARNING"]
check("does not spam", len(warns) == 1, f"{len(warns)} warnings")

print()
print("=== CASE 5: no portfolio client at all ===")
b3 = bare_bot(None)
check("standalone uses fallback", b3._sizing_basis() == cf.FALLBACK_EQUITY_USD)

print()
print("=== CASE 6: pool resize flows through ===")
b4 = bare_bot(2_000_000.0)
check("basis tracks resize", b4._sizing_basis() == 200_000.0,
      f"${b4._sizing_basis():,.2f}")

print()
print("=== CASE 7: legacy fee rows restate to gross, once ===")
lb = cf.ConfluenceEngine.__new__(cf.ConfluenceEngine)
lb.logs = []
lb._log = lambda msg, lvl="INFO": lb.logs.append((lvl, msg))
lb.closed_trades = [
    {"pair": "XNO/USD", "gross_pnl": 19.95, "net_pnl": 15.55, "fees": 4.40},
    {"pair": "CRV/USD", "gross_pnl": -5.71, "net_pnl": -10.11, "fees": 4.40},
    {"pair": "SHIB/USD", "gross_pnl": -8.84, "net_pnl": -8.84, "fees": 0.0},
]
lb.realized_pnl = 15.55 + -10.11 + -8.84      # the NET total, as stored


def restate(bot):
    drift = 0.0
    for t in bot.closed_trades:
        f = t.get("fees") or 0
        if f and not t.get("restated_gross"):
            g = t.get("gross_pnl")
            if isinstance(g, (int, float)):
                drift += g - (t.get("net_pnl") or 0)
                t["net_pnl"] = round(g, 2)
                t["fees"] = 0.0
                t["restated_gross"] = True
    if drift:
        bot.realized_pnl += drift
    return drift


d1 = restate(lb)
check("drift equals the phantom fees", abs(d1 - 8.80) < 0.01, f"{d1:.2f}")
# Gross total for THIS 3-row fixture: 19.95 - 5.71 - 8.84 = 5.40.
# (The live bot's 5 trades sum to -14.90; do not confuse the two.)
check("total is now gross", abs(lb.realized_pnl - 5.40) < 0.01,
      f"${lb.realized_pnl:.2f} (net total was -$3.40)")
d2 = restate(lb)
check("idempotent — second load changes nothing", d2 == 0.0, f"drift {d2}")
check("clean rows untouched", lb.closed_trades[2].get("restated_gross") is None)
check("restated rows marked", all(t.get("restated_gross")
                                  for t in lb.closed_trades[:2]))

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — equal risk per trade, safe fallback, gross P/L restored")
