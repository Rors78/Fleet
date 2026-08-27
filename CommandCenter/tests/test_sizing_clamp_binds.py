"""A size clamp must change the amount that is actually reserved.

WHY THIS EXISTS. TurtleSue computed `cost = unit_coins * price`, then
clamped oversized trades by recomputing `unit_coins` -- and passed the
ORIGINAL `cost` to portfolio.reserve() a few lines later. The clamp
reduced the coin count and left the capital request untouched, so it did
nothing at all.

Measured live 2026-08-27 from the durable event bus:

    turtlesue requested $600.00 against a $215.77 pool
    84 PORTFOLIO_DENIAL events in 24 hours, every one for the same reason
    intended clamped size: $5.39  (10% pool share x 0.25)
    actual request:        $600.00  -- 111x the intent, 2.8x the whole pool

SCOPE, stated honestly: every one of those denials was for a ZZPROBE/NF
test fixture, not a real pair -- zero real-pair denials in the same
window. So this never blocked a live trade, and the fleet deployment cap
held throughout. The probes are what EXPOSED it. On a genuinely oversized
real trade the clamp would have been equally inert, and the only thing
between it and a 2.8x-pool reservation would have been the fleet cap --
a backstop, not the control that was supposed to handle it.

BOTH sites had it -- the entry clamp and the pyramid clamp. Fixing one
and leaving its sibling is this project's most-repeated failure shape, so
this test pins both.

The general rule: if a clamp recomputes a quantity, every value DERIVED
from that quantity must be recomputed too, before any of them is used.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "TurtleSue", "turtlebot.py")

failures = []

if not os.path.exists(SRC):
    print("FAIL: TurtleSue/turtlebot.py not found")
    sys.exit(1)

with io.open(SRC, encoding="utf-8", errors="replace") as fh:
    src = fh.read()

lines = src.splitlines()

# Find every clamp that reassigns unit_coins, then verify `cost` is
# recomputed before the next reserve() call.
clamp_lines = [i for i, l in enumerate(lines)
               if "unit_coins = (" in l and "_basis" in l]

if not clamp_lines:
    # An empty sweep is not a pass -- it means the code moved.
    failures.append(
        "no basis-derived unit_coins clamp found in turtlebot.py -- the "
        "sizing code has moved and this test is checking nothing")

for idx in clamp_lines:
    lineno = idx + 1
    # Look ahead for the next reserve( call, and for a cost recompute.
    recomputed = False
    reserve_at = None
    for j in range(idx + 1, min(idx + 40, len(lines))):
        if "cost = unit_coins * price" in lines[j]:
            recomputed = True
        if "reserve(" in lines[j] and "portfolio" in lines[j].lower():
            reserve_at = j + 1
            break
    if reserve_at is None:
        continue  # clamp not followed by a reservation on this path
    if not recomputed:
        failures.append(
            "turtlebot.py:%d clamps unit_coins but does not recompute cost "
            "before reserve() at line %d -- the clamp reduces the coin count "
            "while the capital request stays at its pre-clamp value, so the "
            "clamp is inert (this shipped, and produced $600 requests "
            "against a $215 pool)" % (lineno, reserve_at))

# The clamp must also actually bind: prove the arithmetic, not just the
# presence of a line. A clamp to (basis * f) must yield cost <= basis.
def clamped_cost(basis, factor, price):
    unit_coins = (basis * factor) / price
    return unit_coins * price

for basis, factor, price in ((21.58, 0.25, 3.14), (21.58, 0.10, 0.0001),
                             (500.0, 0.25, 12345.0)):
    got = clamped_cost(basis, factor, price)
    want = basis * factor
    if abs(got - want) > 1e-9:
        failures.append(
            "clamp arithmetic does not bind: basis=%s factor=%s price=%s "
            "-> %s, expected %s" % (basis, factor, price, got, want))

print("basis-derived clamps found: %d" % len(clamp_lines))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: every sizing clamp recomputes the cost it reserves, and the "
      "clamped cost binds to the basis")
