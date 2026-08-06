"""The stale-reservation sweep must never release capital under a live position.

The guard matches (bot, pair) keys built from two different sources: what a bot
reports as an open position, and what the reservation says. If those are derived
differently, the sweep force-releases a live trade's capital and the bot has a
position with no reservation behind it.

This has now bitten twice — Confluence's 66h swings (2026-07-30, fixed by adding
the guard) and TurtleSue's XLM short (2026-08-06, the guard itself mismatched on
Kraken pair codes). Hence a test.
"""
import os
import sys

sys.path.insert(0, r'D:\CommandCenter')

import command_center as cc

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("=== CASE 1: both sides of the key agree across pair formats ===")
# Left: how a bot names the pair. Right: how the reservation names it.
for bot_fmt, res_fmt, label in (
    ("XXLMZUSD", "XLM/USD", "Kraken XLM code"),
    ("XXBTZUSD", "BTC/USD", "Kraken BTC code"),
    ("XETHZUSD", "ETH/USD", "Kraken ETH code"),
    ("UNIUSD", "UNI/USD", "no-prefix pair"),
    ("APE/USD", "APE/USD", "already canonical"),
):
    a = cc._position_key("turtlesue", bot_fmt)
    b = cc._position_key("turtlesue", res_fmt)
    check(f"{label}: {bot_fmt} == {res_fmt}", a == b, f"{a} vs {b}")

print()
print("=== CASE 2: distinct pairs still produce distinct keys ===")
check("UNI != XLM", cc._position_key("t", "UNI/USD") != cc._position_key("t", "XLM/USD"))
check("different bots differ",
      cc._position_key("turtlesue", "UNI/USD") != cc._position_key("confluence", "UNI/USD"))

print()
print("=== CASE 3: dict-keyed positions (TurtleSue, Rubberband) are protected ===")
bots = {
    "turtlesue": {
        "alive": True,
        "raw": {"positions": {
            "XXLMZUSD": {"name": "XLM/USD", "direction": "SHORT"},
            "UNIUSD": {"name": "UNI/USD", "direction": "LONG"},
        }},
    },
}
keys = cc._active_position_keys(bots)
check("XLM short protected", cc._position_key("turtlesue", "XLM/USD") in keys,
      f"keys={sorted(keys)}")
check("UNI long protected", cc._position_key("turtlesue", "UNI/USD") in keys)

print()
print("=== CASE 4: list-shaped positions (Confluence, Gridzilla) are protected ===")
bots = {
    "confluence": {
        "alive": True,
        "raw": {"positions": [
            {"pair": "APE/USD"}, {"pair": "ETHFI/USD"}, {"symbol": "PEPE/USD"},
        ]},
    },
}
keys = cc._active_position_keys(bots)
for p in ("APE/USD", "ETHFI/USD", "PEPE/USD"):
    check(f"{p} protected", cc._position_key("confluence", p) in keys)

print()
print("=== CASE 4b: Gridzilla holds capital as active_grids, not positions ===")
# Its 4 grids were the largest holdings in the pool ($170,134 on 2026-08-06)
# and matched no key at all — the sweep would have released every one.
bots = {
    "gridzilla": {
        "alive": True,
        "raw": {"active_grids": {
            "AVAX/USD": {"pair": "AVAX/USD", "status": "active"},
            "ATOM/USD": {"pair": "ATOM/USD", "status": "active"},
            "ETH/USD": {"pair": "ETH/USD", "status": "active"},
            "LINK/USD": {"pair": "LINK/USD", "status": "active"},
        }},
    },
}
keys = cc._active_position_keys(bots)
for p in ("AVAX/USD", "ATOM/USD", "ETH/USD", "LINK/USD"):
    check(f"grid {p} protected", cc._position_key("gridzilla", p) in keys)

print()
print("=== CASE 4c: an int 'open_positions' count must not crash or match ===")
bots = {"x": {"alive": True, "raw": {"open_positions": 3, "positions": []}}}
try:
    keys = cc._active_position_keys(bots)
    check("int open_positions handled", keys == set(), f"keys={keys}")
except Exception as e:
    check("int open_positions handled", False, f"raised {type(e).__name__}: {e}")

print()
print("=== CASE 4d: falls back to `normalized` when `raw` is absent ===")
# /api/master strips `raw`, so a caller reading the API rather than _state
# would otherwise build an empty set and protect nothing.
bots = {"turtlesue": {"alive": True,
                      "normalized": {"positions": [{"pair": "UNI/USD"}]}}}
keys = cc._active_position_keys(bots)
check("normalized path works", cc._position_key("turtlesue", "UNI/USD") in keys,
      f"keys={sorted(keys)}")

print()
print("=== CASE 5: dead bots do not protect anything ===")
bots = {"turtlesue": {"alive": False,
                      "raw": {"positions": {"XXLMZUSD": {"name": "XLM/USD"}}}}}
check("offline bot contributes no keys", not cc._active_position_keys(bots))

print()
print("=== CASE 6: end-to-end — a live position survives the sweep ===")
pm = cc.PortfolioManager.__new__(cc.PortfolioManager)
import threading
import time
pm._lock = threading.RLock()
pm.history = []
pm._save = lambda: None
old = time.time() - 72 * 3600          # 72h — well past the 48h cutoff
pm.reservations = {
    "live_xlm": {"bot_id": "turtlesue", "pair": "XLM/USD", "amount": 786.03,
                 "direction": "SHORT", "reserved_at": old},
    "orphan":   {"bot_id": "turtlesue", "pair": "DOT/USD", "amount": 500.0,
                 "direction": "LONG", "reserved_at": old},
}
# Bot reports ONLY the XLM position, under its Kraken key.
active = cc._active_position_keys({
    "turtlesue": {"alive": True,
                  "raw": {"positions": {"XXLMZUSD": {"name": "XLM/USD"}}}},
})
released = pm.force_release_stale(max_age_hours=48, active_positions=active)
rids = {r["reservation_id"] for r in released}
check("live XLM reservation SURVIVES", "live_xlm" not in rids,
      f"released={sorted(rids)}")
check("genuine orphan IS released", "orphan" in rids, f"released={sorted(rids)}")
check("live reservation still held", "live_xlm" in pm.reservations)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — the sweep cannot release capital under a reported position")
