#!/usr/bin/env python3
"""GridExecutor.check_fills -- the live order path.

This path cannot be exercised by running the bot: it needs a funded pool from
CommandCenter and an armed live gate, and both are deliberately off. So it is
driven directly here with a stubbed Kraken client.

The one that matters is the NEGATIVE CONTROL: when the exchange REJECTS the
order, the level must stay unfilled. It used to log "using paper fill" and
book it anyway, which would report realised P/L on trades that never happened.

Run: python test_fills.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\CommandCenter")

import fleet_config as fc          # noqa: E402
import gridzilla as G              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


class StubKraken:
    """Stands in for KrakenSpotClient. Records calls; never touches a network."""

    def __init__(self, ok=True, txid="TXID-1"):
        self.ok, self.txid = ok, txid
        self.calls = []

    def buy(self, pair, qty, price=None):
        self.calls.append(("buy", pair, qty, price))
        return (self.ok, self.txid if self.ok else "EOrder:Insufficient funds")

    def sell(self, pair, qty, price=None):
        self.calls.append(("sell", pair, qty, price))
        return (self.ok, self.txid if self.ok else "EOrder:Insufficient funds")

    def get_fill_price(self, txid, fallback):
        return fallback


def make_executor(spot=None):
    ex = G.GridExecutor(G.CONFIG, kraken=None, publisher=None, expectancy=None)
    ex._kraken_spot = spot
    ex.active_grids["TEST/USD"] = {
        "status": "ACTIVE",
        "fills": [],
        "levels": [{
            "price": 100.0, "side": "BUY", "size_usd": 50.0,
            "size_base": 0.5, "distance_pct": -1.0, "edge_factor": 1.0,
            "expected_profit": 0.5, "fees": 0.0,
            "filled": False, "fill_price": 0, "fill_time": 0,
            "take_profit": 102.0,
        }],
    }
    return ex


def only_level(ex):
    return ex.active_grids["TEST/USD"]["levels"][0]


print("\npaper path -- no Kraken client at all")
fc.FLEET_MODE, fc.FLEET_ENGAGE_STATE = "paper", "paper"
fc.LIVE_ARMED_BOTS.clear()
ex = make_executor(spot=None)
fills = ex.check_fills("TEST/USD", 99.0)          # price below the BUY level
check("a paper fill is booked when price crosses", len(fills) == 1, f"{len(fills)} fill(s)")
check("the level is marked filled", only_level(ex)["filled"])

print("\nlive path -- order ACCEPTED (positive control)")
fc.FLEET_MODE, fc.FLEET_ENGAGE_STATE = "live", "live_engaged"
fc.LIVE_ARMED_BOTS.add("gridzilla")
stub = StubKraken(ok=True)
ex = make_executor(spot=stub)
fills = ex.check_fills("TEST/USD", 99.0)
check("the exchange was actually called", len(stub.calls) == 1, str(stub.calls[:1]))
check("an accepted order books a fill", len(fills) == 1)
check("the level is marked filled", only_level(ex)["filled"])

print("\nlive path -- order REJECTED (THE negative control)")
stub = StubKraken(ok=False)
ex = make_executor(spot=stub)
fills = ex.check_fills("TEST/USD", 99.0)
check("the exchange was called", len(stub.calls) == 1)
check("NEGATIVE CONTROL: a rejected order books NO fill", fills == [], f"{fills}")
check("NEGATIVE CONTROL: the level stays UNFILLED", not only_level(ex)["filled"],
      "a refused order must not become realised P/L")
check("no phantom P/L was recorded", ex.total_pnl == 0, f"total_pnl={ex.total_pnl}")

print("\nthe rejected level is retried, not abandoned")
stub2 = StubKraken(ok=True)
ex._kraken_spot = stub2
fills2 = ex.check_fills("TEST/USD", 99.0)
check("a later successful attempt fills it", len(fills2) == 1)
check("and now the level is filled", only_level(ex)["filled"])

print("\narming gate is consulted AT TRADE TIME, not just at startup")
stub3 = StubKraken(ok=True)
ex = make_executor(spot=stub3)
fc.LIVE_ARMED_BOTS.clear()                 # disarm AFTER the executor was built
fills3 = ex.check_fills("TEST/USD", 99.0)
check("NEGATIVE CONTROL: disarming mid-run stops real orders",
      len(stub3.calls) == 0, "executor held a live client but the gate refused")
check("it still books a paper fill (the grid keeps working)", len(fills3) == 1)

fc.FLEET_MODE, fc.FLEET_ENGAGE_STATE = "paper", "paper"
fc.LIVE_ARMED_BOTS.clear()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1)
print("all green")
