"""The lease sweep must not release capital under a reported position.

Two sweeps release reservations. The 48-hour force_release_stale takes an
active_positions set and skips anything a bot still reports. The 10-minute
lease sweep in confirm() did not — the MORE aggressive path had the WEAKER
guard.

Reproduced in isolation before the fix: a single confirm([]) from a bot whose
position store failed to load swept two live reservations totalling $2,664.24
while both positions remained open on the venue. That capital then became
available for another bot to reserve against the same positions.

An empty declaration is indistinguishable from a bot that genuinely closed
everything — which is exactly why the declaration cannot be the only evidence.
"""
import sys
import threading
import time

sys.path.insert(0, r'D:\CommandCenter')

import command_center as cc

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def mk():
    """A manager holding three aged, undeclared reservations."""
    pm = cc.PortfolioManager.__new__(cc.PortfolioManager)
    pm._lock = threading.RLock()
    pm.history = []
    pm._save = lambda: None
    pm._bot_confirms = {}
    old = time.time() - 1000        # past both grace windows
    pm.reservations = {
        "r1": {"bot_id": "turtlesue", "pair": "UNI/USD", "amount": 1878.21,
               "reserved_at": old, "unconfirmed_since": old},
        "r2": {"bot_id": "turtlesue", "pair": "XLM/USD", "amount": 786.03,
               "reserved_at": old, "unconfirmed_since": old},
        "r3": {"bot_id": "turtlesue", "pair": "DOT/USD", "amount": 500.00,
               "reserved_at": old, "unconfirmed_since": old},
    }
    return pm


print("=== CASE 1: empty confirm, but the bot still reports 2 positions ===")
pm = mk()
active = {cc._position_key("turtlesue", "UNI/USD"),
          cc._position_key("turtlesue", "XLM/USD")}
r = pm.confirm("turtlesue", [], active_positions=active)
swept = {s["reservation_id"] for s in r["swept"]}
held = sum(x["amount"] for x in pm.reservations.values())
print(f"     swept={sorted(swept)}  held=${held:,.2f}")
check("live positions protected", swept == {"r3"}, f"swept {sorted(swept)}")
check("their capital retained", abs(held - 2664.24) < 0.01, f"${held:,.2f}")

print()
print("=== CASE 2: a GENUINE orphan is still reclaimed ===")
check("orphan swept", "r3" in swept,
      "the bot no longer reports DOT/USD — this must still be freed")

print()
print("=== CASE 3: Kraken-format pair keys still match ===")
# _position_key normalizes, so a bot reporting the exchange code must still
# protect a reservation recorded under the display pair.
pm3 = mk()
active3 = {cc._position_key("turtlesue", "XXLMZUSD")}   # exchange code
r3 = pm3.confirm("turtlesue", [], active_positions=active3)
swept3 = {s["reservation_id"] for s in r3["swept"]}
check("XXLMZUSD protects XLM/USD", "r2" not in swept3, f"swept {sorted(swept3)}")

print()
print("=== CASE 4: no position data falls back to legacy behaviour ===")
pm4 = mk()
r4 = pm4.confirm("turtlesue", [], active_positions=None)
check("None means no check", len(r4["swept"]) == 3, f"swept {len(r4['swept'])}")

print()
print("=== CASE 5: a correct declaration still refreshes leases ===")
pm5 = mk()
r5 = pm5.confirm("turtlesue", ["r1", "r2", "r3"], active_positions=set())
check("all three confirmed", r5["confirmed"] == 3, f"{r5['confirmed']}")
check("nothing swept", not r5["swept"], f"{len(r5['swept'])} swept")

print()
print("=== CASE 6: another bot's reservations are untouched ===")
pm6 = mk()
pm6.reservations["r9"] = {"bot_id": "confluence", "pair": "APE/USD",
                          "amount": 550.0, "reserved_at": time.time() - 1000,
                          "unconfirmed_since": time.time() - 1000}
pm6.confirm("turtlesue", [], active_positions=set())
check("foreign reservation survives", "r9" in pm6.reservations,
      "a confirm from one bot must never sweep another's capital")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — a declaration is not the only evidence a position is gone")
