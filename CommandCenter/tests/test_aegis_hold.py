"""Progress toward RELEASING a risk cap must survive a restart, like the cap.

The AEGIS deployment cap tightens instantly and raises only after the
computed tier holds for _AEGIS_RAISE_HOLD_SEC (20 min) — deliberate
hysteresis, because AEGIS spikes over a tier boundary for 5-15 min several
times a day.

The TIGHTENING was persisted to portfolio.json (a de-risking brake must
survive restarts). The PROGRESS TOWARD RELEASE was not: `_aegis_raise_pending`
was an in-memory global, so every restart reset the clock to zero. That made
restarts a ONE-WAY RATCHET — they could only ever hold the fleet tighter.

Live on 2026-08-13: the fleet was restarted roughly every 15 minutes against
a 20-minute hold, so the raise NEVER applied. The pool sat at the DEFENSIVE
30% cap for hours while AEGIS scored 0.2259 = CAUTIOUS = 60%, with the AEGIS
tab showing "ENFORCEMENT MISMATCH: engine says 60%, portfolio enforcing 30%".
That 30% cap is what denied TurtleSue's re-reserve and forced its XRP
position closed at a fabricated price.

Log evidence of the reset, two restarts in a row:
  03:37:40 AEGIS: raise to 60% pending 20min hold (score=0.2328)
  03:55:12 Restored AEGIS-tightened deployment cap 30% (config default 80%)
  03:58:10 AEGIS: raise to 60% pending 20min hold (score=0.2259)   <- restarted
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, 'D:/CommandCenter')

import command_center as cc

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


LIMITS = {"max_deployed_pct": 80, "max_per_bot_pct": 25}


def fresh_file():
    return os.path.join(tempfile.mkdtemp(), 'portfolio.json')


# ── 1. A hold in progress is persisted and restored with its elapsed time ──
path = fresh_file()
pm = cc.PortfolioManager(1_000_000, dict(LIMITS), path, mode_tag="paper")
pm.limits["max_deployed_pct"] = 30          # AEGIS-tightened, as live

started = time.time() - 900                 # 15 of 20 minutes already elapsed
cc._aegis_raise_pending = {"limit": 60, "since": started}
pm._save()

cc._aegis_raise_pending = {}                # simulate the process dying
pm2 = cc.PortfolioManager(1_000_000, dict(LIMITS), path, mode_tag="paper")

check(cc._aegis_raise_pending.get("limit") == 60,
      'the pending raise target must survive a restart, got %r'
      % (cc._aegis_raise_pending,))
check(abs(cc._aegis_raise_pending.get("since", 0) - started) < 1,
      'the ELAPSED time must survive, not restart at zero — a restart every '
      '15min against a 20min hold means the raise never applies; got since=%r '
      'expected %r' % (cc._aegis_raise_pending.get("since"), started))
# .get, not [] — with the fix reverted there is no "since" at all, and this
# must report the finding rather than dying on a KeyError traceback.
_since = cc._aegis_raise_pending.get("since")
_elapsed_min = (time.time() - _since) / 60 if _since else None
check(_elapsed_min is not None and 14 < _elapsed_min < 16,
      'the restored hold must read ~15 min elapsed, got %r'
      % (('%.1f' % _elapsed_min) if _elapsed_min is not None
         else 'NO PENDING STATE AT ALL'))

# The tightened cap must still survive too — this fix must not undo that.
check(pm2.limits.get("max_deployed_pct") == 30,
      'the AEGIS-tightened cap must still survive restarts, got %r'
      % pm2.limits.get("max_deployed_pct"))

# ── 2. A completed hold applies on the very next cycle after a restart ──
# (Rather than being reset and starting another full 20 minutes.)
check(_elapsed_min is not None and _elapsed_min * 60 < cc._AEGIS_RAISE_HOLD_SEC,
      'sanity: 15 min is inside the 20 min hold')
cc._aegis_raise_pending = {"limit": 60,
                           "since": time.time() - cc._AEGIS_RAISE_HOLD_SEC - 60}
pm._save()
cc._aegis_raise_pending = {}
cc.PortfolioManager(1_000_000, dict(LIMITS), path, mode_tag="paper")
_done = (time.time() - cc._aegis_raise_pending.get("since", time.time()))
check(_done >= cc._AEGIS_RAISE_HOLD_SEC,
      'a hold that COMPLETED before the restart must read as complete after '
      'it, so the raise applies on the next cycle; got %.0fs of %ds'
      % (_done, cc._AEGIS_RAISE_HOLD_SEC))

# ── 3. Absent state is absent — no pending raise is fabricated ──
path2 = fresh_file()
cc._aegis_raise_pending = {"limit": 90, "since": time.time()}
cc.PortfolioManager(1_000_000, dict(LIMITS), path2, mode_tag="paper")
check(cc._aegis_raise_pending == {"limit": 90, "since": cc._aegis_raise_pending.get("since")},
      'a missing file must leave the in-memory hold untouched, not fabricate '
      'or clear one; got %r' % (cc._aegis_raise_pending,))

# ── 4. A FUTURE timestamp must not grant the raise instantly ──
# A clock change or hand-edited file would otherwise make the hold read as
# already satisfied — failing toward MORE deployment, the unsafe direction.
path3 = fresh_file()
with open(path3, 'w') as f:
    json.dump({"total": 1_000_000, "reservations": {}, "history": [],
               "limits": {"max_deployed_pct": 30},
               "aegis_raise_pending": {"limit": 90,
                                       "since": time.time() + 86400}}, f)
cc._aegis_raise_pending = {}
cc.PortfolioManager(1_000_000, dict(LIMITS), path3, mode_tag="paper")
check(cc._aegis_raise_pending == {},
      'a FUTURE hold timestamp must be discarded (it would grant the raise '
      'immediately — failing toward more deployment); got %r'
      % (cc._aegis_raise_pending,))

# ── 5. Malformed pending state is discarded, not trusted ──
for _bad in ({"limit": "sixty", "since": time.time() - 100},
             {"limit": 60},
             {"limit": 0, "since": time.time() - 100},
             {"limit": 200, "since": time.time() - 100}):
    _p = fresh_file()
    with open(_p, 'w') as f:
        json.dump({"total": 1_000_000, "reservations": {}, "history": [],
                   "limits": {"max_deployed_pct": 30},
                   "aegis_raise_pending": _bad}, f)
    cc._aegis_raise_pending = {}
    cc.PortfolioManager(1_000_000, dict(LIMITS), _p, mode_tag="paper")
    check(cc._aegis_raise_pending == {},
          'malformed hold state %r must be discarded, got %r'
          % (_bad, cc._aegis_raise_pending))

# ── 6. Source pin: the adjuster persists whenever the hold CHANGES ──
import inspect
_src = inspect.getsource(cc._apply_aegis_adjustment)
check('_pending_before' in _src and '_aegis_raise_pending != _pending_before' in _src,
      'the adjuster must save when the hold state changes even if the cap '
      'did not — otherwise the clock is only ever written on a cap change')

cc._aegis_raise_pending = {}
if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  the raise-hold clock survives restarts; the ratchet is two-way')
