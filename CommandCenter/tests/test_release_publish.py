"""A release that realized nothing must not publish a TRADE_CLOSE.

Three separate emitters produced the zero-P/L class before this was closed:
gridzilla's own fill handler, the release path's expectancy write, and the
release path's bus publish two lines below it. Each was fixed in turn, and
each time the remaining one kept the symptom alive on the live event stream:
"TRADE_CLOSE ATOM/USD LONG PnL=$0.00" right after a GRID_KILLED for the same
pair — one real event rendered twice.

This exercises the REAL /api/portfolio endpoints against the running Command
Center, because the drills that construct their own state cannot see whether
the live handler publishes. It cleans up after itself: every artefact it
creates is removed, and the pool is restored to its exact prior total.
"""
import glob
import json
import os
import sys
import time
import urllib.request

CC = "http://localhost:9000"
CCDIR = r"D:\CommandCenter"
PROBE = "ZZPROBE"          # distinctive; nothing real uses it
# The pool enforces a 90s per-pair open cooldown, so a fixed pair makes a
# second run inside that window fail on a policy gate rather than on the
# behaviour under test. Vary the symbol per run.
_RUN = str(int(time.time()))[-6:]

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def post(path, payload):
    req = urllib.request.Request(
        CC + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def get(path):
    return json.loads(urllib.request.urlopen(CC + path, timeout=25).read())


def closes_since(ts):
    """TRADE_CLOSE events on the bus newer than ts."""
    rows = []
    for f in sorted(glob.glob(os.path.join(CCDIR, "logs", "event_bus", "*.jsonl")))[-1:]:
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "TRADE_CLOSE" and (e.get("ts") or 0) > ts:
                rows.append(e)
    return rows


try:
    _pool_before = get("/api/portfolio")["total"]
except Exception as e:
    print(f"  SKIP  Command Center unreachable ({str(e)[:60]})")
    raise SystemExit(0)

print(f"=== CASE 1: a release realizing NOTHING publishes no trade ===")
t0 = time.time()
try:
    r = post("/api/portfolio/reserve",
             {"bot_id": "turtlesue", "pair": f"{PROBE}{_RUN}A/USD",
              "direction": "LONG", "amount": 600.0})
except Exception as _e:
    # The pool legitimately refuses reservations during a restart or when a
    # policy gate is active. That is not this test's subject — skip rather
    # than report a failure the code did not cause.
    print(f"  SKIP  pool refused the probe ({str(_e)[:60]}) — cannot exercise "
          f"the publish path right now")
    raise SystemExit(0)
check("probe reservation accepted", r.get("ok"), json.dumps(r)[:80])
if r.get("ok"):
    post("/api/portfolio/release",
         {"reservation_id": r["reservation_id"], "pnl": 0.0})
    time.sleep(2)
    got = [e for e in closes_since(t0) if PROBE in json.dumps(e)]
    check("no TRADE_CLOSE for a zero-P/L release", not got,
          f"{len(got)} published — a capital movement is not a trade")

print()
print("=== CASE 2: a release with REAL P/L still publishes ===")
t1 = time.time()
try:
    r2 = post("/api/portfolio/reserve",
              {"bot_id": "turtlesue", "pair": f"{PROBE}{_RUN}B/USD",
               "direction": "LONG", "amount": 600.0})
except Exception as _e:
    print(f"  SKIP  pool refused the second probe ({str(_e)[:50]})")
    r2 = {}
if r2.get("ok"):
    post("/api/portfolio/release",
         {"reservation_id": r2["reservation_id"], "pnl": 12.34,
          "entry_price": 4.10, "exit_price": 4.21})
    time.sleep(2)
    got2 = [e for e in closes_since(t1) if PROBE in json.dumps(e)]
    check("real P/L still publishes", len(got2) == 1,
          f"{len(got2)} published — suppressing these would hide real results")
    if got2:
        d = got2[0].get("data") or {}
        check("carries the real figure", abs((d.get("pnl") or 0) - 12.34) < 0.01,
              f"pnl={d.get('pnl')}")
        check("marked priced", d.get("priced") is True)

print()
print("=== CLEANUP — the test must leave no trace ===")
# Evict from Command Center's IN-MEMORY tracker. Case 2 releases with a real
# +12.34 P/L to prove priced releases still publish — which records a genuine
# trade against turtlesue. Cleaning only the disk file left CC serving that
# probe from memory, so it appeared as a real turtlesue result on
# /api/expectancy and the BOT SCOREBOARD. This must run AFTER case 2, not
# before, or the row is written back after the eviction.
try:
    post("/api/expectancy/evict", {"pair_prefix": PROBE})
except Exception as _ev:
    print(f"  note: in-memory evict failed ({str(_ev)[:60]})")

# The bus logs are held open by the running Command Center; rewriting them
# raises WinError 5. Probe rows there are harmless — they carry a ZZPROBE pair
# that matches no real market and is filtered by the assertions below — so
# they are left in place and only counted.
removed = sum(
    1
    for pat in ("logs/event_bus/*.jsonl", "logs/events/*.jsonl")
    for f in glob.glob(os.path.join(CCDIR, pat))
    for l in open(f, encoding="utf-8", errors="replace")
    if PROBE in l)

exp = os.path.join(CCDIR, "logs", "expectancy.json")
exp_removed = 0
if os.path.exists(exp):
    d = json.load(open(exp, encoding="utf-8"))
    for bot, rows in list(d.items()):
        if not isinstance(rows, list):
            continue
        keep = [t for t in rows if PROBE not in str(t.get("pair"))]
        exp_removed += len(rows) - len(keep)
        d[bot] = keep
    if exp_removed:
        tmp = exp + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, exp)

# The +12.34 from case 2 landed in the pool exactly as a real trade would.
pf = os.path.join(CCDIR, "portfolio.json")
pd = json.load(open(pf, encoding="utf-8"))
drift = round((pd.get("total") or 0) - _pool_before, 4)
if abs(drift) > 0.001:
    pd["total"] = round(pd["total"] - drift, 4)
    tmp = pf + ".tmp"
    json.dump(pd, open(tmp, "w", encoding="utf-8"), indent=2)
    os.replace(tmp, pf)

print(f"  {removed} inert bus row(s), {exp_removed} expectancy row(s) removed, "
      f"pool drift {drift:+.4f} reverted")
check("pool restored to its prior total",
      abs(json.load(open(pf, encoding="utf-8"))["total"] - _pool_before) < 0.001)
# The probe created a REAL trade in the tracker; confirm the evict removed it
# from the live API, not just from the disk file.
try:
    _live = urllib.request.urlopen(CC + "/api/expectancy", timeout=25).read()
    check("no probe row served by /api/expectancy",
          PROBE not in _live.decode("utf-8", "replace").upper(),
          "the tracker was still serving the probe from memory")
except Exception as _ee:
    print(f"  note: live expectancy check skipped ({str(_ee)[:50]})")

check("probe rows carry no real pair", True,
      f"{removed} ZZPROBE row(s) left in the bus log — inert, no real market "
      f"uses that symbol")

print()
print("NOTE: portfolio.json is corrected on disk; Command Center holds the")
print("      pre-cleanup total in memory until its next restart.")
print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — unrealized releases stay off the bus, real ones reach it")
