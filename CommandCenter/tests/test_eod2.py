"""Does the EOD renderer actually differentiate paid vs free when there ARE trades?

With 0 trades both tiers rendered byte-identical. That is expected only if
the tier branch lives in a section that empty data skips. Prove it with data.
"""
import sys, os, hashlib
sys.path.insert(0, r'D:\CommandCenter')
import os as _os
_OUT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'out')
_os.makedirs(_OUT, exist_ok=True)
from card_renderer import CardRenderer

data = {
    "date": "06 Aug 2026",
    "timestamp": "23:59 UTC",
    "source": "test",
    "fleet_expectancy_lifetime": -6.11,
    "trades": [
        {"bot": "confluence", "pair": "BTC/USDT", "side": "LONG", "net": 42.10,
         "hour": 9, "exit_reason": "take_profit", "gross_pnl": 42.10, "fees": 0},
        {"bot": "turtlesue", "pair": "ETH/USDT", "side": "SHORT", "net": -18.30,
         "hour": 14, "exit_reason": "stop_loss", "gross_pnl": -18.30, "fees": 0},
        # Use REAL fleet_config bot ids only. An invented id ("stalker") silently
        # falls through display_name() to .title() and can collide with a real
        # bot's public alias — which looked like a misattribution bug but was
        # entirely a bad fixture.
        {"bot": "gridzilla", "pair": "SOL/USDT", "side": "LONG", "net": 7.75,
         "hour": 20, "exit_reason": "take_profit", "gross_pnl": 7.75, "fees": 0},
    ],
}
data["gross"] = sum(t["gross_pnl"] for t in data["trades"])
data["fees"] = 0.0
data["net"] = data["gross"]

r = CardRenderer()
shas = {}
for tier in ("paid", "free"):
    png = r.render_end_of_day(data, tier=tier)
    dest = os.path.join(_OUT, f'eod2_{tier}.png')
    open(dest, 'wb').write(png)
    shas[tier] = hashlib.sha256(png).hexdigest()[:16]
    print(f"{tier}: {len(png)} bytes  sha={shas[tier]}  -> {dest}")

fails = []
if shas["paid"] == shas["free"]:
    fails.append("paid and free are identical WITH trades — tier branch is dead")

# Two DIFFERENT live bots must never render as the same public name — that
# would attribute one bot's P/L to another on a published card. Alias spellings
# of the same bot (hivemind/hive_mind) are fine; only real fleet ids count.
import fleet_config
from card_renderer import display_name, BOT_DISPLAY_NAMES as REND
from signal_broadcaster import BOT_DISPLAY_NAMES as BCAST
from collections import defaultdict

seen = defaultdict(list)
for bid in fleet_config.BOTS:
    seen[display_name(bid)].append(bid)
for name, ids in sorted(seen.items()):
    if len(ids) > 1:
        fails.append(f"display-name collision: {ids} all render as {name!r}")
print(f"display names unique across {len(fleet_config.BOTS)} fleet bots: "
      f"{all(len(v) == 1 for v in seen.values())}")

# The two maps are duplicated in separate modules; drift means a bot is named
# one thing on an image card and another in text.
if REND != BCAST:
    diff = {k for k in set(REND) | set(BCAST) if REND.get(k) != BCAST.get(k)}
    fails.append(f"renderer/broadcaster name maps disagree on: {sorted(diff)}")
print(f"renderer and broadcaster name maps agree: {REND == BCAST}")

print()
if fails:
    for f in fails:
        print("  FAIL:", f)
    raise SystemExit(1)
print("TIERS DIFFER with trades; names unique and consistent")
