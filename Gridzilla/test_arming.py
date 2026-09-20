#!/usr/bin/env python3
"""Per-bot live arming gate.

FLEET_MODE is ONE switch read by seven bots. Flipping it to "live" armed every
one of them that held credentials, at the same instant, and an individual bot
had no say. These tests pin the stricter gate: a bot trades real money only
when the fleet is live AND engaged AND that bot is individually armed.

The one that matters is the NEGATIVE CONTROL: fleet fully live and engaged,
bot not armed -> still paper. If that ever passes live, the gate is gone.

Run: python test_arming.py
"""
import os
import sys

sys.path.insert(0, r"D:\CommandCenter")
import fleet_config as fc  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def reset(mode="paper", engage="paper", armed=()):
    fc.FLEET_MODE = mode
    fc.FLEET_ENGAGE_STATE = engage
    fc.LIVE_ARMED_BOTS.clear()
    fc.LIVE_ARMED_BOTS.update(armed)


print("\nper-bot arming -- all three predicates required")

reset("paper", "paper")
check("paper fleet, unarmed bot -> paper", not fc.is_bot_live("gridzilla"))

reset("live", "live_armed")
check("fleet live but NOT engaged, unarmed -> paper", not fc.is_bot_live("gridzilla"))

reset("live", "live_engaged")
check("NEGATIVE CONTROL: fleet live AND engaged, bot UNARMED -> still paper",
      not fc.is_bot_live("gridzilla"),
      "this is the whole point of the gate")

reset("live", "live_engaged", ("gridzilla",))
check("live + engaged + armed -> LIVE", fc.is_bot_live("gridzilla"))

reset("paper", "paper", ("gridzilla",))
check("armed bot in a PAPER fleet -> paper", not fc.is_bot_live("gridzilla"))

reset("live", "live_armed", ("gridzilla",))
check("armed bot, live but not engaged -> paper", not fc.is_bot_live("gridzilla"))

reset("live", "live_engaged", ("turtlesue",))
check("arming ANOTHER bot does not arm this one", not fc.is_bot_live("gridzilla"),
      "turtlesue armed, gridzilla asked")
check("...and the armed one IS live", fc.is_bot_live("turtlesue"))

print("\nis_live() is unchanged for bots that have not opted in")
reset("live", "live_engaged")
check("is_live() still reports fleet state", fc.is_live())
check("is_bot_live() is STRICTER than is_live()",
      fc.is_live() and not fc.is_bot_live("gridzilla"))

print("\nthe reason is always stated -- never a silent branch")
reset("paper", "paper")
_w = fc.why_not_live("gridzilla")
check("paper reason names every failing predicate",
      "fleet_mode" in _w and "engage" in _w and "not armed" in _w, _w[:88])
reset("live", "live_engaged", ("gridzilla",))
_w2 = fc.why_not_live("gridzilla")
check("live reason says LIVE", _w2.startswith("LIVE:"), _w2[:88])

print("\nenv-var arming (arming is out-of-band, not an HTTP call)")
_prev = os.environ.get("GRIDZILLA_LIVE_ARM")
try:
    os.environ["GRIDZILLA_LIVE_ARM"] = "1"
    check("GRIDZILLA_LIVE_ARM=1 is recognised", "gridzilla" in fc._load_armed_bots())
    os.environ["GRIDZILLA_LIVE_ARM"] = "0"
    check("NEGATIVE CONTROL: =0 does not arm", "gridzilla" not in fc._load_armed_bots())
    os.environ.pop("GRIDZILLA_LIVE_ARM")
    check("unset does not arm", "gridzilla" not in fc._load_armed_bots())
finally:
    if _prev is not None:
        os.environ["GRIDZILLA_LIVE_ARM"] = _prev

print("\ngridzilla.py consults the gate, not the fleet switch")
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "gridzilla.py"), encoding="utf-8").read()
check("startup no longer gates on bare is_live()",
      "_fc.is_live()" not in _src, "must use is_bot_live")
check("gate consulted at startup AND both trade sites",
      _src.count('is_bot_live("gridzilla")') >= 3,
      f'{_src.count(chr(34)) and _src.count("is_bot_live(" + chr(34) + "gridzilla" + chr(34) + ")")} call sites')
check("NEGATIVE CONTROL: a rejected order is never booked as a fill",
      "using paper fill" not in _src and _src.count("REJECTED") >= 2)

reset("paper", "paper")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"  FAILED: {f}")
    sys.exit(1)
print("all green")
