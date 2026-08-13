"""A level that PERSISTS is not a level that keeps being CROSSED.

Nexus's state-announcement engines published their state every scan cycle,
so the bus recorded "price is near support" over and over as if it were
"price approached support" again. Measured on the durable event log
(2026-08-13, one hour):

    EUCLID_LEVEL           2626 events,  86% byte-identical repeats
    SCHWARZSCHILD_HORIZON  1763 events,  85%
    CYCLE_DETECTED          562 events,  88%
    SHANNON_ENTROPY          60 events,  98%

BTC/USD re-announced the SAME support level (63418.0, strength 0.751) 388
times in one hour — roughly every 9 seconds, nothing changed. Three costs:
the durable log is mostly padding (which is why /api/events/recent covers
under 3 MINUTES instead of hours), any consumer counting events measures
how LONG a condition lasted rather than how often anything happened, and
real transitions are buried.

Nexus already knew the rule — QUANTUM_COLLAPSE was fixed this way on
2026-08-06 with a comment reading "a level, not an event", and
MANIFOLD_WARNING has its own gate. Both measure 0% duplicates live. The
fix was never generalized to their eight siblings.

Two things this test exists to prevent, both worse than the original bug:
  1. A gate that suppresses a TRANSACTIONAL event. Gridzilla's denial
     retries repeat an identical payload every 62s; collapsing those would
     erase the evidence that a bot is stuck in a denial loop.
  2. A gate that silently suppresses NOTHING because a continuously
     varying field (EUCLID's distance_pct) sits in the signature — it
     would look fixed while changing nothing at all.
"""
import re
import sys
import time

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/Nexus/nexus.py', encoding='utf-8', errors='replace').read()


# ── Build a minimal stand-in that runs the SHIPPED _emit_changed body ──
class _Pub:
    def __init__(self):
        self.sent = []

    def emit(self, et, data):
        self.sent.append((et, data))


_m = re.search(r'\n    def _emit_changed\(self.*?\n(?=    def _log\()', SRC, re.S)
check(_m is not None, 'could not locate _emit_changed in the shipped nexus.py')

if _m:
    import json
    import textwrap
    _ns = {'json': json, 'time': time}
    exec('class _N:\n    EMIT_REANNOUNCE_SEC = 900\n'
         + textwrap.indent(textwrap.dedent(_m.group(0)), '    '), _ns)
    _N = _ns['_N']

    def fresh():
        n = _N()
        n._emit_state = {}
        n._event_pub = _Pub()
        return n

    # 1. The same state repeated is emitted ONCE.
    n = fresh()
    for _ in range(50):
        n._emit_changed('EUCLID_LEVEL', 'BTC/USD:SUPPORT',
                        {'pair': 'BTC/USD', 'type': 'SUPPORT_APPROACHING',
                         'level': 63418.0, 'strength': 0.751,
                         'distance_pct': 0.3},
                        gate_on=('pair', 'type', 'level'))
    check(len(n._event_pub.sent) == 1,
          '50 scans of an UNCHANGED level must emit once, got %d'
          % len(n._event_pub.sent))

    # 2. THE CRITICAL CASE: a drifting field must not defeat the gate.
    # distance_pct changes every scan as price moves. Gating on the whole
    # payload would emit all 50 times — a gate that looks present and does
    # nothing, which is worse than no gate because it reads as fixed.
    n = fresh()
    for i in range(50):
        n._emit_changed('EUCLID_LEVEL', 'BTC/USD:SUPPORT',
                        {'pair': 'BTC/USD', 'type': 'SUPPORT_APPROACHING',
                         'level': 63418.0, 'strength': 0.751,
                         'distance_pct': 0.30 + i * 0.001},
                        gate_on=('pair', 'type', 'level'))
    check(len(n._event_pub.sent) == 1,
          'a DRIFTING distance_pct must not defeat the gate — the level is '
          'what makes it the same announcement; got %d emissions'
          % len(n._event_pub.sent))
    check(n._event_pub.sent[0][1]['distance_pct'] == 0.30,
          'the FULL payload must still be published, drifting field included')

    # 3. A real change emits.
    n = fresh()
    n._emit_changed('EUCLID_LEVEL', 'BTC/USD:SUPPORT',
                    {'pair': 'BTC/USD', 'type': 'SUPPORT_APPROACHING',
                     'level': 63418.0}, gate_on=('pair', 'type', 'level'))
    n._emit_changed('EUCLID_LEVEL', 'BTC/USD:SUPPORT',
                    {'pair': 'BTC/USD', 'type': 'SUPPORT_APPROACHING',
                     'level': 61200.0}, gate_on=('pair', 'type', 'level'))
    check(len(n._event_pub.sent) == 2,
          'a NEW level must emit — suppressing real transitions is the '
          'failure this fix must not cause; got %d' % len(n._event_pub.sent))

    # 4. Different keys never suppress each other.
    n = fresh()
    for p in ('BTC/USD', 'ETH/USD', 'SOL/USD'):
        n._emit_changed('EUCLID_LEVEL', p + ':SUPPORT',
                        {'pair': p, 'type': 'SUPPORT_APPROACHING',
                         'level': 100.0}, gate_on=('pair', 'type', 'level'))
    check(len(n._event_pub.sent) == 3,
          'three pairs at the same level must each emit, got %d'
          % len(n._event_pub.sent))

    # 5. Suppression EXPIRES. Without this a consumer that starts late (or
    # restarts) sees silence and cannot tell "no level" from "level never
    # repeated" — absent-vs-empty one layer down.
    n = fresh()
    pay = {'pair': 'BTC/USD', 'type': 'SUPPORT_APPROACHING', 'level': 1.0}
    n._emit_changed('EUCLID_LEVEL', 'k', pay, gate_on=('pair', 'type', 'level'))
    n._emit_state[('EUCLID_LEVEL', 'k')] = (
        n._emit_state[('EUCLID_LEVEL', 'k')][0],
        time.time() - _N.EMIT_REANNOUNCE_SEC - 1)
    n._emit_changed('EUCLID_LEVEL', 'k', pay, gate_on=('pair', 'type', 'level'))
    check(len(n._event_pub.sent) == 2,
          'an unchanged state must RE-ANNOUNCE after %ds so a late consumer '
          'still learns it; got %d emissions'
          % (_N.EMIT_REANNOUNCE_SEC, len(n._event_pub.sent)))

    # 6. A failed emission must not suppress the retry.
    class _Broken:
        def __init__(self):
            self.n = 0

        def emit(self, et, data):
            self.n += 1
            raise RuntimeError('bus down')

    n = fresh()
    n._event_pub = _Broken()
    for _ in range(3):
        n._emit_changed('EUCLID_LEVEL', 'k', {'pair': 'X', 'level': 1.0},
                        gate_on=('pair', 'level'))
    check(n._event_pub.n == 3,
          'an emission that FAILED never reached the bus, so the next scan '
          'must retry rather than suppress against it; got %d attempts'
          % n._event_pub.n)

    # 7. No publisher: no crash, no phantom success.
    n = fresh()
    n._event_pub = None
    check(n._emit_changed('X', 'k', {'a': 1}) is False,
          'with no publisher the helper must report False, not pretend')


# ── Source pins: every state engine gated, transactional paths untouched ──
for _et in ('EUCLID_LEVEL', 'SCHWARZSCHILD_HORIZON', 'NEWTON_FORCE',
            'NEWTON_REACTION', 'CYCLE_DETECTED', 'CAUSAL_FLOW', 'CHAOS_STATE',
            'STRUCTURE_FORMING', 'EINSTEIN_ENERGY', 'BOOK_PHASE',
            'SHANNON_ENTROPY'):
    check(('_event_pub.emit("%s"' % _et) not in SRC,
          '%s still emits UNGATED — it re-announces its state every scan'
          % _et)

check('NEVER route a transactional event through this' in SRC,
      'the helper must carry the transactional warning: gridzilla repeats '
      'an identical PORTFOLIO_DENIAL every 62s and those are real')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  state announcements emit on change, re-announce, and never gate trades')
