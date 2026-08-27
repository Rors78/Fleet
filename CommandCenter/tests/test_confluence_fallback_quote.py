"""A held position must never become permanently unmanageable.

WHY THIS EXISTS. Confluence reads prices from exactly one place: Oracle's
signal set. Oracle publishes ~10 rows and its coverage rotates. Measured
2026-08-27, BLUR/USD was opened while Oracle covered it and then dropped
out of that set -- at which point `_price_for` returned None forever, and
stop, target AND time exits were all blind simultaneously. The position
could be FLAGGED (the 36h ATTENTION fired correctly at age 36.007h) but
never CLOSED.

That is the same failure shape as the original unreachable-exit bug, one
layer further out: the exit code is reachable, but the input it needs
never arrives.

The fix adds a last-resort quote via kraken_ohlc for HELD pairs only.
This test pins the three properties that make it safe:

  1. It is reachable only from manage_positions -- never from entry.
     Otherwise the bot would silently trade a wider universe than Oracle
     covers, which is a strategy change, not a bug fix.
  2. A stale bar is refused. Exiting on a lagging price books a P/L that
     never happened; staying flagged-and-open is the honest failure.
  3. Total failure returns None, preserving the previous behaviour
     exactly -- absence still reads as absence, never a fabricated price.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "Confluence", "confluence.py")

failures = []

with io.open(SRC, encoding="utf-8", errors="replace") as fh:
    src = fh.read()
tree = ast.parse(src)

def find_func(name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None

price_for = find_func("_price_for")
manage = find_func("manage_positions")
try_enter = find_func("try_enter")
score = find_func("score_candidates")

if price_for is None:
    failures.append("_price_for is gone -- this test no longer checks anything")
if manage is None:
    failures.append("manage_positions is gone")

# --- 1. the fallback exists and lives inside _price_for --------------------
if price_for is not None:
    pf_src = ast.get_source_segment(src, price_for) or ""
    if "_fetch_ohlc" not in pf_src:
        failures.append(
            "_price_for no longer calls the kraken_ohlc fallback -- a held "
            "pair that Oracle stops covering can never be exited again")
    if "age_min" not in pf_src or "30" not in pf_src:
        failures.append(
            "_price_for lost its stale-bar refusal -- it can now close a "
            "position on a lagging price and book a P/L that never happened")

# --- 2. ONLY manage_positions may reach it --------------------------------
callers = []
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                nm = getattr(f, "attr", None) or getattr(f, "id", None)
                if nm == "_price_for":
                    callers.append(node.name)
callers = sorted(set(callers))
if callers != ["manage_positions"]:
    failures.append(
        "_price_for is called from %s -- it must be reachable ONLY from "
        "manage_positions. A fallback quote on the ENTRY path would let the "
        "bot open positions on pairs Oracle never signalled, which is a "
        "strategy change disguised as a bug fix" % (callers or ["nothing"]))

# --- 3. the entry path must not reach kraken_ohlc by any other route -------
for fn, label in ((try_enter, "try_enter"), (score, "score_candidates")):
    if fn is None:
        continue
    seg = ast.get_source_segment(src, fn) or ""
    if "_fetch_ohlc" in seg:
        failures.append("%s calls _fetch_ohlc directly -- entry must depend "
                        "on Oracle coverage alone" % label)

# --- 4. the import must be soft (bot has to run standalone) ---------------
if "_fetch_ohlc = None" not in src:
    failures.append("kraken_ohlc import is not soft -- an ImportError would "
                    "take the whole bot down at startup")

# --- 5. fabricating a price must be impossible ----------------------------
if price_for is not None:
    pf_src = ast.get_source_segment(src, price_for) or ""
    if "if not candles:" not in pf_src and "if not candles" not in pf_src:
        failures.append("_price_for does not guard an empty candle list")
    if "close <= 0" not in pf_src:
        failures.append("_price_for does not reject a non-positive close")

print("_price_for callers: %s" % (callers or "none"))
print("stale-bar refusal : %s" % ("present" if price_for is not None and
      "age_min" in (ast.get_source_segment(src, price_for) or "") else "MISSING"))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: fallback quote is held-positions-only, stale-refusing, and "
      "cannot fabricate a price")
