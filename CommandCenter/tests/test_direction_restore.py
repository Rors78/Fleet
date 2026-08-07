"""A persisted SHORT must never come back as a LONG.

Three bots restore open positions from disk after a crash or restart, and all
three defaulted a missing `direction` to "LONG":

    confluence.py    pd.get("direction", "LONG")
    nexus_brain.py   d.get("direction", "LONG")
    gridzilla        (grids are long-only by construction — not affected)

The LONG fallback was deliberate and documented: legacy state files predate
shorts and carried only longs. But it was SILENT, and current state files all
record a direction, so a missing one now means corruption rather than age.
Quietly restoring a live SHORT as a LONG inverts its P/L, its stop and its
target — and a restart is exactly the moment nobody is watching.

The fallback is kept for genuine legacy files. What changed is that it now
announces itself, naming the pair and the consequence.

A second instance of the same shape: nexus_brain._signal_changed compared a
new signal's direction against `prev.get("direction", "LONG")`. When the
stored signal had no direction, that answered "did it flip?" with a guess —
a genuine SHORT read as a flip (spurious alert) and a LONG read as unchanged
(suppressed alert).
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── confluence: fallback kept, but must warn ──
cf = open('D:/Confluence/confluence.py', encoding='utf-8', errors='replace').read()
check('pd.get("direction", "LONG")' not in cf,
      'confluence still silently defaults a restored position to LONG')
check('has NO direction' in cf,
      'confluence must WARN when it falls back to LONG — a silent inversion '
      'of a live short is the whole defect')
check('_pdir = pd.get("direction")' in cf,
      'confluence must read the direction explicitly before deciding')

# ── nexus_brain: same contract ──
nb = open('D:/NexusBrain/nexus_brain.py', encoding='utf-8', errors='replace').read()
check('direction=d.get("direction", "LONG")' not in nb,
      'nexus_brain still silently defaults a restored position to LONG')
check('def _restored_direction' in nb,
      'nexus_brain must route restores through a helper that logs the fallback')
check('has NO direction' in nb,
      'nexus_brain must WARN when it falls back to LONG')

# ── nexus_brain: the change-detection gate ──
check('prev.get("direction", "LONG")' not in nb,
      '_signal_changed still compares against a DEFAULTED previous direction, '
      'which answers "did it flip?" with a guess')
check('_prev_dir is None or signal.direction != _prev_dir' in nb,
      'an unknown previous direction must count as changed — we cannot rule '
      'out a flip, and suppressing a real flip is worse than one extra alert')

# ── Behavioural: replicate the shipped restore decision ──
warnings = []


def restored_direction(pair, d):
    """Mirrors nexus_brain._restored_direction / confluence's inline form."""
    direction = d.get('direction')
    if direction:
        return direction
    warnings.append(pair)
    return 'LONG'


check(restored_direction('XRP/USD', {'direction': 'SHORT'}) == 'SHORT',
      'a persisted SHORT must be restored as SHORT — this is the inversion '
      'the whole fix exists to prevent')
check(restored_direction('BTC/USD', {'direction': 'LONG'}) == 'LONG',
      'a persisted LONG must be restored as LONG')
check(not warnings,
      'a well-formed state file must produce NO warnings, got %r — a fix that '
      'cries wolf on every restart gets ignored' % warnings)

check(restored_direction('OLD/USD', {}) == 'LONG',
      'a legacy record with no direction still falls back to LONG')
check(warnings == ['OLD/USD'],
      'the fallback must name the affected pair, got %r' % warnings)

# ── Behavioural: the change gate ──
def signal_changed(prev, new_direction):
    prev_dir = prev.get('direction')
    return prev_dir is None or new_direction != prev_dir


check(signal_changed({'direction': 'LONG'}, 'SHORT') is True,
      'a real flip must register as changed')
check(signal_changed({'direction': 'LONG'}, 'LONG') is False,
      'an unchanged direction must not register as changed — otherwise every '
      'scan re-alerts')
check(signal_changed({}, 'SHORT') is True,
      'an unknown previous direction must count as changed; the old default '
      'compared against a fabricated LONG')
check(signal_changed({}, 'LONG') is True,
      'an unknown previous direction must count as changed even when the new '
      'signal is LONG — the old default silently SUPPRESSED this case')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  persisted shorts survive restart; silent LONG assumptions are gone')
