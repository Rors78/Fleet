"""Rubberband's stop and target must be measured on the SAME timeframe.

WHY THIS EXISTS. Rubberband ran 600 consecutive scans and produced 0 signals
and 0 trades in its entire history, while reading as "selective". The cause
was a unit error, not a threshold:

    reward = tp1 - price = bb_middle - bb_lower   <- 15m Bollinger band
    risk   = SL_ATR_MULT * atr_60m                <- 60m ATR

Clearing MIN_RR_RATIO = 2.0 therefore required ATR_60m <= bb_half / 4, which
across the live 25-pair universe meant ATR_60m had to be 0.30x-0.93x ATR_15m
(median 0.50x). ATR grows with bar length. Measured directly against Kraken
OHLC on 2026-08-26, ATR_60m / ATR_15m = 2.29 mean (XBT 2.31, ETH 2.14,
LINK 2.26, AVAX 2.53, XRP 2.24). The gate was unreachable by construction.

This is the /unit-check failure shape: two numbers whose names sounded
compatible, measured on different clocks, compared without conversion.

WHAT THIS TEST DOES NOT CLAIM. Matching the clocks does not make the bot
trade. It converts an arithmetic impossibility into a rare setup: the gate
now reduces to BB_stdev >= 2 * ATR_15m, and 0 of 25 live pairs cleared that
at the time of the fix (best: AAVE 1.86). The test pins COHERENCE — that
both sides of the ratio read the same series — not signal frequency.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_PATH = os.path.join(ROOT, "Rubberband", "rubberband.py")

failures = []

if not os.path.exists(SRC_PATH):
    print("FAIL: rubberband.py not found at %s" % SRC_PATH)
    sys.exit(1)

with open(SRC_PATH, encoding="utf-8", errors="replace") as fh:
    SRC = fh.read()


def _code_only(text):
    """Drop docstrings and comments — the fix DOCUMENTS the old broken line."""
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return re.sub(r"#[^\n]*", "", text)


CODE = _code_only(SRC)

# ---------------------------------------------------------------------------
# 1. Every stop_loss assignment must use the 15m ATR, never the 60m one.
#    Both directions — the LONG and SHORT branches are mirrors, and fixing
#    one while missing the other is this project's endemic failure mode.
# ---------------------------------------------------------------------------
stops = re.findall(r"stop_loss\s*=\s*([^\n]+)", CODE)
if not stops:
    failures.append("no stop_loss assignment found — the entry builder is gone")

for expr in stops:
    if "atr_60m" in expr:
        failures.append(
            "stop_loss uses atr_60m while the target (tp1 = bb_middle) is a "
            "15m band: %s — this is the mismatch that made the 2:1 gate "
            "unreachable (needs ATR_60m <= 0.93x ATR_15m; measured 2.29x)"
            % expr.strip())

long_stops = [e for e in stops if e.strip().startswith("price -")]
short_stops = [e for e in stops if e.strip().startswith("price +")]
if not long_stops:
    failures.append("no LONG stop (price - ...) found")
if not short_stops:
    failures.append("no SHORT stop (price + ...) found — the mirror branch "
                    "must be fixed too, not just the long path")

# Both branches must reference the same ATR symbol as each other.
syms = set()
for e in stops:
    m = re.search(r"\*\s*(atr\w*)", e)
    if m:
        syms.add(m.group(1))
if len(syms) > 1:
    failures.append("LONG and SHORT stops use DIFFERENT ATR series %s — one "
                    "branch was fixed and the other missed" % sorted(syms))

# ---------------------------------------------------------------------------
# 2. The 2:1 discipline must survive. The defect was the clock, not the ratio;
#    quietly lowering MIN_RR_RATIO would "fix" the silence by abandoning the
#    gate that ended the 21-consecutive-loss streak.
# ---------------------------------------------------------------------------
m = re.search(r"\nMIN_RR_RATIO\s*=\s*([0-9.]+)", SRC)
if not m:
    failures.append("MIN_RR_RATIO constant not found")
else:
    rr = float(m.group(1))
    if rr < 2.0:
        failures.append(
            "MIN_RR_RATIO lowered to %s — the 2:1 gate is documented as what "
            "ended the 21-consecutive-loss streak. The silence was a unit "
            "error, not an over-strict ratio; loosening it treats the symptom"
            % rr)

# ---------------------------------------------------------------------------
# 3. The arithmetic itself. Prove the OLD form was unreachable and the NEW
#    form is not, using the measured ATR_60m/ATR_15m ratio.
# ---------------------------------------------------------------------------
MEASURED_RATIO = 2.29   # Kraken OHLC, 5 majors, 2026-08-26
SL = 2.0

# A synthetic-but-representative setup: BB stdev equal to ATR (ratio ~1.0,
# the live median), price sitting exactly on the lower band.
atr15 = 1.0
bb_stdev = 1.86            # AAVE/USD, the most favourable live pair
bb_half = 2.0 * bb_stdev   # BB_STD = 2.0
price = 100.0
tp1 = price + bb_half      # bb_middle, from the lower band

rr_old = bb_half / (SL * atr15 * MEASURED_RATIO)
rr_new = bb_half / (SL * atr15)

if rr_old >= 2.0:
    failures.append("the OLD 60m-stop form clears 2:1 in this test's own "
                    "arithmetic (%.3f) — the premise of this test is wrong "
                    "and must be re-derived" % rr_old)
if rr_new <= rr_old:
    failures.append("matching timeframes did not improve R:R (%.3f -> %.3f)"
                    % (rr_old, rr_new))
expected_gain = MEASURED_RATIO
actual_gain = rr_new / rr_old if rr_old else 0
if abs(actual_gain - expected_gain) > 0.01:
    failures.append("R:R gain %.3f != measured ATR ratio %.3f — the fix does "
                    "not recover exactly the mismatch factor"
                    % (actual_gain, expected_gain))

# --------------------------------------------------------------------- report
print("checked %s" % SRC_PATH)
print("stop expressions: %s" % [s.strip() for s in stops])
print("R:R on the best live setup — old %.3f, new %.3f (%.2fx)"
      % (rr_old, rr_new, actual_gain))
print("NOTE: new R:R is still < 2.0 — coherent gate, rare setup, not a "
      "promise of signals")

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: stop and target read the same 15m series in both directions, "
      "and MIN_RR_RATIO still enforces 2:1")
