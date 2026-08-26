"""A bot's minimum trade size must never exceed what the pool can grant.

THE INCIDENT (2026-08-26). TurtleSue carried a private hardcoded
CONFIG["min_trade_size_usd"] = 500.0, set on 2026-04-07 when the pool was
$1M. On 2026-08-13 the operator resized the pool to $210.53 and Command
Center moved its own MIN_TRADE_USD 100 -> 1 -> 0.25 to match. TurtleSue's
copy never moved.

At a $210.53 pool the per-trade cap is 20% = $42.11. A $500 FLOOR against a
$42 CEILING is unsatisfiable: _open_position computed a correct pool-scaled
unit and then rejected its own trade as "too small". The bot could not place
a single real trade, and its only rows on the expectancy ledger were ZZPROBE
test fixtures.

This is the "fix one sibling, miss the others" shape. NexusBrain and
Arbitrageur already imported the one fleet floor from command_center;
TurtleSue was the straggler. The test therefore checks EVERY trader, not
just the one that broke, so the next straggler is caught by the suite
instead of by a parked bot.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
sys.path.insert(0, CC)

import fleet_config  # noqa: E402
from command_center import MIN_TRADE_USD  # noqa: E402

failures = []
checked = 0

POOL = fleet_config.PORTFOLIO_TOTAL
LIMITS = fleet_config.PORTFOLIO_LIMITS
PER_TRADE_CAP = POOL * LIMITS["max_per_trade_pct"] / 100.0

# ---------------------------------------------------------------- invariant 1
# The fleet floor itself must be satisfiable against the fleet's own cap.
if MIN_TRADE_USD > PER_TRADE_CAP:
    failures.append(
        "FLEET FLOOR UNSATISFIABLE: MIN_TRADE_USD=%.2f > per-trade cap %.2f "
        "(%d%% of a $%.2f pool). No bot can place a legal trade."
        % (MIN_TRADE_USD, PER_TRADE_CAP, LIMITS["max_per_trade_pct"], POOL)
    )

# ---------------------------------------------------------------- invariant 2
# No trader may carry a private hardcoded floor above the pool's per-trade
# cap. A literal is allowed only if it is still satisfiable; importing the
# fleet floor is the preferred form and always passes.
FLOOR_KEYS = ("min_trade_size_usd", "min_size_usd", "min_notional_usd")
_NUM = re.compile(
    r"[\"'](%s)[\"']\s*:\s*([0-9]+(?:\.[0-9]+)?)" % "|".join(FLOOR_KEYS)
)

for bot_id, cfg in sorted(fleet_config.BOTS.items()):
    if cfg.get("role") != "trader":
        continue
    entry = cfg.get("cmd", [None, None])[1]
    if not entry:
        continue
    path = os.path.join(cfg["dir"], entry)
    if not os.path.exists(path):
        # Absent is not empty: report it rather than passing silently.
        failures.append("%s: entry point not found at %s" % (bot_id, path))
        continue
    checked += 1
    with open(path, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    for m in _NUM.finditer(src):
        key, raw = m.group(1), float(m.group(2))
        line = src[: m.start()].count("\n") + 1
        if raw > PER_TRADE_CAP:
            failures.append(
                "%s:%d hardcoded %s=%.2f exceeds the per-trade cap %.2f "
                "(%d%% of a $%.2f pool) — this bot cannot trade. Import "
                "MIN_TRADE_USD from command_center instead, as NexusBrain "
                "and Arbitrageur do."
                % (bot_id, line, key, raw, PER_TRADE_CAP,
                   LIMITS["max_per_trade_pct"], POOL)
            )

# ---------------------------------------------------------------- invariant 3
# TurtleSue specifically must resolve its floor to the fleet value, and the
# import must be bound before the CONFIG dict that reads it (a NameError
# there takes the whole bot down at import).
ts_dir = fleet_config.BOTS["turtlesue"]["dir"]
ts_path = os.path.join(ts_dir, "turtlebot.py")
if os.path.exists(ts_path):
    with open(ts_path, encoding="utf-8", errors="replace") as fh:
        ts_src = fh.read()
    m_def = re.search(r"^\s*_MIN_TRADE_USD\s*=", ts_src, re.M)
    m_imp = re.search(r"from\s+command_center\s+import\s+MIN_TRADE_USD", ts_src)
    m_cfg = re.search(r"^CONFIG\s*=", ts_src, re.M)
    if not m_imp:
        failures.append(
            "turtlesue: no `from command_center import MIN_TRADE_USD` — it is "
            "back to carrying a private floor copy."
        )
    if m_def and m_cfg and m_def.start() > m_cfg.start():
        failures.append(
            "turtlesue: _MIN_TRADE_USD is bound AFTER the CONFIG dict that "
            "reads it — NameError at import, bot dies on startup."
        )
else:
    failures.append("turtlesue: turtlebot.py not found at %s" % ts_path)

# --------------------------------------------------------------------- report
print("pool $%.2f  per-trade cap $%.2f  fleet MIN_TRADE_USD $%.2f"
      % (POOL, PER_TRADE_CAP, MIN_TRADE_USD))
print("traders checked: %d" % checked)

if checked == 0:
    # Measured nothing is not the same as measured and found nothing.
    print("FAIL: no trader entry points were read — the check proved nothing")
    sys.exit(1)

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: every trader's minimum trade size is satisfiable at the current pool")
