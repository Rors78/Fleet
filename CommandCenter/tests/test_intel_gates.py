"""A risk gate must not be satisfied by a value nobody measured.

fleet_intel_score._compute_pair builds `risk_multiplier`, which gates real
position size in PortfolioManager.reserve(). Three of its inputs carried
plausible-looking defaults:

  predictability -> 50   tested against `pred < 15`
  attractor_departure -> 0  tested against `depart > 0.5`
  confidence -> 0.5      then `risk_multiplier *= 1.4` unconditionally
  entropy -> 0.5         tested against `entropy > 0.9`

Each default sits on the safe side of its own test, so an UNMEASURED pair
sailed through every risk-reducing gate exactly as a genuinely healthy one
would — the gates failed open. The confidence case was worse than open: it
INCREASED position size by 40% on the strength of a number no engine produced.

Both directions are asserted. A gate that never fires is as broken as one that
always fires, so the real-signal cases are as important as the absent ones.
"""
import importlib
import sys

sys.path.insert(0, 'D:/CommandCenter')
import fleet_intel_score as fis
importlib.reload(fis)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


E = fis.FleetIntelScore()


def sc(data, pair='BTC/USD'):
    return E._compute_pair(pair, data)


# ── 1. Genuine signals must still fire at full strength ──
r = sc({'lorenz': {'chaotic': [{'pair': 'BTC/USD',
                                'attractor_departure': 0.8,
                                'predictability': 9.0}]}})
check(abs(r['risk_multiplier'] - 0.3) < 1e-9,
      'real chaos must apply both penalties (0.5*0.6=0.3), got %.4f'
      % r['risk_multiplier'])
check(len(r['active_warnings']) == 2,
      'both chaos warnings must be raised, got %r' % r['active_warnings'])
check('lorenz' in r['contributing_engines'],
      'a measured lorenz reading must be credited as contributing')

r = sc({'quantum': {'collapsed': [{'pair': 'BTC/USD', 'confidence': 0.95}]}})
check(abs(r['risk_multiplier'] - 1.4) < 1e-9,
      'a measured high-conviction collapse must still upsize, got %.4f'
      % r['risk_multiplier'])

r = sc({'quantum': {'superposed': [{'pair': 'BTC/USD', 'entropy': 0.95}]}})
check(abs(r['risk_multiplier'] - 0.7) < 1e-9,
      'measured high entropy must de-risk, got %.4f' % r['risk_multiplier'])
check(r['regime_type'] == 'UNCERTAIN',
      'measured high entropy must mark the regime UNCERTAIN')

# ── 2. Absent measurements must not satisfy any gate ──
r = sc({'lorenz': {'chaotic': [{'pair': 'BTC/USD'}]}})
check(r['risk_multiplier'] == 1.0,
      'an unmeasured lorenz item must not move risk_multiplier, got %.4f'
      % r['risk_multiplier'])
check('lorenz' not in r['contributing_engines'],
      'an engine that measured nothing must not claim to have contributed — '
      'contributing_engines feeds engine_agreement')
check(not r['active_warnings'],
      'no warning may be raised from unmeasured values, got %r'
      % r['active_warnings'])

r = sc({'quantum': {'collapsed': [{'pair': 'BTC/USD'}]}})
check(r['risk_multiplier'] == 1.0,
      'a collapsed item with NO confidence must not upsize position size — '
      'this branch multiplies real capital by 1.4; got %.4f'
      % r['risk_multiplier'])
check('quantum_state' not in r['contributing_engines'],
      'an unmeasured quantum item must not be credited')

r = sc({'quantum': {'superposed': [{'pair': 'BTC/USD'}]}})
check(r['risk_multiplier'] == 1.0,
      'a superposed item with NO entropy must leave risk unchanged, got %.4f'
      % r['risk_multiplier'])
check(r['regime_type'] != 'UNCERTAIN',
      'absent entropy must not be reported as measured uncertainty')
# The multiplier alone cannot catch a reintroduced `entropy` default: 0.5
# fails the `> 0.9` test, so risk stays 1.0 either way. The observable
# difference is whether the engine CLAIMS to have contributed — which feeds
# engine_agreement, and so must reflect real measurements only.
check('quantum_state' not in r['contributing_engines'],
      'a superposed item with NO entropy must not be credited as a '
      'contributing engine; got %r' % r['contributing_engines'])

# ── 2b. The FIVE sibling gates the 2026-08-06 fix missed ──
# That fix converted predictability / attractor_departure / entropy /
# confidence and stopped. A capital-risk audit on 2026-08-07 found five more
# gates in this same file with the identical shape — the default sitting on
# the safe side of its own test. Ranked by capital at risk:
#
#   ews_score          -> 0          vs `> 0.7`  (0.3x, the most aggressive
#                                                 de-risker in the file: a
#                                                 catastrophe warning with no
#                                                 score scored as no warning)
#   phase              -> "UNKNOWN"  falls through every elif INCLUDING the
#                                    explicit `LIQUID: pass` branch, so
#                                    "could not read the book" took the same
#                                    path as "the book is normal"
#   regime_change_prob -> 0          vs `> 0.7`
#   noise_ratio        -> 0          vs `> 0.7`  (fleet-wide: one dead engine
#                                                 un-scales EVERY pair at once)
#   type               -> ""         gates a 1.3x UPSIZE on an unvalidated str
FIVE = [
    ('thom/ews_score',
     {'thom': {'warnings': [{'pair': 'BTC/USD', 'ews_score': 0.9}]}},
     {'thom': {'warnings': [{'pair': 'BTC/USD'}]}}, 0.3),
    ('boltzmann/phase',
     {'boltzmann': {'phases': [{'pair': 'BTC/USD', 'phase': 'PLASMA'}]}},
     {'boltzmann': {'phases': [{'pair': 'BTC/USD'}]}}, 0.3),
    ('info_geometry/regime_change_prob',
     {'info_geometry': {'manifold_warnings': [{'pair': 'BTC/USD', 'regime_change_prob': 0.95}]}},
     {'info_geometry': {'manifold_warnings': [{'pair': 'BTC/USD'}]}}, 0.335),
    ('shannon/noise_ratio',
     {'shannon': {'noise_ratio': 0.9}}, {'shannon': {}}, 0.7),
]
for _name, _measured, _absent, _expect in FIVE:
    r_m = sc(_measured)
    check(abs(r_m['risk_multiplier'] - _expect) < 1e-9,
          '%s: a MEASURED value must still de-risk to %.3f, got %.4f — a gate '
          'that never fires is as broken as one that always fires'
          % (_name, _expect, r_m['risk_multiplier']))
    r_a = sc(_absent)
    check(r_a['risk_multiplier'] == 1.0,
          '%s: an ABSENT measurement must not move risk_multiplier, got %.4f'
          % (_name, r_a['risk_multiplier']))
    check(not r_a['contributing_engines'],
          '%s: an engine that measured NOTHING must not appear in '
          'contributing_engines — engine_agreement is 1 - warnings/engines, so '
          'claiming credit while raising no warning manufactures perfect '
          'consensus out of absence; got %r'
          % (_name, r_a['contributing_engines']))

# Prigogine is the odd one: its branch UPSIZES 1.3x rather than de-risking.
r_up = sc({'prigogine': {'structures_forming': [{'pair': 'BTC/USD', 'type': 'TREND_UP'}]}})
check(abs(r_up['risk_multiplier'] - 1.3) < 1e-9,
      'a measured TREND structure must still upsize to 1.3, got %.4f'
      % r_up['risk_multiplier'])
r_up_a = sc({'prigogine': {'structures_forming': [{'pair': 'BTC/USD'}]}})
check(r_up_a['risk_multiplier'] == 1.0,
      'an absent structure type must not upsize position size, got %.4f'
      % r_up_a['risk_multiplier'])

# ── 3. The unscored-pair contract from the earlier fix must still hold ──
E2 = fis.FleetIntelScore()
g = E2.get_score('NEVERSCORED/USD')
check(g['scored'] is False, 'an unscored pair must be marked scored=False')
check(g['risk_multiplier'] is None,
      'an unscored pair must carry risk_multiplier None, not 1.0 — "no '
      'opinion" is not "full size approved"')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  risk gates fire on measurements and stay shut on absent ones')
