"""Position size must never be guessed from magnitude.

Size fields do not share units across the fleet: turtlesue.total_size is
COINS, every other trader's size_usd is DOLLARS. fleet_logger used to pick
between them with `_size * _entry if _size < 100 else _size` — a magnitude
threshold, which cannot recover units a producer never declared.

On live data that rule was wrong in BOTH directions, which is why a
one-sided test would have missed it.
"""
import os
import sys

sys.path.insert(0, r'D:\CommandCenter')

from portfolio_math import (notional_usd, UnknownBotError,
                            AmbiguousPositionError)

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def old_heuristic(size, entry):
    """The rule that was in fleet_logger.py:458."""
    return size * entry if size < 100 else size


print("=== CASE 1: the old heuristic was wrong in BOTH directions ===")
# Real TurtleSue positions, 2026-08-06.
uni = {"total_size": 446.9044092868955, "avg_entry": 4.2,
       "units": [{"size": 236.12301224588253, "entry_price": 4.09},
                 {"size": 210.78139704101297, "entry_price": 4.33}]}
xlm = {"total_size": 6158.321682212315, "avg_entry": 0.16,
       "units": [{"size": 6158.321682212315, "entry_price": 0.16}]}

uni_true = notional_usd("turtlesue", uni)
xlm_true = notional_usd("turtlesue", xlm)
uni_old = old_heuristic(446.9044092868955, 4.2)
xlm_old = old_heuristic(6158.321682212315, 0.16)

print(f"     UNI  true ${uni_true:>10,.2f}   old rule ${uni_old:>10,.2f}")
print(f"     XLM  true ${xlm_true:>10,.2f}   old rule ${xlm_old:>10,.2f}")
check("UNI notional is coins x price", abs(uni_true - 1878.43) < 1.0,
      f"${uni_true:,.2f}")
check("XLM notional is coins x price", abs(xlm_true - 985.33) < 1.0,
      f"${xlm_true:,.2f}")
check("old rule UNDER-stated UNI", uni_old < uni_true / 2,
      f"${uni_old:,.2f} vs ${uni_true:,.2f}")
check("old rule OVER-stated XLM", xlm_old > xlm_true * 2,
      f"${xlm_old:,.2f} vs ${xlm_true:,.2f}")

print()
print("=== CASE 2: USD-denominated bots pass through unchanged ===")
for bot in ("confluence", "gridzilla", "rubberband", "nexusbrain", "arbitrageur"):
    got = notional_usd(bot, {"size_usd": 550.0})
    check(f"{bot} size_usd is dollars", got == 550.0, f"${got}")

print()
print("=== CASE 3: an unregistered bot RAISES rather than guessing ===")
try:
    notional_usd("newbot", {"size_usd": 100})
    check("unknown bot raises", False, "returned a value instead")
except UnknownBotError:
    check("unknown bot raises", True)

print()
print("=== CASE 4: missing fields RAISE rather than defaulting ===")
try:
    notional_usd("turtlesue", {})
    check("missing fields raise", False, "returned a value instead")
except (AmbiguousPositionError, UnknownBotError):
    check("missing fields raise", True)

print()
print("=== CASE 5: the magnitude threshold has no safe value ===")
# A $50 position in a $0.10 coin is 500 coins (>100, reads as USD -> wrong).
# A 50-coin position in a $2,000 coin is $100,000 (<100, reads as coins -> right
# by luck). No single threshold separates the two, which is the whole point.
small_coins = {"total_size": 500.0, "avg_entry": 0.10,
               "units": [{"size": 500.0, "entry_price": 0.10}]}
true = notional_usd("turtlesue", small_coins)
old = old_heuristic(500.0, 0.10)
check("threshold fails on cheap coins", abs(true - 50.0) < 0.01 and old == 500.0,
      f"true ${true:,.2f} vs old ${old:,.2f}")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — notional comes from declared units, never from magnitude")
