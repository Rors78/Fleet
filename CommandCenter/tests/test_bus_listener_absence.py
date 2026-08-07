"""Bus helpers must distinguish "no data" from a measured value.

Three helpers in bus_listener.py returned a plausible constant when they had
nothing:

    manifold_reliable  -> .get("model_reliability", 1.0) > 0.5
    chaos_confidence   -> .get("confidence", 0.5) if state else 0.5
    lyapunov           -> .get("lyapunov_exponent", 0) if state else 0

The first is the worst. A MANIFOLD_WARNING exists precisely because something
is objecting to the model; if its reliability figure is missing, the old code
scored it 1.0 — the maximum — so the one case where something is
demonstrably wrong sailed through the `> 0.5` gate as "reliable". nexus now
publishes a real figure and 0.0 is a legitimate value there (live: 0.611 /
0.434 / 0.264 / 0.000), so a missing key means absence, never zero and never
one.

The other two returned values that are legitimate measurements in their own
right: 0.5 is a real confidence, and 0 is a real Lyapunov exponent for a
marginally stable system. Returning them for "we have no idea" made the two
cases indistinguishable to every caller.
"""
import sys

sys.path.insert(0, 'D:/CommandCenter')
import bus_listener as bl

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


class Fake(bl.BusListener):
    """Serve scripted bus payloads without touching the network."""

    def __init__(self, payloads):
        self._payloads = payloads

    def _latest_data(self, etype, pair=None, max_age=300):
        return self._payloads.get(etype)


# ── manifold_reliable ──
# 1. No warning at all: nothing is objecting.
check(Fake({}).manifold_reliable('BTC/USD') is True,
      'with no MANIFOLD_WARNING the manifold must read reliable')

# 2. Warning with a good figure.
check(Fake({'MANIFOLD_WARNING': {'model_reliability': 0.9}}).manifold_reliable('BTC/USD') is True,
      'a warning with high measured reliability must read reliable')

# 3. Warning with a bad figure.
check(Fake({'MANIFOLD_WARNING': {'model_reliability': 0.2}}).manifold_reliable('BTC/USD') is False,
      'a warning with low measured reliability must read unreliable')

# 4. A genuine 0.0 must be honoured, not treated as missing.
check(Fake({'MANIFOLD_WARNING': {'model_reliability': 0.0}}).manifold_reliable('BTC/USD') is False,
      'model_reliability 0.0 is a real measurement (live ZEC/USD) and must '
      'read unreliable')

# 5. THE DEFECT: a warning we cannot grade is not a clean bill of health.
check(Fake({'MANIFOLD_WARNING': {'pair': 'BTC/USD'}}).manifold_reliable('BTC/USD') is False,
      'a MANIFOLD_WARNING with NO model_reliability must not read as '
      'reliable — the old 1.0 default scored the ungradeable case perfect')

# ── chaos_confidence ──
check(Fake({}).chaos_confidence('BTC/USD') is None,
      'no chaos state must return None, not a fabricated 0.5')
check(Fake({'CHAOS_STATE': {'pair': 'BTC/USD'}}).chaos_confidence('BTC/USD') is None,
      'a chaos state with no confidence must return None')
check(Fake({'CHAOS_STATE': {'confidence': 0.5}}).chaos_confidence('BTC/USD') == 0.5,
      'a MEASURED 0.5 must be returned as 0.5 — it is a real value and must '
      'stay distinguishable from absence')
check(Fake({'CHAOS_STATE': {'confidence': 0.93}}).chaos_confidence('BTC/USD') == 0.93,
      'a measured confidence must pass through unchanged')

# ── lyapunov ──
check(Fake({}).lyapunov('BTC/USD') is None,
      'no chaos state must return None, not 0 — 0 is a real Lyapunov value')
check(Fake({'CHAOS_STATE': {'lyapunov_exponent': 0}}).lyapunov('BTC/USD') == 0,
      'a MEASURED 0 exponent must be returned as 0')
check(Fake({'CHAOS_STATE': {'lyapunov_exponent': 0.143}}).lyapunov('BTC/USD') == 0.143,
      'a measured exponent must pass through unchanged')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  bus helpers report absence as None; measured values pass through')
