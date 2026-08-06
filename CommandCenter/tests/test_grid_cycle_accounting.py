"""A completed grid cycle must reach trade history and expectancy.

The dashboard showed Net P&L +$1,414.54 next to Wins/Losses 0/0 and a 0.0%
win rate — a winning trade counted as zero wins. Two independent gaps:

  1. trade_history was only appended in remove_grid() (teardown), but a
     completed cycle RESETS the grid and keeps it running, so a grid could
     cycle profitably for days and never appear.
  2. The snapshot's expectancy block came from ExpectancyTracker.get_bot_stats(),
     and nothing ever called record_trade() — queried, never fed.
"""
import os
import sys
import time

sys.path.insert(0, r'D:\Gridzilla')
sys.path.insert(0, r'D:\CommandCenter')

import gridzilla as gz

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class StubTracker:
    def __init__(self):
        self.recorded = []

    def record_trade(self, **kw):
        self.recorded.append(kw)

    def get_bot_stats(self, bot_id, last_n=None):
        wins = [t for t in self.recorded if (t.get("realized_pnl") or 0) > 0]
        return {"total_trades": len(self.recorded), "wins": len(wins),
                "win_rate": (len(wins) / len(self.recorded) * 100)
                if self.recorded else 0}


def make_executor():
    tracker = StubTracker()
    ex = gz.GridExecutor.__new__(gz.GridExecutor)
    ex.config = {"name": "Gridzilla"}
    ex.kraken = None
    ex.publisher = None
    ex.expectancy = tracker
    ex.active_grids = {}
    ex.trade_history = []
    ex.total_pnl = 0
    ex.total_fees = 0
    ex.total_cycles = 0
    ex._kraken_spot = None   # paper mode — no live venue
    import threading
    ex.lock = threading.RLock()
    return ex, tracker


print("=== CASE 1: a completed cycle is recorded where P/L is realized ===")
ex, tracker = make_executor()
# Minimal grid with one filled BUY level ready to be closed by a SELL.
# Shape matters: check_fills matches a SELL level against an entry in
# grid["fills"] carrying take_profit == that level's price. A `levels` entry
# alone never closes anything.
grid = {
    "pair": "ETH/USD", "status": "ACTIVE", "deployed_at": time.time() - 3600,
    "cycles_completed": 0, "grid_pnl": 0.0, "peak_pnl": 0.0, "max_drawdown": 0.0,
    "reservation_id": "gridzilla_ETH_1",
    "fills": [
        {"pair": "ETH/USD", "side": "BUY", "price": 1868.37,
         "size_usd": 10000.0, "fee": 0.0, "pnl": 0.0,
         "take_profit": 1906.98, "closed": False,
         "time": time.time() - 1800},
    ],
    "levels": [
        {"price": 1868.37, "size_usd": 10000.0, "side": "BUY", "filled": True,
         "closed": False, "fill_time": time.time() - 1800},
        {"price": 1906.98, "size_usd": 10000.0, "side": "SELL", "filled": False,
         "closed": False, "fill_time": 0},
    ],
}
# Buy $10,000 at 1868.37, sell at 1906.98 -> 10000*(1906.98/1868.37)-10000.
EXPECTED_PNL = 10000.0 * (1906.98 / 1868.37) - 10000.0
ex.active_grids["ETH/USD"] = grid

# Drive the real fill path: price at/above the SELL level closes the cycle.
fills = ex.check_fills("ETH/USD", 1906.98)

check("a cycle completed", grid["cycles_completed"] == 1,
      f"cycles_completed={grid['cycles_completed']}")
check("total_cycles incremented", ex.total_cycles == 1, f"{ex.total_cycles}")
check("trade_history got the cycle", len(ex.trade_history) == 1,
      f"{len(ex.trade_history)} entries")
if ex.trade_history:
    t = ex.trade_history[0]
    print(f"     recorded: pnl={t.get('pnl')} won={t.get('won')} status={t.get('status')}")
    check("cycle P/L is positive", (t.get("pnl") or 0) > 0, f"pnl={t.get('pnl')}")
    check("cycle marked won", t.get("won") is True)
    check("P/L is the buy->sell gain on the BUY size",
          abs((t.get("pnl") or 0) - EXPECTED_PNL) < 0.01,
          f"got {t.get('pnl')}, expected {EXPECTED_PNL:.4f}")
check("expectancy tracker was fed", len(tracker.recorded) == 1,
      f"{len(tracker.recorded)} records")

print()
print("=== CASE 2: the stats block now reflects the win ===")
stats = tracker.get_bot_stats("gridzilla")
print(f"     {stats}")
check("total_trades is 1", stats["total_trades"] == 1)
check("win_rate is 100%, not 0%", stats["win_rate"] == 100.0,
      f"{stats['win_rate']}%")

print()
print("=== CASE 3: teardown must not double-count recorded cycles ===")
before = len(ex.trade_history)
summary = ex.remove_grid("ETH/USD")
check("teardown adds no duplicate", len(ex.trade_history) == before,
      f"{before} -> {len(ex.trade_history)}")
check("teardown still returns a summary", summary is not None)

print()
print("=== CASE 4: a grid torn down with ZERO cycles is still recorded ===")
ex2, tracker2 = make_executor()
ex2.active_grids["ATOM/USD"] = {
    "pair": "ATOM/USD", "status": "RANGE_BREAK", "deployed_at": time.time() - 60,
    "cycles_completed": 0, "grid_pnl": -12.5, "fills": [],
    "levels": [], "reservation_id": "gridzilla_ATOM_1",
}
ex2.remove_grid("ATOM/USD")
check("zero-cycle teardown recorded", len(ex2.trade_history) == 1,
      f"{len(ex2.trade_history)} entries")
if ex2.trade_history:
    check("recorded as a loss", ex2.trade_history[0].get("won") is False)

print()
print("=== CASE 5: the live ETH cycle that recorded a 40x overstatement ===")
# Real state from gridzilla_state.json, 2026-08-06: BUY $9,895.80 @ 1894.01,
# SELL level priced 1900.545 but sized $11,309.48, filled at 1900.69.
# Old formula: 11309.48*(1900.69/1900.545) - 9895.80 = $1,414.54
# True gain:    9895.80*(1900.69/1894.01) - 9895.80 = $34.90
ex5, tracker5 = make_executor()
ex5.active_grids["ETH/USD"] = {
    "pair": "ETH/USD", "status": "ACTIVE", "deployed_at": time.time() - 3600,
    "cycles_completed": 0, "grid_pnl": 0.0, "peak_pnl": 0.0, "max_drawdown": 0.0,
    "reservation_id": "gridzilla_ETH_live",
    "fills": [
        {"pair": "ETH/USD", "side": "BUY", "price": 1894.01,
         "size_usd": 9895.80, "fee": 0.0, "pnl": 0.0,
         "take_profit": 1900.545, "closed": False, "time": time.time() - 900},
    ],
    "levels": [
        {"price": 1894.11, "size_usd": 9895.80, "side": "BUY", "filled": True,
         "closed": False, "fill_time": time.time() - 900},
        # Note the SELL level is sized DIFFERENTLY from the buy — that gap is
        # what the old formula booked as profit.
        {"price": 1900.545, "size_usd": 11309.48, "side": "SELL",
         "filled": False, "closed": False, "fill_time": 0},
    ],
}
ex5.check_fills("ETH/USD", 1900.69)
true_gain = 9895.80 * (1900.69 / 1894.01) - 9895.80
old_wrong = 11309.48 * (1900.69 / 1900.545) - 9895.80
got = ex5.trade_history[0]["pnl"] if ex5.trade_history else None
print(f"     recorded ${got}   true ${true_gain:.2f}   old formula ${old_wrong:.2f}")
check("live cycle prices the buy size, not the level size",
      got is not None and abs(got - true_gain) < 0.05,
      f"got {got}, true {true_gain:.2f}")
check("no longer reports the 40x figure",
      got is not None and abs(got - old_wrong) > 1000.0,
      f"old formula gave {old_wrong:.2f}")

print()
print("=== CASE 6: a sell landing exactly on its level still scores the gain ===")
# The old formula returned $0.00 here no matter how far price rose from the buy.
ex6, tracker6 = make_executor()
ex6.active_grids["AVAX/USD"] = {
    "pair": "AVAX/USD", "status": "ACTIVE", "deployed_at": time.time() - 600,
    "cycles_completed": 0, "grid_pnl": 0.0, "peak_pnl": 0.0, "max_drawdown": 0.0,
    "reservation_id": "gridzilla_AVAX_1", "fills": [
        {"pair": "AVAX/USD", "side": "BUY", "price": 6.00, "size_usd": 6000.0,
         "fee": 0.0, "pnl": 0.0, "take_profit": 6.60, "closed": False,
         "time": time.time() - 300}],
    "levels": [
        {"price": 6.00, "size_usd": 6000.0, "side": "BUY", "filled": True,
         "closed": False, "fill_time": time.time() - 300},
        {"price": 6.60, "size_usd": 6000.0, "side": "SELL", "filled": False,
         "closed": False, "fill_time": 0}],
}
ex6.check_fills("AVAX/USD", 6.60)          # exactly on the level
got6 = ex6.trade_history[0]["pnl"] if ex6.trade_history else None
expected6 = 6000.0 * (6.60 / 6.00) - 6000.0   # $600
print(f"     recorded ${got6}   expected ${expected6:.2f}   old formula $0.00")
check("exact-level sell scores the real gain",
      got6 is not None and abs(got6 - expected6) < 0.01,
      f"got {got6}, expected {expected6:.2f}")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — completed cycles reach both trade history and expectancy, "
      "priced on the buy size")
