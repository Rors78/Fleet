"""An unreachable Brainiac must not score the fleet SAFER than it is.

get_correlation_regime() fabricated a mid-range 0.5 at THREE separate
points — the bare `except`, the `.get("avg_abs_correlation", 0.5)` default
for a malformed 200, and a final `return 0.5` — all reading downstream as
a measurement.

The chain: rho -> corr_penalty (rho*0.15) -> safety -> the AEGIS score ->
aegis_regime() -> recommended_max_deployed() -> the fleet's deployment cap.
So a dead endpoint moved real capital limits.

Direction is what makes it a defect rather than a wash. If true correlation
is HIGH — herding, the exact regime this component exists to detect — an
invented 0.5 understates the penalty and scores the fleet SAFER, raising
the cap in the most dangerous conditions. Absence must never buy a higher
score.

Unmeasured now takes the FULL penalty (rho=1.0), the conservative end, so a
dead endpoint tightens rather than loosens. aegis.py already applies exactly
this rule to a whale alert with no score and to a missing portfolio total;
this component was simply missed.
"""
import importlib
import re
import sys

sys.path.insert(0, 'D:/Aegis')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/Aegis/aegis.py', encoding='utf-8', errors='replace').read()

# ── 1. The fabricated defaults must be gone from the fetch ──
_fn = re.search(r'def get_correlation_regime\(\):(?:.|\n)*?\n(?=\n\n#|def )', SRC)
check(_fn is not None, 'could not locate get_correlation_regime in aegis.py')
if _fn:
    body = _fn.group(0)
    check('return 0.5' not in body,
          'the unreachable-endpoint path still returns a fabricated 0.5 — '
          'that reads downstream as a measured correlation')
    check('"avg_abs_correlation", 0.5' not in body
          and "'avg_abs_correlation', 0.5" not in body,
          'a malformed 200 response still defaults to 0.5')
    check('return None' in body,
          'an unmeasured correlation must return None')
    check('DEGRADED' in body,
          'the failure must be logged with a severity keyword the stdout '
          'mirror greps for, or a dead Brainiac is silent on disk')

# ── 2. compute_aegis must take the CONSERVATIVE end when unmeasured ──
import aegis
importlib.reload(aegis)

# Identical inputs, correlation measured-low vs unmeasured.
base = dict(H=0.43, C=0.71, W=0.0, S=0.0, phi=0.15)


def score(rho):
    """Never let a defect kill the run — report it as the finding it is.

    Without the fix, rho=None raises TypeError inside compute_aegis. A test
    that dies there prints nothing and reads as 'no failures'.
    """
    try:
        return aegis.compute_aegis(rho=rho, **base)
    except Exception as e:
        return 'RAISED %s' % type(e).__name__


s_low = score(0.0)
s_mid = score(0.5)
s_high = score(1.0)
s_none = score(None)
check(not isinstance(s_none, str),
      'compute_aegis must ACCEPT an unmeasured correlation — an unguarded '
      'rho*0.15 raises on None and takes the scan loop down; got %r' % s_none)

_numeric = all(not isinstance(x, str) for x in (s_low, s_mid, s_high, s_none))
check(_numeric and s_low > s_mid > s_high,
      'sanity: higher correlation must lower the score; got %r %r %r'
      % (s_low, s_mid, s_high))
if _numeric:
    check(s_none == s_high,
          'an UNMEASURED correlation must take the FULL penalty (same as '
          'rho=1.0), not a neutral one — absence must not buy a higher '
          'score; got unmeasured=%r vs full-penalty=%r' % (s_none, s_high))
    check(s_none < s_mid,
          'the old behaviour scored unmeasured == rho 0.5 (%r); it must now '
          'be STRICTLY more conservative; got %r' % (s_mid, s_none))

    # ── 3. A real measurement must still be used verbatim ──
    check(score(0.3) != s_none,
          'a genuine low correlation must still produce a different (higher) '
          'score than the unmeasured case — the fix must not flatten real data')

    # ── 4. The change must reach the deployment cap, not just the score ──
    check(aegis.recommended_max_deployed(s_none)
          <= aegis.recommended_max_deployed(s_mid),
          'the unmeasured case must never recommend MORE deployment than the '
          'fabricated 0.5 did; got %r%% (unmeasured) vs %r%% (old default)'
          % (aegis.recommended_max_deployed(s_none),
             aegis.recommended_max_deployed(s_mid)))

# ── 5. Reporting surfaces must not crash on None ──
check('round(self.rho, 4),' not in SRC,
      'an unguarded round(self.rho, 4) raises TypeError on None and takes '
      'the whole snapshot endpoint down')

# ── 6. The dashboard must not render unmeasured as 0.000 ──
HTML = open('D:/CommandCenter/command_center_v4.html', encoding='utf-8',
            errors='replace').read()
check('var rho_val=comps.correlation!=null?+comps.correlation:0;' not in HTML,
      'the AEGIS formula panel still renders an unmeasured correlation as '
      '0.000 — "perfectly uncorrelated", the most reassuring value there is')
check('rho_known' in HTML and '"UNMEASURED"' in HTML,
      'the panel must label an unmeasured correlation rather than showing '
      'the substituted value as a reading')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  an unreachable Brainiac tightens the cap instead of loosening it')
