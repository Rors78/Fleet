"""There must be exactly ONE definition of a synthetic probe pair.

THE INCIDENT (2026-08-26). This predicate lived in four modules —
command_center, expectancy, signal_broadcaster, weekly_analysis — and the
copies had already drifted. Three upper-cased the pair before testing it;
weekly_analysis did not. So `nf138587ok/usd` was filtered as a probe
everywhere except the weekly report, where it counted as a real trade.

Four copies, two behaviours, and nothing anywhere to make them disagree
loudly. That is the same shape that caused the incidents the predicate exists
to prevent:

  - 2026-08-07: 50 of 73 rows on /api/trades were probes worth +$617 of
    fabricated P/L, flipping the fleet trade feed's SIGN.
  - 2026-08-26: expectancy.py had no probe filter at all while three siblings
    did, so TurtleSue's 92 ZZPROBE rows (identical hardcoded pnl=12.34)
    reached the dashboard as wins.

This test pins two things: the copies stay collapsed into probe_pairs, and
every consumer agrees on every case — including the ones that used to drift.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
sys.path.insert(0, CC)

from probe_pairs import is_probe_pair  # noqa: E402

failures = []

# ---------------------------------------------------------------- invariant 1
# No module may re-implement the prefix test inline. The literal tuple is the
# fingerprint of a copy; probe_pairs.py itself is the one legitimate home.
LITERAL = re.compile(r'startswith\(\s*\(\s*["\']ZZPROBE["\']')
for fname in sorted(os.listdir(CC)):
    if not fname.endswith(".py") or fname == "probe_pairs.py":
        continue
    path = os.path.join(CC, fname)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        continue
    for m in LITERAL.finditer(src):
        line = src[: m.start()].count("\n") + 1
        failures.append(
            "%s:%d re-implements the probe prefix test inline — import "
            "is_probe_pair from probe_pairs instead. Copies drift: the "
            "weekly_analysis copy silently disagreed on lowercase pairs."
            % (fname, line)
        )

# ---------------------------------------------------------------- invariant 2
# Every consumer must agree with the canonical predicate on every case,
# including the two that exposed the historical drift.
CASES = [
    ("ZZPROBE032265B/USD", True),
    ("zzprobe032265b/usd", True),    # drifted case
    ("NF138587OK/USD", True),
    ("nf138587ok/usd", True),        # drifted case
    ("NFNOK/USD", True),
    ("ZZ/USD", True),
    ("BTC/USD", False),
    ("ETH/USD", False),
    ("NFT/USD", False),              # a real ticker, NOT a probe
    ("NF/USD", False),               # no digit -> not a generated marker
    (None, False),
    ("", False),
]

for pair, expected in CASES:
    got = is_probe_pair(pair)
    if got != expected:
        failures.append(
            "probe_pairs.is_probe_pair(%r) = %r, expected %r" % (pair, got, expected)
        )

consumers = {}
try:
    import command_center
    consumers["command_center"] = command_center._is_probe_pair
except Exception as exc:                                    # pragma: no cover
    failures.append("could not import command_center: %s" % exc)
try:
    import expectancy
    consumers["expectancy"] = expectancy._is_probe
except Exception as exc:                                    # pragma: no cover
    failures.append("could not import expectancy: %s" % exc)

if not consumers:
    # Measured nothing is not the same as measured and found nothing.
    print("FAIL: no consumer modules could be imported — the check proved nothing")
    sys.exit(1)

for name, fn in consumers.items():
    for pair, expected in CASES:
        try:
            got = fn(pair)
        except Exception as exc:
            failures.append("%s(%r) raised %s" % (name, pair, exc))
            continue
        if bool(got) != expected:
            failures.append(
                "%s(%r) = %r but the canonical predicate says %r — a copy has "
                "drifted back" % (name, pair, got, expected)
            )

# --------------------------------------------------------------------- report
print("consumers checked: %s" % ", ".join(sorted(consumers)))
print("cases per consumer: %d" % len(CASES))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: one probe predicate, and every consumer agrees on every case")
