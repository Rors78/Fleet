"""A non-finite amount must never reach the portfolio.

Found by a capital-path audit and confirmed against the live endpoint on
2026-08-06. Every risk gate in reserve() is an upper-bound comparison, and
EVERY comparison against NaN is False — so a single NaN amount passed the
deployment limit, the per-bot cap, the per-pair cap, the directional cap, the
concentration cap and the size floor simultaneously.

It was worse than a bypassed gate. deployed() then summed to NaN, json.dumps
with allow_nan=False raised rather than emitting invalid JSON, and
/api/portfolio and /api/master both served EMPTY BODIES — the two endpoints
every bot polls. The fleet went to 0/19 reporting. The NaN also persisted
into portfolio.json (in history, not reservations), so it survived a restart.

Three layers now: reject the JSON literal at the request boundary, reject a
non-finite or non-positive amount at the handler, and drop unloadable
reservations at _load. Plus allow_nan=False on save and a sanitizing retry on
the response encoder so one bad value can never take an endpoint down again.
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

CC = "http://localhost:9000"
fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def raw_post(path, body):
    """POST a raw string so we can send literals json.dumps would refuse."""
    req = urllib.request.Request(CC + path, data=body.encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


try:
    _before = json.loads(urllib.request.urlopen(CC + "/api/portfolio",
                                                timeout=25).read())
except Exception as e:
    print(f"  SKIP  Command Center unreachable ({str(e)[:60]})")
    raise SystemExit(0)

RUN = str(int(time.time()))[-6:]

print("=== CASE 1: the JSON boundary rejects non-finite literals ===")
for label, lit in (("NaN", "NaN"), ("Infinity", "Infinity"),
                   ("-Infinity", "-Infinity")):
    code, body = raw_post("/api/portfolio/reserve",
                          '{"bot_id":"turtlesue","pair":"NF%s%s/USD",'
                          '"direction":"LONG","amount":%s}' % (RUN, label[:3], lit))
    check(f"{label} rejected", code == 400 and "non-finite" in body,
          f"HTTP {code} {body[:70]}")

print()
print("=== CASE 2: the amount guard rejects non-positive ===")
for label, val in (("negative", "-5000"), ("zero", "0")):
    code, body = raw_post("/api/portfolio/reserve",
                          '{"bot_id":"turtlesue","pair":"NF%s%s/USD",'
                          '"direction":"LONG","amount":%s}' % (RUN, label[:3], val))
    check(f"{label} rejected", code == 400 and "positive" in body,
          f"HTTP {code} {body[:70]}")

print()
print("=== CASE 3: a legitimate amount still reserves ===")
code, body = raw_post("/api/portfolio/reserve",
                      '{"bot_id":"turtlesue","pair":"NF%sOK/USD",'
                      '"direction":"LONG","amount":600}' % RUN)
ok = code == 200 and json.loads(body).get("ok")
# What this case proves is that the NON-FINITE guard does not reject a
# legitimate number — NOT that the fleet always has capacity. A denial by a
# DOWNSTREAM capacity gate (deployment/per-bot/directional limit or cooldown)
# means the amount already PASSED the finite/positive guards and reached real
# risk logic, which is exactly the property under test. On 2026-08-08 the
# fleet sat at 30.4% deployed against an AEGIS-tightened 30% cap, and this
# case read the correct denial as a regression.
_reason = ""
if not ok:
    try:
        _reason = str(json.loads(body).get("reason") or "")
    except Exception:
        _reason = ""
_capacity = code == 403 and any(k in _reason.lower() for k in (
    "limit", "cooldown", "headroom", "cap"))
if _capacity:
    check("legit reserve accepted",
          True, f"denied by a capacity gate, not the guard: {_reason[:60]}")
else:
    check("legit reserve accepted", ok, f"HTTP {code} {body[:70]}")
_rid = json.loads(body).get("reservation_id") if ok else None

print()
print("=== CASE 4: the API is still serving after all of it ===")
for name in ("/api/portfolio", "/api/master"):
    try:
        raw = urllib.request.urlopen(CC + name, timeout=25).read()
        json.loads(raw)
        check(f"{name} serves valid JSON", len(raw) > 0, f"{len(raw)} bytes")
    except Exception as e:
        check(f"{name} serves valid JSON", False, str(e)[:60])

print()
print("=== CASE 5: nothing non-finite reached disk ===")
pf = os.path.join(r"D:\CommandCenter", "portfolio.json")
if os.path.exists(pf):
    raw = open(pf, encoding="utf-8", errors="replace").read()
    check("no NaN/Infinity literal in portfolio.json",
          "NaN" not in raw and "Infinity" not in raw,
          "an invalid-JSON literal on disk survives every restart")

print()
print("=== CLEANUP ===")
if _rid:
    raw_post("/api/portfolio/release",
             json.dumps({"reservation_id": _rid, "pnl": 0.0}))
p = json.loads(urllib.request.urlopen(CC + "/api/portfolio", timeout=25).read())
strays = [r for r in (p.get("reservations") or [])
          if "NF" + RUN in str(r.get("pair"))]
check("no probe reservations left", not strays, f"{len(strays)} remain")
check("pool total unchanged",
      abs((p.get("total") or 0) - (_before.get("total") or 0)) < 0.001,
      f"{_before.get('total')} -> {p.get('total')}")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — non-finite input cannot bypass a gate or kill an endpoint")
