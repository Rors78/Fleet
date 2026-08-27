"""The same close stored twice must collapse -- and only that.

WHY THIS EXISTS. A close reaches the durable store by two routes: the
portfolio release and the bus TRADE_CLOSE handler. record_trade dedups on
trade_id, which only works if both routes produce the SAME key. Confluence
emitted a rich TRADE_CLOSE (carrying reservation_id) and a thin one
(without it) for the same close, so the keys were structurally different
and dedup could never fire.

Measured 2026-08-27 in logs/expectancy.json:

    ENA/USD   +0.6945  confluence_ENA/USD_1787782175_hwma   (rich)
    ENA/USD   +0.6900  confluence_1787847562412            (thin, size 0)
    BLUR/USD  -0.2807  confluence_BLUR/USD_1787715350_4paj  (rich)
    BLUR/USD  -0.2800  confluence_1787851488650            (thin, size 0)

Two trades became four rows. Note the P/L values DIFFER (one path rounds),
so no content-derived dedup key can catch them either -- which is why the
real fix is at the emitter, and this collapse only repairs history.

This was fixed once before on the CONSUMER side and came back, because a
consumer fix cannot help an emitter that never sends the field. Both ends
are now pinned: the emitter must send reservation_id, and the loader must
collapse what is already on disk.

The collapse must stay CONSERVATIVE. Eating a legitimate close would be a
worse bug than the one it fixes, so the pairing requires same pair, <2s
apart, AND P/L agreeing within 1%.
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
ROOT = os.path.dirname(CC)
sys.path.insert(0, CC)

failures = []

from expectancy import ExpectancyTracker

# Redirect the class-level persist path BEFORE anything can touch it.
#
# test_no_live_writes.py rejected the first version of this file for
# importing ExpectancyTracker without doing this, and it was RIGHT to: the
# class persists on its own inside record_trade(), and this is exactly how
# logs/expectancy.json -- the fleet's entire measured history -- was erased
# once before. This test only calls a @staticmethod and never instantiates
# the class, but "it happens not to write today" is not a guarantee, and
# the guard is checking the pattern rather than the current call graph.
#
# Redirected on the CLASS, not on an instance: a per-instance override is
# the exact hole that let the erasure happen.
import tempfile
ExpectancyTracker.PERSIST_PATH = os.path.join(
    tempfile.gettempdir(), "test_double_record_collapse_store.json")

collapse = ExpectancyTracker._collapse_double_records

CASES = [
    ("true duplicate: same pair, same pnl, 8ms apart",
     [{"pair": "X/USD", "gross_pnl": -0.2807, "size_usd": 13.1, "timestamp": 100.000},
      {"pair": "X/USD", "gross_pnl": -0.2800, "size_usd": 0, "timestamp": 100.008}],
     1, "the live BLUR/USD shape -- must collapse"),
    ("two real closes, same pair, different pnl",
     [{"pair": "X/USD", "gross_pnl": 2.50, "size_usd": 10, "timestamp": 100.0},
      {"pair": "X/USD", "gross_pnl": -1.10, "size_usd": 10, "timestamp": 100.5}],
     2, "different outcomes are different trades -- must NOT collapse"),
    ("two real closes, same pnl, 5s apart",
     [{"pair": "X/USD", "gross_pnl": 1.00, "size_usd": 10, "timestamp": 100.0},
      {"pair": "X/USD", "gross_pnl": 1.00, "size_usd": 10, "timestamp": 105.0}],
     2, "outside the 2s window -- must NOT collapse"),
    ("different pairs at the same instant",
     [{"pair": "A/USD", "gross_pnl": 1.0, "size_usd": 10, "timestamp": 100.0},
      {"pair": "B/USD", "gross_pnl": 1.0, "size_usd": 10, "timestamp": 100.0}],
     2, "different pairs -- must NOT collapse"),
]

for name, rows, expected, why in CASES:
    got = len(collapse({"b": rows})["b"])
    print("  %-46s kept %d (want %d)" % (name, got, expected))
    if got != expected:
        failures.append("%s -> kept %d, expected %d. %s" % (name, got, expected, why))

# The RICH copy must survive -- size-normalized expectancy reads size_usd,
# and the thin copy carries 0, which would corrupt it.
kept = collapse({"b": [
    {"pair": "X/USD", "gross_pnl": -0.2807, "size_usd": 13.1, "timestamp": 100.0},
    {"pair": "X/USD", "gross_pnl": -0.2800, "size_usd": 0, "timestamp": 100.008},
]})["b"]
if kept and not kept[0].get("size_usd"):
    failures.append(
        "the collapse kept the THIN copy (size_usd 0) -- size-normalized "
        "expectancy is computed from size_usd and a zero corrupts it")

# --- the EMITTER must send reservation_id; that is the real fix ---------
conf_path = os.path.join(ROOT, "Confluence", "confluence.py")
if os.path.exists(conf_path):
    with io.open(conf_path, encoding="utf-8", errors="replace") as fh:
        csrc = fh.read()
    code = chr(10).join(l.split("#", 1)[0] for l in csrc.splitlines())
    if '"reservation_id": pos.reservation_id' not in code:
        failures.append(
            "confluence's TRADE_CLOSE no longer carries reservation_id -- "
            "the two recording routes go back to structurally different "
            "dedup keys and every close is stored twice again")
else:
    failures.append("Confluence/confluence.py not found")

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: double-recorded closes collapse, genuine closes survive, the "
      "rich copy is kept, and the emitter still sends reservation_id")
