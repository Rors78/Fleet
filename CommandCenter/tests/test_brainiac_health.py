"""Brainiac's collectors were invisible — a dead thread looked like a quiet market.

Brainiac is five collector threads INSIDE Command Center: no port, no
process, no entry in /api/master. Nothing reported whether those threads
were alive or whether their data was still being refreshed. A thread that
died simply stopped updating its category, and every consumer read the last
stored value as current.

That is not hypothetical: AEGIS consumed exactly this for correlation and
fabricated 0.5 when it could not reach Brainiac (fixed in af857df), which
fed the fleet's deployment cap.

/api/brainiac/health now reports MEASURED collector state. The rule this
test enforces: absence is never freshness. A category with no sample at all
must read stale, and a collector that never started must not read healthy.
"""
import sys
import time

sys.path.insert(0, 'D:/CommandCenter')

import collector as C

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def fresh_collector():
    c = C.BrainiacCollector()
    with C._latest_lock:
        C._latest_cache.clear()
    return c


# ── 1. No threads started → NOT healthy, everything stale ──
c = fresh_collector()
h = c.health()
check(h['healthy'] is False,
      'a collector with no running threads must never report healthy; got %r'
      % h['healthy'])
check(h['threads_expected'] == 0 and h['threads_alive'] == 0,
      'thread counts must be measured, got expected=%r alive=%r'
      % (h['threads_expected'], h['threads_alive']))
check(all(v['stale'] for v in h['categories'].values()),
      'a category with NO sample must read stale — absent is not fresh; '
      'got %r' % {k: v['stale'] for k, v in h['categories'].items()})
check(all(v['last_ts'] is None and v['age_s'] is None
          for v in h['categories'].values()),
      'an unsampled category must report last_ts/age None, not 0 — a zero '
      'age reads as "just updated"')

# ── 2. A fresh sample clears staleness for THAT category only ──
with C._latest_lock:
    C._latest_cache[('depth', 'BTC/USD')] = {
        'ts': time.time(), 'key': 'BTC/USD', 'data': {'imbalance': -0.5}}
h2 = c.health()
check(h2['categories']['depth']['stale'] is False,
      'a category sampled just now must not be stale; got age=%r'
      % h2['categories']['depth']['age_s'])
check(h2['categories']['funding']['stale'] is True,
      'an unsampled sibling category must STILL read stale — one healthy '
      'collector must not vouch for the others')

# ── 3. An OLD sample is stale again ──
with C._latest_lock:
    C._latest_cache[('depth', 'BTC/USD')] = {
        'ts': time.time() - 9999, 'key': 'BTC/USD', 'data': {}}
h3 = c.health()
check(h3['categories']['depth']['stale'] is True,
      'a sample far past its cadence must read stale — otherwise a dead '
      'collector is indistinguishable from a quiet market; got age=%r '
      'cadence=%r' % (h3['categories']['depth']['age_s'],
                      h3['categories']['depth']['cadence_s']))

# ── 4. Staleness is judged against each category's OWN cadence ──
# metrics/correlations/funding run every 300s; depth/trades every 60s.
# A 200s-old funding sample is fine; a 200s-old depth sample is not.
with C._latest_lock:
    C._latest_cache.clear()
    C._latest_cache[('funding', 'rates')] = {'ts': time.time() - 200,
                                             'key': 'rates', 'data': {}}
    C._latest_cache[('depth', 'BTC/USD')] = {'ts': time.time() - 200,
                                             'key': 'BTC/USD', 'data': {}}
h4 = c.health()
check(h4['categories']['funding']['stale'] is False,
      'a 200s-old funding sample is within its 300s cadence and must NOT '
      'be stale — a single global threshold would cry wolf on the slow '
      'collectors')
check(h4['categories']['depth']['stale'] is True,
      'a 200s-old depth sample is far past its 60s cadence and must be '
      'stale — a single global threshold would miss it')

# ── 5. healthy requires BOTH live threads and fresh data ──
class _T:
    def __init__(self, name, alive):
        self.name = name
        self._a = alive

    def is_alive(self):
        return self._a


c2 = fresh_collector()
c2._threads = [_T('Brainiac-Depth', True), _T('Brainiac-Funding', False)]
c2._started_at = time.time()
with C._latest_lock:
    for _cat, _k in (('depth', 'BTC/USD'), ('trades', 'BTC/USD'),
                     ('metrics', 'global'), ('correlations', 'matrix'),
                     ('funding', 'rates')):
        C._latest_cache[(_cat, _k)] = {'ts': time.time(), 'key': _k, 'data': {}}
h5 = c2.health()
check(h5['categories']['depth']['stale'] is False,
      'sanity: all categories freshly sampled')
check(h5['healthy'] is False,
      'a DEAD thread must make the collector unhealthy even when every '
      'category still has fresh data — the last sample outlives the thread '
      'that wrote it; got healthy=%r alive=%r/%r'
      % (h5['healthy'], h5['threads_alive'], h5['threads_expected']))
check(h5['threads_alive'] == 1 and h5['threads_expected'] == 2,
      'the dead thread must be counted and named, got %r' % (h5['threads'],))

# ── 6. All threads alive + all fresh → healthy ──
c3 = fresh_collector()
c3._threads = [_T('Brainiac-Depth', True)]
c3._started_at = time.time()
with C._latest_lock:
    for _cat, _k in (('depth', 'x'), ('trades', 'x'), ('metrics', 'x'),
                     ('correlations', 'x'), ('funding', 'x')):
        C._latest_cache[(_cat, _k)] = {'ts': time.time(), 'key': _k, 'data': {}}
check(c3.health()['healthy'] is True,
      'live threads plus fresh data in every category must read healthy — '
      'the check must not be permanently pessimistic')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  brainiac health is measured; absence never reads as freshness')
