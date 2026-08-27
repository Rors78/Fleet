"""Every bot that reads candles must go through the canonical fetcher.

WHY THIS EXISTS. kraken_ohlc.fetch_ohlc asks Command Center first and falls
back to Kraken's public OHLC endpoint when CC cannot answer. A private copy
that only asks CC returns None/[] the moment CC is unreachable -- and every
caller treats that as "this pair has no candles":

    candles = fetch_ohlc(pair, interval=5, limit=4)
    if not candles or len(candles) < 3:
        continue

So the bot does not degrade, it goes BLIND, and a silent market becomes
indistinguishable from a market with no signal. That matters here: CC's own
cold start was measured at 703 seconds on 2026-08-26, which is a long time to
be quietly not looking.

Two private copies existed when this was written:

    Contrarian/contrarian.py   CC-only, returned None      -> consolidated
    Nexus/nexus.py             CC-only, returned []        -> consolidated

Seven other bots were already on the canonical module. This test pins that
count at "all of them" so the next copy-paste gets caught at the suite rather
than during an outage.

PhiTex is exempt: it calls Kraken's public OHLC endpoint directly, which has
the fallback property this test is protecting. The failure mode being pinned
is CC-only, not not-canonical.
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
ROOT = os.path.dirname(CC)

sys.path.insert(0, CC)

failures = []

BOT_DIRS = [
    "Confluence", "TurtleSue", "Gridzilla", "Rubberband", "Arbitrageur",
    "NexusBrain", "Nexus", "Trinity", "PhiTex", "Aegis", "Chronos",
    "Contrarian", "Sentinel", "Oracle", "HiveMind",
]

CC_OHLC = "api/market/ohlc"
DIRECT_KRAKEN = "api.kraken.com/0/public/OHLC"
CANONICAL = "kraken_ohlc"

checked = 0
canonical_users = []
direct_users = []

for d in BOT_DIRS:
    for path in glob.glob(os.path.join(ROOT, d, "*.py")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        if CC_OHLC not in src and DIRECT_KRAKEN not in src:
            continue
        checked += 1
        rel = os.path.relpath(path, ROOT)
        if CANONICAL in src:
            canonical_users.append(rel)
        elif DIRECT_KRAKEN in src:
            # Has its own venue access — the fallback property is satisfied.
            direct_users.append(rel)
        else:
            failures.append(
                "%s reads candles from Command Center only (no %s import, no "
                "direct venue call). When CC is down this returns empty and "
                "its caller reads that as 'no candles', so the bot scans "
                "blind instead of falling back to Kraken" % (rel, CANONICAL))

if checked == 0:
    # An empty sweep is not a pass — it means the paths moved.
    failures.append("no candle-reading files found under %s — the bot "
                    "directories moved and this test is checking nothing"
                    % ROOT)

# ---------------------------------------------------------------------------
# The canonical module must actually HAVE the fallback this test assumes.
# Pinning "everyone imports it" is worthless if it stops falling back.
# ---------------------------------------------------------------------------
kpath = os.path.join(CC, "kraken_ohlc.py")
if not os.path.exists(kpath):
    failures.append("kraken_ohlc.py is missing — the canonical fetcher this "
                    "test points every bot at does not exist")
else:
    with open(kpath, encoding="utf-8", errors="replace") as fh:
        ksrc = fh.read()
    if DIRECT_KRAKEN not in ksrc:
        failures.append(
            "kraken_ohlc.py no longer calls %s — it is CC-only now, so every "
            "bot consolidated onto it inherits the exact blindness this test "
            "was written to remove" % DIRECT_KRAKEN)
    if "cc_url" not in ksrc:
        failures.append("kraken_ohlc.fetch_ohlc lost its cc_url parameter — "
                        "callers pass their own CC address")

# --------------------------------------------------------------------- report
print("candle-reading files checked: %d" % checked)
print("  canonical (kraken_ohlc): %d" % len(canonical_users))
for f in sorted(canonical_users):
    print("     %s" % f)
if direct_users:
    print("  direct venue access (exempt): %d" % len(direct_users))
    for f in sorted(direct_users):
        print("     %s" % f)

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: no bot reads candles from Command Center alone; the canonical "
      "fetcher still falls back to Kraken")
