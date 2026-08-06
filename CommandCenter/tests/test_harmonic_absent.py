"""Absent timeframes must not vote, and must not manufacture confidence.

Two real defects are locked down here.

1. PythagoreanResonance.compute() classified each timeframe with `s > 0.5`.
   A missing timeframe defaulted to exactly 0.5, and `0.5 > 0.5` is False —
   so an ABSENT timeframe cast a silent BEARISH vote and was then counted as
   an agreeing voice in `consonance`. With no PHITEX data at all the engine
   returned resolution_bias BEAR at consonance 1.0: its strongest possible
   reading, from five timeframes that were never measured.

2. InformationGeometryEngine._default() — the INSUFFICIENT_DATA path —
   returned model_reliability 1.0, the MAXIMUM value. The one branch that
   knows it failed to measure anything was the branch claiming a perfect
   model fit, and nexus.py published that onto the bus in MANIFOLD_WARNING.

Both directions are asserted: the defect is gone AND a genuine signal
still fires unchanged.
"""
import re
import sys
import os

sys.path.insert(0, "D:/CommandCenter")

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── Load PythagoreanResonance from nexus.py without booting the bot ──
_src = open("D:/Nexus/nexus.py", encoding="utf-8", errors="replace").read()
_m = re.search(r"def _mean_or_none.*?\nclass PythagoreanResonance:.*?\n(?=\n# -{10})",
               _src, re.S)
if not _m:
    print("FAIL  could not extract PythagoreanResonance from nexus.py")
    sys.exit(1)
_ns = {}
exec(_m.group(0), _ns)
Pyth = _ns["PythagoreanResonance"]
mean_or_none = _ns["_mean_or_none"]

p = Pyth()

# ── 1. The defect must be gone ──
r = p.compute("XRP/USD", {})
check(r["resolution_bias"] == "UNMEASURED",
      "zero measured timeframes must not yield a directional bias, got %r"
      % r["resolution_bias"])
check(r["consonance"] is None,
      "zero measured timeframes must not yield a consonance number, got %r"
      % r["consonance"])
check(r["measured"] is False, "measured flag must be False with no data")

# A single timeframe cannot resonate with anything.
r1 = p.compute("ADA/USD", {"5m": 0.7})
check(r1["resolution_bias"] == "UNMEASURED",
      "one timeframe cannot produce a harmonic reading, got %r" % r1["resolution_bias"])

# ── 2. A genuine signal must survive untouched ──
r5 = p.compute("BTC/USD", {"5m": .70, "15m": .72, "1h": .68, "4h": .66, "1d": .71})
check(r5["resolution_bias"] == "BULL", "five real bullish TFs must read BULL")
check(r5["consonance"] == 1.0,
      "five agreeing measured TFs must read consonance 1.0, got %r" % r5["consonance"])
check(r5["measured"] is True, "measured flag must be True with real data")

# Real dissonance must still be detected and named.
r4 = p.compute("ETH/USD", {"5m": .70, "15m": .72, "1h": .20, "4h": .66, "1d": .71})
check(r4["resolution_bias"] == "BULL", "4-1 bullish must still read BULL")
check(r4["dissonant_tf"] == ["1h"],
      "the genuinely dissonant timeframe must be named, got %r" % r4["dissonant_tf"])
check(abs(r4["consonance"] - 0.8) < 1e-9,
      "4 of 5 agreeing must read 0.8, got %r" % r4["consonance"])

# Absent timeframes must not dilute a real, fully-aligned signal.
r3 = p.compute("SUI/USD", {"5m": .70, "15m": .72, "1h": .68})
check(r3["consonance"] == 1.0,
      "3 measured, fully aligned, must read 1.0 not 0.6 — absent TFs must not "
      "dilute a real signal, got %r" % r3["consonance"])
check(r3["tf_measured"] == ["5m", "15m", "1h"],
      "tf_measured must name only measured timeframes, got %r" % r3["tf_measured"])

# ── 3. Fleet mean must distinguish "none measured" from zero ──
check(mean_or_none([]) is None,
      "empty fleet consonance must be None, not 0 — 0 reads as a measurement")
check(mean_or_none([1.0, 0.6]) == 0.8, "fleet mean of measured values must be exact")

# ── 4. info_geometry: INSUFFICIENT_DATA must not claim reliability ──
import importlib
import info_geometry
importlib.reload(info_geometry)
eng = info_geometry.InformationGeometryEngine()

d = eng._default()
check(d["model_reliability"] is None,
      "INSUFFICIENT_DATA must report model_reliability None, not a number — "
      "got %r" % d["model_reliability"])
check(d["model_reliability"] != 1.0,
      "INSUFFICIENT_DATA must never claim MAXIMUM reliability")
check(d.get("measured") is False, "_default must be flagged unmeasured")

# ── 5. model_reliability must not be saturated dead across the live range ──
# Live fisher_total on real pairs runs ~0.08 (calm) to ~0.60 (elevated).
# The old `1 - f*10` scale hit 0 at f=0.10, making the field a constant 0.
import random
random.seed(11)
_px, _c = 100.0, []
for i in range(300):
    _px *= (1 + random.gauss(0, 0.004))
    _c.append([i, _px, _px * 1.002, _px * 0.998, _px, 1000])
real = eng.compute("TEST/USD", _c)
check(real.get("measured") is True, "a real compute must be flagged measured")

# Read the live scale out of the source rather than restating it here — an
# inline `1 - f*1.6` would assert this test's own arithmetic and stay green
# even if the shipped constant regressed to the saturating `* 10`.
_igsrc = open("D:/CommandCenter/info_geometry.py", encoding="utf-8").read()
_scale_m = re.search(r"'model_reliability':\s*round\(max\(0,\s*1\s*-\s*fisher_total\s*\*\s*([\d.]+)\)",
                     _igsrc)
check(_scale_m is not None, "could not locate the model_reliability scale in source")
if _scale_m:
    _scale = float(_scale_m.group(1))
    vals = [max(0.0, 1 - f * _scale) for f in (0.08, 0.247, 0.354, 0.46)]
    check(len(set(vals)) == len(vals),
          "model_reliability must vary across the live fisher range, not saturate. "
          "scale=%s gives %r" % (_scale, vals))
    check(all(v > 0 for v in vals),
          "no live-range fisher value (0.08-0.46) may floor model_reliability to 0 — "
          "that makes the field a dead constant. scale=%s gives %r" % (_scale, vals))

# ── 6. PHITEX magnitudes must not be read as directions ──
# PHITEX state variables are normalized MAGNITUDES. `temperature` is >0.5 on
# every live pair; `chi_norm`/`phi_tex` are >0.5 on almost none. Feeding them
# straight in made the direction pattern an artefact of each variable's
# distribution: every top pair read a confident BEAR while PHITEX itself
# reported NEUTRAL/EQUILIBRIUM on 48 of 56 pairs.
_nxsrc = open("D:/Nexus/nexus.py", encoding="utf-8", errors="replace").read()
check('_dir = str(pdata.get("direction") or "").upper()' in _nxsrc,
      "the PHITEX proxy must take direction from PHITEX's `direction` field, "
      "not infer it from magnitude thresholds")
check("abs(_v) > 1e-9" in _nxsrc,
      "zero-magnitude timeframes must abstain — mapping them to 0.5 casts a "
      "silent bear vote, which inverted live LTC/USD and PUMP/USD")

# Replicate the shipped proxy against the two shapes that actually broke.
SRC = {"5m": "chi_norm", "15m": "C_norm", "1h": "temperature",
       "4h": "FCI_norm", "1d": "phi_tex"}


def proxy(pdata):
    dr = str(pdata.get("direction") or "").upper()
    if dr in ("LONG", "BULL", "UP"):
        sign = 1.0
    elif dr in ("SHORT", "BEAR", "DOWN"):
        sign = -1.0
    else:
        return {}
    out = {}
    for tf, k in SRC.items():
        v = pdata.get(k)
        if isinstance(v, (int, float)) and abs(v) > 1e-9:
            out[tf] = max(0.0, min(1.0, 0.5 + sign * abs(v) * 0.5))
    return out


# A NEUTRAL pair in equilibrium must produce no directional claim at all.
neutral = {"direction": "NEUTRAL", "chi_norm": 0.05, "C_norm": 0.0,
           "temperature": 0.914, "FCI_norm": 0.53, "phi_tex": 0.0}
rn = p.compute("AAVE/USD", proxy(neutral))
check(rn["resolution_bias"] == "UNMEASURED",
      "a PHITEX-NEUTRAL pair must not be assigned a direction, got %r"
      % rn["resolution_bias"])

# Live LTC/USD: PHITEX LONG, three 0.0000 magnitudes. Must not invert to BEAR.
ltc = {"direction": "LONG", "chi_norm": 0.0, "C_norm": 0.0,
       "temperature": 0.9527, "FCI_norm": 0.59, "phi_tex": 0.0}
rl = p.compute("LTC/USD", proxy(ltc))
check(rl["resolution_bias"] == "BULL",
      "PHITEX LONG with zero-conviction timeframes must not invert to BEAR, "
      "got %r" % rl["resolution_bias"])
check(rl["tf_measured"] == ["1h", "4h"],
      "only the timeframes with real conviction may vote, got %r"
      % rl["tf_measured"])

# A SHORT pair must still resolve bearish.
short = {"direction": "SHORT", "chi_norm": 0.0, "C_norm": 0.31,
         "temperature": 0.95, "FCI_norm": 0.42, "phi_tex": 0.0}
rs = p.compute("CC/USD", proxy(short))
check(rs["resolution_bias"] == "BEAR",
      "a PHITEX SHORT pair must resolve BEAR, got %r" % rs["resolution_bias"])

if FAIL:
    for f in FAIL:
        print("FAIL  " + f)
    sys.exit(1)
print("ok  absent timeframes do not vote; unmeasured data claims no confidence")
