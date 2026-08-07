"""A persistent condition must not be re-logged on every scan.

Contrarian already gated its BUS publishes correctly: `_publish` fires on a
new under->over crossing, on a material change (>20% or a direction flip), or
on a 30-minute heartbeat. But the `log.warning("SIGNAL: ...")` calls sat
OUTSIDE that gate and fired unconditionally every scan.

Live result: 12 funding pairs over threshold produced 162 identical WARNING
lines in one window, growing ~72 lines per interval. That is how a log stops
being read — a genuine warning arriving in the middle of it is invisible, and
the fleet had just spent a day fixing logs precisely so they could be trusted
as evidence.

The fix routes the log through the same gate the bus already used, so the two
cannot disagree. Suppressed repeats are still counted and reported at INFO,
so a quiet log is never mistaken for a quiet market.

Both directions asserted: unchanged repeats suppressed, genuine events still
logged immediately.
"""
import re
import sys
import time

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/Contrarian/contrarian.py', encoding='utf-8', errors='replace').read()

# ── Structural: every log site must be gated ──
ungated = re.findall(r'\n\s+self\._publish\([^\n]*\)\n\s+signals_found \+= 1\n\s+log\.warning',
                     src)
check(not ungated,
      'a SIGNAL log.warning is still fired unconditionally after _publish — '
      'found %d ungated site(s)' % len(ungated))
check(src.count('_new = self._publish(') == 5,
      'all 5 _publish call sites must capture the gate result; found %d'
      % src.count('_new = self._publish('))
check('return True' in src and 'return False' in src,
      '_publish must report whether it actually published')

# ── Behavioural: exercise the real _publish ──
m = re.search(r'    def _publish\(self, signal_type, data\):\n(?:.*\n)*?(?=\n    def _add_alert)',
              src)
check(m is not None, 'could not extract _publish from contrarian.py')
if not m:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)


class FakeLog:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass


class FakePub:
    def emit(self, *a, **k): pass


ns = {'time': time, 'log': FakeLog()}
exec('class Shim:\n'
     '    def __init__(self):\n'
     '        self._last_published = {}\n'
     '        self._active_episode_keys = set()\n'
     '        self._material_delta = 0.20\n'
     '        self._heartbeat = 1800\n'
     '        self.sentiment_state = "NEUTRAL"\n'
     '        self.sentiment_score = 50\n'
     '        self._event_pub = None\n'
     '    def _comparable_value(self, st, d):\n'
     '        return d.get("rate_8h_pct")\n'
     + m.group(0), ns, ns)

s = ns['Shim']()
s._event_pub = FakePub()


def alert(pair, rate):
    return {'pair': pair, 'rate_8h_pct': rate,
            'direction': 'LONG' if rate > 0 else 'SHORT',
            'message': 'Extreme funding on %s' % pair}


# 1. A new crossing must publish (and so must log)
check(s._publish('FUNDING_EXTREME', alert('YFI/USD', 4.0)) is True,
      'a new under->over crossing must publish immediately')

# 2. Unchanged repeats must ALL be suppressed
sup = sum(0 if s._publish('FUNDING_EXTREME', alert('YFI/USD', 4.0)) else 1
          for _ in range(9))
check(sup == 9,
      'an unchanged persistent condition must be suppressed on every '
      'subsequent scan; suppressed %d of 9' % sup)

# 3. A material change must break through
check(s._publish('FUNDING_EXTREME', alert('YFI/USD', 6.0)) is True,
      'a >20%% move must publish again — suppressing it would hide a '
      'worsening condition')

# 4. A direction flip must break through
check(s._publish('FUNDING_EXTREME', alert('YFI/USD', -6.0)) is True,
      'a direction flip is always material and must publish')

# 5. A different pair must not be suppressed by another pair's episode
check(s._publish('FUNDING_EXTREME', alert('KSM/USD', 4.0)) is True,
      'dedupe is per (signal_type, pair) — a different pair crossing must '
      'publish on its own merits')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  persistent conditions log once per episode; real changes still fire')
