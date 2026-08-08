"""/api/signals/decay must serve measurements, not constants.

SignalDecay.record_outcome had ZERO call sites since the module was written.
The endpoint served 35 half-lives, every one `data_points: 0, source:
"default"` — hardcoded constants presented on a measurement surface. The
payload at least labelled them honestly, but a 35-row table of defaults
invites being read as empirical.

The close handler now feeds it: when a TRADE_CLOSE resolves with a measured,
non-zero P/L and the open-time signal record still exists, each signal that
was present at open gets an outcome. Unpriced and flat closes deliberately do
NOT count — a capital movement says nothing about whether a signal's edge had
decayed.
"""
import importlib
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, 'D:/CommandCenter')
import signal_decay as sd
importlib.reload(sd)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── 1. The call site must exist, positively pinned ──
src = open('D:/CommandCenter/command_center.py', encoding='utf-8',
           errors='replace').read()
check('_signal_decay.record_outcome(' in src,
      'the TRADE_CLOSE handler must feed _signal_decay.record_outcome — '
      'without a call site the endpoint serves constants forever')
check('isinstance(pnl, (int, float)) and pnl != 0' in src,
      'only measured, non-zero outcomes may feed the decay estimate — an '
      'unpriced or flat close says nothing about signal decay')

# ── 2. Behavioural: outcomes accumulate and flip the source to empirical ──
t = sd.SignalDecay()
t.PERSIST_PATH = os.path.join(tempfile.mkdtemp(), 'd.json')
t.outcome_log = []
now = time.time()
random.seed(3)
for i in range(25):
    delay = random.uniform(30, 1800)
    profitable = (delay < 600) or (random.random() < 0.5)
    t.record_outcome('gridzilla:grid_fill', now - delay, now, profitable)

rows = {r['signal_type']: r for r in t.get_all_half_lives()}
mine = rows.get('gridzilla:grid_fill')
check(mine is not None,
      'a signal with 25 recorded outcomes must appear in the listing')
if mine:
    check(mine['data_points'] == 25,
          'data_points must count the real outcomes, got %r'
          % mine['data_points'])
    check(mine.get('source') == 'empirical',
          'with >= 20 outcomes the half-life must be EMPIRICAL, not the '
          'default constant; got %r' % mine.get('source'))
    check(100 < mine['half_life_seconds'] < 1800,
          'the estimate must land in a plausible band for a ~600s decay, '
          'got %r' % mine['half_life_seconds'])

# ── 3. Below the threshold the default is served, still labelled ──
t2 = sd.SignalDecay()
t2.PERSIST_PATH = os.path.join(tempfile.mkdtemp(), 'd2.json')
t2.outcome_log = []
for i in range(5):
    t2.record_outcome('WHALE_ALERT', now - 100, now, True)
w = {r['signal_type']: r for r in t2.get_all_half_lives()}.get('WHALE_ALERT')
check(w is not None and w.get('source') == 'default',
      'below 20 outcomes the default must still be served AND labelled '
      'default — an under-sampled estimate is worse than an honest constant; '
      'got %r' % (w and w.get('source')))
check(w is not None and w.get('data_points') == 5,
      'the accumulating count must be visible, got %r'
      % (w and w.get('data_points')))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  decay outcomes accumulate; empirical overrides default at n>=20')
