"""Gridzilla must be able to record a losing trade.

THE INCIDENT (2026-08-26). Gridzilla stood at 16W / 0L / 17F — a win rate of
100.0% that was structurally unable to fall.

A grid cycle only completes when a sell fires at `current_price >=
level["price"]` against a buy filled at a strictly lower level, so every
realized cycle P/L is >= 0 by construction. `check_fills` was the ONLY caller
of `expectancy.record_trade`, so the expectancy ledger could never receive a
losing trade. Measured on the durable bus at the time:

    GRID_KILLED events : 167
      negative P/L     :   0
      positive P/L     :  13
      zero P/L         : 154

167 grids torn down, not one loss booked. The loss in a grid is not a cycle —
it is open inventory bought on the way down and abandoned when price leaves
the range. 155 of those teardowns had zero completed cycles and reported
pnl 0, every one of them reason "range_break".

This test asserts the teardown path reaches the expectancy tracker, and that
the double-count guard protecting already-recorded cycles still holds.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
GZ = os.path.join(os.path.dirname(CC), "Gridzilla", "gridzilla.py")

failures = []

if not os.path.exists(GZ):
    print("FAIL: gridzilla.py not found at %s" % GZ)
    sys.exit(1)

with open(GZ, encoding="utf-8", errors="replace") as fh:
    src = fh.read()

# Isolate remove_grid's body: from its def to the next def at the same indent.
m = re.search(r"\n(    )def remove_grid\(self, pair\):\n(.*?)(?=\n    def )",
              src, re.S)
if not m:
    print("FAIL: could not locate remove_grid in gridzilla.py")
    sys.exit(1)
body = m.group(2)

# ---------------------------------------------------------------- invariant 1
# The teardown path must feed the expectancy tracker at all. This is the
# single line whose absence created the unreachable 100%.
if "record_trade" not in body:
    failures.append(
        "remove_grid never calls expectancy.record_trade — losing grids are "
        "invisible to expectancy and the win rate cannot fall below 100%."
    )

# ---------------------------------------------------------------- invariant 2
# It must record the mark-to-market figure, not grid_pnl. grid_pnl is the sum
# of COMPLETED cycles and is monotonically non-decreasing, so recording it
# would reintroduce the exact bug.
if "total_pnl_mtm" not in body:
    failures.append(
        "remove_grid does not reference total_pnl_mtm — recording grid_pnl "
        "alone can only ever be >= 0 and reintroduces the unreachable 100%."
    )

# ---------------------------------------------------------------- invariant 3
# The double-count guard must survive. Cycles record themselves in
# check_fills; a teardown that recorded the full mark-to-market for a grid
# WITH completed cycles would count that P/L twice.
if "cycles_completed" not in body:
    failures.append(
        "remove_grid lost its cycles_completed guard — completed cycles are "
        "recorded by check_fills and would now be double-counted."
    )

# ---------------------------------------------------------------- invariant 4
# Prices must be None, never 0.0. A recorded 0.0 lands in the durable store
# and is indistinguishable from a real reading. (The file already carries a
# scar about exactly this: a live ETH/USD row with entry 0.0 / exit 0.0.)
if re.search(r"entry_price\s*=\s*0(\.0)?\b", body) or \
   re.search(r"exit_price\s*=\s*0(\.0)?\b", body):
    failures.append(
        "remove_grid records a price of 0.0 — an unmeasured price must be "
        "None so the durable store cannot mistake it for a real reading."
    )

# ---------------------------------------------------------------- invariant 5
# A clean teardown with nothing unclaimed must not be recorded: padding the
# denominator with non-trades is its own dishonesty.
if not re.search(r"_record_pnl\s*!=\s*0", body):
    failures.append(
        "remove_grid does not gate on a non-zero unclaimed P/L — a clean "
        "teardown is not a trade and must not pad the denominator."
    )

# --------------------------------------------------------------------- report
print("checked remove_grid in %s" % GZ)

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: gridzilla's teardown path records losses and preserves the "
      "double-count guard")
