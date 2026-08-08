"""Pearson of a constant P/L stream is undefined, not 0.0.

/api/fleet/correlations served a full 15-pair matrix of exactly 0.0000 with
status "active" and concentration_risk false. Not one of those was a
measurement: four of six trader bots report a CONSTANT pnl between closes,
so every delta series was all-zero, and Pearson of a zero-variance series is
0/0 — mathematically undefined. A `+ 1e-10` epsilon in the denominator
quietly turned every undefined pair into a confident "uncorrelated" reading,
and the concentration-risk check read "measured and safe" forever.

The dashboard had the same collapse one layer up: a missing/None matrix cell
fell back to 0 and rendered "0.00".

Undefined pairs now carry None, the payload discloses pairs_measured /
pairs_undefined and a coverage note when nothing was measurable, and the
matrix cell renders a dash.
"""
import importlib
import sys

sys.path.insert(0, 'D:/CommandCenter')
import command_center as cc

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def feed(streams):
    with cc._bot_correlation_lock:
        cc._bot_pnl_history.clear()
    n = max(len(v) for v in streams.values())
    result = None
    for k in range(n):
        bots_data = {
            bid: {'alive': True, 'normalized': {'pnl': vals[k]}}
            for bid, vals in streams.items() if k < len(vals)
        }
        result = cc._compute_bot_correlations(bots_data)
    # Leave no residue for other tests / the live poll thread.
    with cc._bot_correlation_lock:
        cc._bot_pnl_history.clear()
    return result


# ── 1. The live fleet shape: constant streams must be UNDEFINED ──
r = feed({'gridzilla': [50.0] * 40,
          'rubberband': [0.0] * 40,
          'turtlesue': [0.0] * 40})
check(all(v is None for v in r['matrix'].values()),
      'every pair of constant streams must be None — the old epsilon '
      'rendered them all 0.0, a full matrix of fabricated "uncorrelated" '
      'readings; got %r' % r['matrix'])
check(r.get('pairs_undefined') == 3 and r.get('pairs_measured') == 0,
      'the payload must disclose how much of the matrix is real, got '
      'measured=%r undefined=%r'
      % (r.get('pairs_measured'), r.get('pairs_undefined')))
check(bool(r.get('coverage_note')),
      'with nothing measurable, the payload must say so — '
      'concentration_risk=False over an all-undefined matrix is "no risk '
      'DETECTED", not "no risk"')

# ── 2. A genuinely correlated pair must still measure ──
base = [float(i + 7 * ((i * 13) % 5)) for i in range(40)]
r2 = feed({'gridzilla': base,
           'confluence': [b * 1.5 + 3 for b in base],
           'rubberband': [0.0] * 40})
c = r2['matrix'].get('confluence|gridzilla')
check(c is not None and c > 0.99,
      'an affine copy must read ~1.0 — a guard that blanks real '
      'correlations is the opposite failure; got %r' % c)
check(r2['matrix'].get('gridzilla|rubberband') is None,
      'the constant stream must stay undefined even beside moving ones')
check(r2.get('pairs_measured') == 1 and r2.get('pairs_undefined') == 2,
      'mixed matrix must disclose the split, got measured=%r undefined=%r'
      % (r2.get('pairs_measured'), r2.get('pairs_undefined')))
check(r2.get('coverage_note') is None,
      'with at least one real measurement the coverage note must be absent')

# ── 3. Anti-correlation must still measure AND alert ──
r3 = feed({'gridzilla': base, 'confluence': [-b for b in base]})
c3 = r3['matrix'].get('confluence|gridzilla')
check(c3 is not None and c3 < -0.99,
      'perfect anti-correlation must read ~-1.0, got %r' % c3)
check(any(abs(a.get('correlation', 0)) > 0.85 for a in r3.get('alerts', [])),
      'the |corr| > 0.85 concentration alert must still fire on a real '
      'measurement')

# ── 4. The dashboard must render undefined as a dash, not 0.00 ──
html = open('D:/CommandCenter/command_center_v4.html', encoding='utf-8',
            errors='replace').read()
check('matrix[key1]!==undefined?matrix[key1]:matrix[key2]!==undefined' in html,
      'the matrix cell must distinguish a null value from a missing key — '
      'the old fallback-to-0 rendered undefined pairs as "0.00"')
check('matrix[key1]!=null?matrix[key1]:matrix[key2]!=null?matrix[key2]:0'
      not in html,
      'the fallback-to-0 matrix cell is back in the shipped dashboard')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  undefined correlations stay undefined; real ones still measure and alert')
