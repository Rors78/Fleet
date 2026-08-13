"""A corrupt portfolio.json must not read as an empty, unconstrained pool.

PortfolioManager._load swallowed JSONDecodeError with `pass`, so a
truncated write produced the LOOSEST possible state, not a safe one:

  - max_deployed_pct fell back to the 80% config default, discarding an
    AEGIS tightening to 30% (the file's own docstring records this handing
    back "$200,000 of deployment headroom ... in the regime AEGIS had just
    scored as defensive")
  - reservations came back EMPTY, so deployed() reports 0.00 and the entire
    $1M pool reads as available while the capital is still committed
  - the AEGIS raise-hold clock reset

Every one of those fails toward RELEASE, and all three at once.

Separately: _pair_cooldowns and _pair_opens were never persisted at all.
The OPEN gate exists to stop burst same-pair entries (written for 4xENJ/USD
opens in 60s) and it was silently disarmed by every restart — with restarts
as frequent as they are on this machine, disarmed most of the time. Absent
was indistinguishable from "no pair has traded recently".
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


# Use the SHIPPED limits shape — a hand-written subset omits keys reserve()
# indexes directly and turns a behavioural test into a KeyError.
import fleet_config as _fc
LIMITS = dict(_fc.PORTFOLIO_LIMITS)


def tmpfile(name='portfolio.json'):
    return os.path.join(tempfile.mkdtemp(), name)


# ── 1. A corrupt file ARMS and refuses new capital ──
p = tmpfile()
with open(p, 'w') as f:
    f.write('{"total": 1000000, "reservations": {"a": {"amou')   # truncated
pm = cc.PortfolioManager(1_000_000, dict(LIMITS), p, mode_tag="paper")

check(getattr(pm, 'state_unreadable', False) is True,
      'a corrupt portfolio.json must set state_unreadable — otherwise it is '
      'indistinguishable from a first run')
r = pm.reserve('turtlesue', 'BTC/USD', 'LONG', 1000.0)
check(r.get('ok') is False and 'unreadable' in (r.get('reason') or '').lower(),
      'reserve() must REFUSE while the reservation book is unknown — '
      'deployed() reports 0.00 and every limit is computed against it; '
      'got %r' % (r,))

# The unparseable bytes must be preserved, not overwritten.
_d = os.path.dirname(p)
check(any(n.startswith('portfolio.json.corrupt_') for n in os.listdir(_d)),
      'the unreadable file must be quarantined — it is the only record of '
      'what was reserved; dir=%r' % os.listdir(_d))

# ── 2. Releases must still work — a bot must always be able to return capital ──
pm2 = cc.PortfolioManager(1_000_000, dict(LIMITS), tmpfile(), mode_tag="paper")
ok = pm2.reserve('turtlesue', 'BTC/USD', 'LONG', 1000.0)
check(ok.get('ok') is True, 'sanity: a healthy pool must accept a reservation')
_rid = ok.get('reservation_id')
pm2.state_unreadable = True          # simulate arming after the reserve
rel = pm2.release(_rid, pnl=5.0)
check(rel.get('ok') is True,
      'release must still work while armed — refusing returns would strand '
      'capital and make the arming worse than the failure; got %r' % (rel,))

# ── 3. A genuinely ABSENT file is a first run and must NOT arm ──
pm3 = cc.PortfolioManager(1_000_000, dict(LIMITS), tmpfile(), mode_tag="paper")
check(getattr(pm3, 'state_unreadable', False) is False,
      'a missing file is a first run — arming would refuse every reservation '
      'on a brand new deployment')
check(pm3.reserve('turtlesue', 'BTC/USD', 'LONG', 1000.0).get('ok') is True,
      'a first run must be able to reserve capital')

# ── 4. Cooldowns survive a restart ──
p4 = tmpfile()
pm4 = cc.PortfolioManager(1_000_000, dict(LIMITS), p4, mode_tag="paper")
res4 = pm4.reserve('turtlesue', 'ENJ/USD', 'LONG', 1000.0)
check(res4.get('ok') is True, 'sanity: first ENJ open must be allowed')
check('ENJ/USD' in pm4._pair_opens or 'ENJUSD' in str(pm4._pair_opens),
      'the OPEN stamp must be recorded, got %r' % (pm4._pair_opens,))

with open(p4) as f:
    _on_disk = json.load(f)
check('pair_opens' in _on_disk and _on_disk['pair_opens'],
      'the open-cooldown stamp must be PERSISTED — memory-only means every '
      'restart re-opens the burst window; keys=%r' % sorted(_on_disk))

# Restart: a second manager over the same file must still block the burst.
pm5 = cc.PortfolioManager(1_000_000, dict(LIMITS), p4, mode_tag="paper")
check(pm5._pair_opens != {},
      'the cooldown must be restored on restart, got %r' % (pm5._pair_opens,))
burst = pm5.reserve('rubberband', 'ENJ/USD', 'LONG', 1000.0)
check(burst.get('ok') is False and 'cooldown' in (burst.get('reason') or '').lower(),
      'a same-pair entry within the cooldown must STILL be blocked after a '
      'restart — this is the gate that was disarmed by every reboot; got %r'
      % (burst,))

# ── 5. A FUTURE cooldown timestamp is nonsense and must be dropped ──
p6 = tmpfile()
with open(p6, 'w') as f:
    json.dump({"total": 1_000_000, "reservations": {}, "history": [],
               "limits": {"max_deployed_pct": 60},
               "pair_opens": {"BTC/USD": {"ts": time.time() + 86400,
                                          "bot_id": "x"}}}, f)
pm6 = cc.PortfolioManager(1_000_000, dict(LIMITS), p6, mode_tag="paper")
check(pm6._pair_opens == {},
      'a FUTURE cooldown ts must be discarded rather than holding the gate '
      'open indefinitely; got %r' % (pm6._pair_opens,))
check(getattr(pm6, 'state_unreadable', False) is False,
      'a well-formed file with one odd field is READABLE — it must not arm')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  corrupt portfolio state arms; cooldowns survive restarts')
