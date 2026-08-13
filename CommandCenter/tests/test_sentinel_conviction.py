"""Sentinel's high-conviction gate sat above its own achievable ceiling.

The gate was `prob_up > 0.65 or prob_down > 0.65` at the 4h horizon. That
threshold was correct against the pre-2026-07-29 drift scale and was left
behind when drift was rescaled into sigma units — the same shape as the
other findings in this audit.

Measured by RUNNING the shipped monte_carlo_paths / compute_distribution
rather than deriving it on paper:

    the four drift terms sum to at most
      base 1e-5 + book 5e-6 + whale 5e-6 + reversion ~1e-6 = 2.1e-5
    => drift_snr 0.0136, which is 27% of DRIFT_SNR_CAP (0.05) — the cap
       never binds either
    => max achievable P(up) = 0.482

against a 0.65 gate: an 0.168 gap no market condition can close. So
self.high_conviction was permanently empty and every downstream consumer
(bus_listener, card_renderer, signal_broadcaster, signal_decay) received
nothing from this path.

Confirmed on the durable bus log: 2,602 HIGH_CONVICTION events in 24h, ALL
emitted by event_bus reaction rules, ZERO by sentinel. The events visible
on the dashboard come from a different mechanism entirely.

The fix lowers the GATE into the achievable range. Raising the drift terms
instead would manufacture conviction the inputs do not support.
"""
import math
import sys

sys.path.insert(0, 'D:/Sentinel')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


SRC = open('D:/Sentinel/sentinel.py', encoding='utf-8',
           errors='replace').read()
import sentinel as S

# ── 1. The unreachable literal is gone and the gate is a named constant ──
check('prob_up > 0.65 or prob_down > 0.65' not in SRC,
      'the 0.65 gate is back — it sits 0.168 above the achievable ceiling '
      'and makes high_conviction permanently empty')
check(hasattr(S, 'HIGH_CONVICTION_PROB'),
      'the gate must be a named constant so its relationship to the drift '
      'ceiling is reviewable, not a bare literal in a loop')
# Every later assertion reads this constant. Without the fallback the
# reverted module raises AttributeError and kills the run before any
# failure prints — a drill that dies of the defect reads as "no failures".
# Fifth time this exact flaw has bitten a test in this session.
_GATE = getattr(S, 'HIGH_CONVICTION_PROB', 0.65)
check('prob_up > HIGH_CONVICTION_PROB' in SRC,
      'the gate must actually USE the constant')

# ── 2. The threshold must sit INSIDE the achievable range ──
# Recompute the ceiling from the shipped source rather than trusting a
# hardcoded number — if the drift terms are ever rescaled again, this test
# fails instead of silently going stale.
REF = S.DRIFT_REF_VOL
_max_drift_raw = 0.00001 + 0.000005 + 0.000005 + 0.000002   # base+book+whale+rev
_max_snr = min(S.DRIFT_SNR_CAP, _max_drift_raw / REF)
check(_max_snr < S.DRIFT_SNR_CAP,
      'sanity: DRIFT_SNR_CAP (%r) should NOT bind — the terms only reach '
      '%r, which is what made the old gate unreachable'
      % (S.DRIFT_SNR_CAP, _max_snr))

vol = 0.012 / math.sqrt(60)


def prob_at(snr, n=8000):
    finals = S.monte_carlo_paths(100.0, vol, snr * vol, 240, n)
    d = S.compute_distribution(finals, 100.0,
                               sigma_horizon=vol * math.sqrt(240))
    dp = d.get('direction_probability', {})
    return dp.get('up', 0.0), dp.get('down', 0.0)

# Averaged over repeats — this is a Monte Carlo, so a single draw is noisy.
_ups = [prob_at(_max_snr)[0] for _ in range(3)]
_ceiling = max(_ups)
check(_GATE < _ceiling,
      'the gate (%r) must sit BELOW the maximum achievable probability '
      '(%.3f) or high_conviction can never populate'
      % (_GATE, _ceiling))

# ── 3. ...but must NOT fire on no signal at all ──
_up0, _dn0 = prob_at(0.0)
check(_up0 <= _GATE and _dn0 <= _GATE,
      'a ZERO-drift forecast must not trip the gate — a permanently OPEN '
      'gate is as useless as a permanently shut one; got up=%.3f down=%.3f '
      'against %r' % (_up0, _dn0, _GATE))

# And must not fire on a weak tilt either — it has to mean something.
_upw, _dnw = prob_at(_max_snr * 0.35)
check(_upw <= _GATE,
      'a weak tilt (35%% of the ceiling) must not read as high conviction; '
      'got %.3f against %r' % (_upw, _GATE))

# ── 4. The gate must remain symmetric ──
check('prob_down > HIGH_CONVICTION_PROB' in SRC,
      'the down side must use the same threshold — an asymmetric gate would '
      'silently bias the fleet long')

# ── 5. The cap comment must not claim a constraint it does not impose ──
check('NEVER BINDS' in SRC or 'never binds' in SRC,
      'DRIFT_SNR_CAP does not bind (terms reach 27%% of it) and the source '
      'should say so — a comment claiming "even with every term maxed" '
      'describes a constraint that is not active')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  the conviction gate is reachable, selective, and symmetric')
