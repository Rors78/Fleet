"""Unreadable state must not look like a first run.

Absent, empty, and corrupt all produced the same empty self.positions. That
matters because _reconcile_positions releases every reservation whose id no
local position references — with an empty position dict that is ALL of them,
while the positions stay open on the venue with no capital behind them.

Verified before the fix: a truncated turtle_positions.json loaded as 0
positions, identical to a genuine first run, differing only by a log line.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, r'D:\TurtleSue')
sys.path.insert(0, r'D:\CommandCenter')

import turtlebot as tb

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def load_from(path):
    e = tb.TurtleEngine.__new__(tb.TurtleEngine)
    e.positions = {}
    e.errors = []
    e._positions_file = path
    e._load_positions()
    return e


GOOD = json.dumps({
    "positions": {
        "UNIUSD": {"pair": "UNIUSD", "direction": "LONG", "system": 1,
                   "entry_n": 0.21, "opened_at": 1785409992,
                   "reservation_ids": ["turtlesue_UNI/USD_1_a"],
                   "units": [{"entry_price": 4.09, "size": 236.12, "n": 0.21,
                              "timestamp": 1785409992}]},
    },
    "saved_at": 1785989186,
})

tmp = tempfile.mkdtemp()
try:
    p_good = os.path.join(tmp, "good.json")
    open(p_good, "w").write(GOOD)
    p_empty = os.path.join(tmp, "empty.json")
    open(p_empty, "w").write('{"positions":{},"saved_at":0}')
    p_absent = os.path.join(tmp, "nope.json")
    p_bad = os.path.join(tmp, "bad.json")
    open(p_bad, "w").write(GOOD[:len(GOOD) // 2])   # truncated mid-write

    print("=== CASE 1: the three cases are now distinguishable ===")
    e_good = load_from(p_good)
    e_empty = load_from(p_empty)
    e_absent = load_from(p_absent)
    e_bad = load_from(p_bad)

    check("good file restores positions", len(e_good.positions) == 1,
          f"{len(e_good.positions)}")
    check("good file is not flagged", e_good._state_unreadable is False)
    check("empty file: 0 positions, NOT flagged",
          not e_empty.positions and e_empty._state_unreadable is False)
    check("absent file: 0 positions, NOT flagged",
          not e_absent.positions and e_absent._state_unreadable is False)
    check("CORRUPT file IS flagged", e_bad._state_unreadable is True,
          "this is the whole point — it used to be identical to a first run")

    print()
    print("=== CASE 2: the orphan sweep refuses to run on unknown state ===")
    released = []

    class StubPortfolio:
        bot_id = "turtlesue"

        def get_reservations(self):
            # The pool holds two reservations this bot really owns.
            return {"turtlesue_UNI/USD_1_a": {"bot_id": "turtlesue"},
                    "turtlesue_XLM/USD_1_b": {"bot_id": "turtlesue"}}

        def release(self, rid, pnl=0.0):
            released.append(rid)
            return True, "ok"

        def reserve(self, *a, **kw):
            return True, "new_rid"

    e_bad._portfolio_client = StubPortfolio()
    e_bad._reconcile_positions()
    check("corrupt state releases NOTHING", released == [],
          f"released {released} — each one strands a live position")

    print()
    print("=== CASE 3: a genuinely empty state still sweeps real orphans ===")
    released.clear()
    e_empty._portfolio_client = StubPortfolio()
    e_empty._reconcile_positions()
    check("empty state still sweeps", len(released) == 2,
          f"released {released} — real orphans must still be reclaimed")

    print()
    print("=== CASE 4: entries are blocked while state is unknown ===")
    e_bad.errors = []
    allowed = e_bad._can_add_unit("BTCUSD", "LONG", "BTC")
    check("no new units on unknown state", allowed is False)
    check("and it says why", any("unreadable" in x for x in e_bad.errors),
          f"errors={e_bad.errors}")

    e_good.errors = []
    e_good._portfolio_client = None
    ok_good = e_good._can_add_unit("BTCUSD", "LONG", "BTC")
    check("healthy state still allows entries", ok_good is True)

    print()
    print("=== CASE 5: the unreadable bytes are quarantined, not overwritten ===")
    quarantined = [f for f in os.listdir(tmp) if ".corrupt_" in f]
    check("corrupt file preserved for diagnosis", len(quarantined) == 1,
          f"{quarantined}")
    check("original left intact", os.path.exists(p_bad))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — unreadable state fails toward armed, not toward 'first run'")
