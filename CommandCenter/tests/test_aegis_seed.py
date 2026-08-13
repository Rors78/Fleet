"""A score no scan produced must not set the fleet's deployment cap.

AegisEngine seeded self.score = 0.5 / regime = "NORMAL" at __init__, and
0.5 is NOT a neutral placeholder: it lands exactly on the NORMAL tier, and
command_center._apply_aegis_adjustment maps score -> tier -> cap without
checking status or cycle.

Observed live, in the fleet log during an AEGIS restart:

  2026-08-13 04:15:26Z INFO AEGIS: raise to 80% pending 20min hold (score=0.5000)

That is a request to raise the fleet cap to 80% derived from an
initialization constant. The 20-minute raise hold happened to absorb it —
a real 0.2259 replaced it five minutes later — but that hold exists to damp
genuine tier spikes, not to filter fabricated values, and a cap DROP
applies instantly with no hold at all.

Fixed on both sides, because neither should depend on the other behaving:
AEGIS publishes score None until a scan computes one, and CC refuses to
adjust while AEGIS reports initializing / cycle < 1.
"""
import importlib
import re
import sys

sys.path.insert(0, 'D:/Aegis')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def safe(fn, *a):
    """Call fn, returning 'RAISED <Type>' instead of propagating.

    Written after this same flaw bit four separate tests this session: the
    REVERTED code raises on the very input the fix exists to handle, so an
    unguarded call kills the run before any assertion prints and the red
    proof reads as 'no failures'. A drill must report the defect, not die
    of it.
    """
    try:
        return fn(*a)
    except Exception as e:
        return 'RAISED %s' % type(e).__name__


ASRC = open('D:/Aegis/aegis.py', encoding='utf-8', errors='replace').read()
CSRC = open('D:/CommandCenter/command_center.py', encoding='utf-8',
            errors='replace').read()

# ── 1. The seed is gone ──
check(re.search(r'def __init__\(self\):[\s\S]{0,900}?self\.score = 0\.5',
                ASRC) is None,
      'AegisEngine still seeds score 0.5 — that value maps straight to the '
      'NORMAL tier and an 80% cap')
check(re.search(r'def __init__\(self\):[\s\S]{0,1200}?self\.score = None',
                ASRC) is not None,
      'score must start as None until a scan computes one')

# ── 2. Absence takes the DEFENSIVE cap, never the loosest ──
import aegis
importlib.reload(aegis)

_capNone = safe(aegis.recommended_max_deployed, None)
check(_capNone == 30,
      'an unmeasured score must recommend the most DEFENSIVE cap — absence '
      'must never buy deployment headroom; got %r' % (_capNone,))
_regNone = safe(aegis.aegis_regime, None)
check(_regNone is None,
      'an unmeasured score has no regime — "NORMAL" would be a lie; got %r'
      % (_regNone,))

# Real scores must still map exactly as before — the guard must not shift
# any live band.
for _s, _cap in ((0.85, 90), (0.5, 80), (0.2, 60), (0.19, 30), (0.0, 30)):
    _got = safe(aegis.recommended_max_deployed, _s)
    check(_got == _cap,
          'score %r must still map to %r%%, got %r' % (_s, _cap, _got))

# ── 3. A fresh engine publishes no score at all ──
eng = aegis.AegisEngine.__new__(aegis.AegisEngine)
_init = safe(aegis.AegisEngine.__init__, eng)
check(not isinstance(_init, str),
      'constructing the engine must not raise — recommended_max_deployed '
      'runs at init and would blow up on an unguarded None; got %r' % _init)
check(getattr(eng, 'score', 'MISSING') is None
      and getattr(eng, 'regime', 'MISSING') is None,
      'a freshly constructed engine must have no score/regime, got %r/%r'
      % (getattr(eng, 'score', 'MISSING'), getattr(eng, 'regime', 'MISSING')))
snap = safe(eng.snapshot)
if isinstance(snap, str):
    check(False, 'snapshot() must not raise before the first scan; got %s' % snap)
    snap = {}
check(snap.get('score') is None,
      'the snapshot CC polls must not carry a fabricated score before the '
      'first scan; got %r' % snap.get('score'))
check(snap.get('status') == 'initializing' and snap.get('cycle') == 0,
      'the snapshot must disclose that no scan has run yet, got status=%r '
      'cycle=%r' % (snap.get('status'), snap.get('cycle')))
check(snap.get('recommended_max_deployed') == 30,
      'even the pre-scan recommendation must be the defensive floor, got %r'
      % snap.get('recommended_max_deployed'))

# ── 4. The STALE path must not publish a pre-scan score ──
# When CC is unreachable on the very first cycle there is no "last known"
# value to fall back on.
check(re.search(r'if self\._event_pub and self\.score is not None:', ASRC)
      is not None,
      'the DEGRADED/stale publish path must be gated on a real score — '
      'before the first scan there is no last-known value to republish')

# ── 5. Command Center refuses to adjust from a pre-scan report ──
check('AEGIS not yet scanned' in CSRC,
      'the cap adjuster must skip while AEGIS reports initializing/cycle<1 '
      '— it must not depend on AEGIS publishing None correctly')
_adj = re.search(r'def _apply_aegis_adjustment\(\):[\s\S]*?\n    score = ',
                 CSRC)
check(_adj is not None and 'status' in _adj.group(0)
      and 'cycle' in _adj.group(0),
      'the status/cycle check must run BEFORE the score is read and mapped '
      'to a tier')
check(re.search(r'score != score', CSRC) is not None,
      'a NaN score must be rejected — every tier comparison against NaN is '
      'False, which silently selects the DEFENSIVE branch by accident '
      'rather than by decision')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  no cap change from a score no scan produced')
