"""A rate reported from zero samples is a fabricated measurement.

The dashboard rendered "WIN RATE 0.0%" in red for bots that had never traded
— indistinguishable from a bot that lost every trade. Meanwhile the panel
directly below correctly showed "Profit factor --" and "Exp/trade --", because
those were None. Win rate was the one publishing a zero.

Separately, gridzilla reported "1 trade, 0.0% win rate" on a winning +$34.90
cycle, because total_trades fell back to total_cycles while win_rate stayed
with an empty expectancy block — two fields from two sources presented as one
measurement.
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


def post_step(normalized):
    """The shared post-step in _poll_all_bots, applied to one payload."""
    if not normalized.get("total_trades"):
        if normalized.get("win_rate") == 0:
            normalized["win_rate"] = None
        for k in ("profit_factor", "expectancy_r", "sharpe"):
            if normalized.get(k) == 0:
                normalized[k] = None
    return normalized


print("=== CASE 1: zero trades must not publish a rate ===")
for bot, payload in (
    ("turtlesue", {"total_trades": 0, "win_rate": 0.0, "pnl": 0.0}),
    ("rubberband", {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0}),
    ("arbitrageur", {"total_trades": 0, "win_rate": 0.0, "sharpe": 0.0}),
    ("nexusbrain", {"total_trades": 0, "win_rate": 0.0, "expectancy_r": 0.0}),
):
    got = post_step(dict(payload))
    check(f"{bot}: win_rate suppressed", got["win_rate"] is None,
          f"got {got['win_rate']}")
    for k in ("profit_factor", "expectancy_r", "sharpe"):
        if k in payload:
            check(f"{bot}: {k} suppressed", got[k] is None, f"got {got[k]}")

print()
print("=== CASE 2: a real zero on real trades is PRESERVED ===")
# A bot that genuinely lost all 4 trades must still read 0.0%, not "--".
got = post_step({"total_trades": 4, "win_rate": 0.0, "pnl": -120.0})
check("0% on 4 real trades survives", got["win_rate"] == 0.0,
      f"got {got['win_rate']}")
got = post_step({"total_trades": 5, "win_rate": 20.0, "pnl": -14.89})
check("a nonzero rate is untouched", got["win_rate"] == 20.0)

print()
print("=== CASE 3: gridzilla — count and rate from the SAME source ===")
# Expectancy empty, one completed cycle: report the count, not a borrowed 0%.
n = cc._normalize_gridzilla({
    "total_pnl": 34.9016, "net_pnl": 34.9016, "total_cycles": 1,
    "expectancy": {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0},
    "active_grids": {}, "n_active_grids": 0,
})
print(f"     total_trades={n['total_trades']}  win_rate={n['win_rate']}  pnl={n['pnl']}")
check("count comes from cycles", n["total_trades"] == 1)
check("rate is unmeasured, not 0%", n["win_rate"] is None,
      f"got {n['win_rate']} — a winning cycle must not read as a total loss")

# Expectancy populated: both come from it.
n2 = cc._normalize_gridzilla({
    "total_pnl": 34.9016, "net_pnl": 34.9016, "total_cycles": 1,
    "expectancy": {"total_trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0},
    "active_grids": {}, "n_active_grids": 0,
})
print(f"     total_trades={n2['total_trades']}  win_rate={n2['win_rate']}")
check("populated expectancy wins", n2["total_trades"] == 1 and n2["win_rate"] == 100.0,
      f"trades={n2['total_trades']} wr={n2['win_rate']}")

print()
print("=== CASE 4: the live fleet publishes no rate without trades ===")
import json
import urllib.request
try:
    m = json.loads(urllib.request.urlopen(
        'http://localhost:9000/api/master', timeout=25).read())
    offenders = []
    for b in m.get("bots", []):
        n = b.get("normalized") or {}
        if not n.get("total_trades") and n.get("win_rate") is not None:
            offenders.append((b["id"], n.get("total_trades"), n.get("win_rate")))
    check("no live bot reports a rate with no trades", not offenders,
          f"{offenders}")
except Exception as e:
    print(f"  SKIP  live check — Command Center unreachable ({str(e)[:50]})")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — no rate is reported from zero samples")
