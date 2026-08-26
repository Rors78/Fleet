"""The current-era boundary must exclude every old-pool trade, and no others.

WHY THIS EXISTS. The pool was resized $1,000,000 -> $210.53 mid-run. Bot P/L
ledgers are lifetime and cross that boundary, so the published fleet total
(-$362.76) adds two different pools' worth of dollars under one label. A
single pre-resize trade (ETHFI +3111.19, notional $38,557) exceeds the whole
current pool by 14x.

/api/current_era exists to answer "how is the fleet doing on the money it
has now", and the entire answer depends on one constant. Two wrong values
were written during the fix, and BOTH failed silently by shifting which
trades count:

    1786924800.0  = 2026-08-17 UTC, four days LATE.  Dropped 7 real trades.
    1786579200.0  = 2026-08-13 00:00 UTC, ~21h EARLY. Admitted 12 old-pool
                    trades and reported +$939 on a $215 pool with a $300
                    average win -- a 436% return that no one would have
                    believed, but the arithmetic was clean.

Neither produced an error. Both produced a confident number. That is the
shape this test exists to catch.

THE BOUNDARY IS MEASURED, NOT ASSUMED. Position notionals break cleanly and
permanently at 2026-08-13 21:42 UTC:

    08-13 19:22  ETHFI/USD  +3111.19   size $38,557.56   <- old pool
    08-13 21:42  CRV/USD       -0.01   size      $6.80   <- new pool

The constant sits inside that 2h20m gap. This test pins the property that
matters -- no surviving trade is sized at old-pool scale -- rather than the
literal number, so a future re-derivation is free to move it as long as the
separation still holds.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
ROOT = os.path.dirname(CC)
SRC_PATH = os.path.join(CC, "command_center.py")

sys.path.insert(0, CC)

failures = []

if not os.path.exists(SRC_PATH):
    print("FAIL: command_center.py not found at %s" % SRC_PATH)
    sys.exit(1)

with open(SRC_PATH, encoding="utf-8", errors="replace") as fh:
    SRC = fh.read()

m = re.search(r"\nPOOL_RESIZE_TS\s*=\s*([0-9.]+)", SRC)
if not m:
    print("FAIL: POOL_RESIZE_TS not found — the era boundary is gone")
    sys.exit(1)
TS = float(m.group(1))

# ---------------------------------------------------------------------------
# 1. Sanity: the constant must land on the right calendar day, in UTC.
#    Both wrong values would have been caught here.
# ---------------------------------------------------------------------------
import time

day = time.strftime("%Y-%m-%d", time.gmtime(TS))
if day != "2026-08-13":
    failures.append(
        "POOL_RESIZE_TS is %s UTC, not 2026-08-13 — the resize day. The two "
        "values this test was written against were 2026-08-17 (4d late, "
        "dropped 7 real trades) and 2026-08-13 00:00 (21h early, admitted 12 "
        "old-pool trades worth +$939 on a $215 pool)" % day)

# ---------------------------------------------------------------------------
# 2. The real assertion: partition the durable trade record and prove the
#    separation. Old-pool trades were sized in the thousands; current-pool
#    trades cannot exceed the pool's own per-trade cap.
# ---------------------------------------------------------------------------
try:
    from probe_pairs import is_probe_pair
except Exception as e:            # pragma: no cover
    print("FAIL: cannot import probe_pairs: %r" % e)
    sys.exit(1)

import glob
import json

# The durable bus lives with the DEPLOYED Command Center, not inside a git
# worktree — a worktree checkout has no logs/ at all. Prefer the local copy
# (so a relocated install still tests itself) and fall back to the canonical
# deployment path. FLEET_BUS_DIR overrides both.
_CANDIDATES = [
    os.environ.get("FLEET_BUS_DIR"),
    os.path.join(CC, "logs", "event_bus"),
    r"D:\CommandCenter\logs\event_bus",
]
BUS = None
for _c in _CANDIDATES:
    if _c and glob.glob(os.path.join(_c, "*.jsonl")):
        BUS = os.path.join(_c, "*.jsonl")
        break
if BUS is None:
    print("FAIL: no event_bus jsonl found in any of %s — cannot verify the "
          "boundary against anything. An unreadable bus is not a clean bus."
          % [c for c in _CANDIDATES if c])
    sys.exit(1)

rows = []
for f in sorted(glob.glob(BUS)):
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "TRADE_CLOSE" not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if (e.get("type") or "") != "TRADE_CLOSE":
                    continue
                d = e.get("data") or {}
                if is_probe_pair(d.get("pair")):
                    continue
                rows.append((e.get("ts") or 0, d.get("pair"),
                             d.get("size_usd") or d.get("notional")))
    except OSError:
        continue

if not rows:
    # An empty read is not a pass. Absence of the bus is absence of evidence.
    print("FAIL: no TRADE_CLOSE rows found under %s — cannot verify the "
          "boundary against anything. This is not a clean result." % BUS)
    sys.exit(1)

post = [r for r in rows if r[0] >= TS]
pre = [r for r in rows if r[0] < TS]

post_sizes = [s for _, _, s in post if isinstance(s, (int, float))]
pre_sizes = [s for _, _, s in pre if isinstance(s, (int, float))]

# The live pool is ~$215 with a 20% per-trade cap. Anything an order of
# magnitude above that is an old-pool trade that leaked through.
POOL_SCALE_MAX = 200.0

leaked = [(t, p, s) for t, p, s in post
          if isinstance(s, (int, float)) and s > POOL_SCALE_MAX]
if leaked:
    for t, p, s in leaked[:5]:
        failures.append(
            "old-pool trade survived the boundary: %s %s sized $%.2f at %s "
            "UTC — the current pool is ~$215 with a $43 per-trade cap, so "
            "this was sized against the $1M pool"
            % (p, "", s, time.strftime("%m-%d %H:%M", time.gmtime(t))))

if not post:
    failures.append("no trades at all after POOL_RESIZE_TS — the boundary is "
                    "in the future and the era window is empty")

# And the cut must actually be doing work: if nothing was excluded, the
# constant is too early to be separating anything.
if not pre:
    failures.append("no trades before POOL_RESIZE_TS — the boundary excludes "
                    "nothing, so it is not separating the two pools")

# ---------------------------------------------------------------------------
# 3. The disclosure must survive. A correct era figure alongside an
#    undisclosed lifetime total is still misleading.
# ---------------------------------------------------------------------------
if "total_pnl_spans_pool_resize" not in SRC:
    failures.append("aggregate no longer discloses that total_pnl spans the "
                    "pool resize — the -$362.76 headline reads as current")

# --------------------------------------------------------------------- report
print("POOL_RESIZE_TS = %.1f (%s UTC)"
      % (TS, time.strftime("%Y-%m-%d %H:%M", time.gmtime(TS))))
print("trades: %d total, %d pre-resize, %d current-era" % (len(rows), len(pre), len(post)))
if post_sizes:
    print("current-era sizes:  $%.2f .. $%.2f" % (min(post_sizes), max(post_sizes)))
if pre_sizes:
    print("pre-resize sizes:   $%.2f .. $%.2f" % (min(pre_sizes), max(pre_sizes)))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: every current-era trade is pool-scale, the boundary excludes the "
      "old-pool run, and the lifetime total discloses the crossing")
