"""A rate the system has marked DEGENERATE must not reach the headline.

WHY THIS EXISTS. expectancy.py computes `win_rate_degenerate` -- true when
one side of the book is empty, so the rate is FORCED rather than measured
(16 wins, 0 losses is not "a 100% win rate", it is a bot that has never
recorded a loss). The dashboard honoured that flag in two DISPLAY sites.
The fleet aggregate in command_center.py, which produces the headline WR
chip and `best_performer`, never read it at all.

Measured 2026-08-27, live:

    gridzilla   16/16  = 100.0%   <- win_rate_degenerate: true
    confluence  11/32  =  34.4%
    turtlesue    1/4   =  25.0%
    ------------------------------
    published  28/52   =  53.8%

Gridzilla was 57% of the NUMERATOR and 31% of the denominator. Excluding
it the fleet is 12/36 = 33.3% -- a 20.5 point swing carried entirely by a
bot whose loss column is empty by construction.

Its scar (gridzilla-cannot-record-a-loss, fixed 2026-08-26) is that losing
grids exited through `remove_grid`, which never called `record_trade`. The
CODE is fixed; all 33 stored records predate the fix, and there have been
zero GRID_KILLED events since, so nothing has re-measured it.

The same bot ranked `best_performer` on +$960.94 -- earned pre-resize on
positions up to $49,784 against a pool that is now $210.53. Real
arithmetic, impossible economics, top of the leaderboard.

This test pins: the flag survives normalization, the rate excludes it, the
exclusion is disclosed rather than silent, and a degenerate bot cannot be
crowned best performer.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
SRC = os.path.join(CC, "command_center.py")

failures = []

with io.open(SRC, encoding="utf-8", errors="replace") as fh:
    src = fh.read()


def _code_only(text):
    """Strip comments and docstrings.

    A previous test in this suite failed against CORRECT code because it
    matched the defect it described inside its own explanatory comment.
    Only executable text is evidence.
    """
    out = []
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0]
        out.append(line)
    joined = "\n".join(out)
    parts = joined.split('"""')
    return "".join(parts[::2]) if len(parts) > 2 else joined


code = _code_only(src)

# --- 1. the normalizer must CARRY the flag -------------------------------
if '"win_rate_degenerate": _degen' not in code:
    failures.append(
        "the gridzilla normalizer no longer carries win_rate_degenerate -- "
        "the aggregate cannot exclude a rate it never receives")

# --- 2. _degen must be bound on BOTH branches ---------------------------
# It is assigned inside `if _exp_trades:`; without an unconditional
# initialiser the else path raises UnboundLocalError and takes down the
# whole bot poll, not just this field.
if "_degen = None" not in code:
    failures.append(
        "_degen has no unconditional initialiser -- the else branch of the "
        "expectancy check would raise UnboundLocalError and kill the poll")

# --- 3. the aggregate must SKIP degenerate rates ------------------------
if 'win_rate_degenerate") is True' not in code:
    failures.append(
        "the fleet win-rate loop no longer tests win_rate_degenerate -- a "
        "forced rate is back inside the headline figure")

# --- 4. the exclusion must be DISCLOSED, not silent ---------------------
if '"win_rate_degenerate_excluded"' not in code:
    failures.append(
        "win_rate_degenerate_excluded is not published -- a bot dropped "
        "from the rate with no way for a consumer to say why is as opaque "
        "as including it")

# --- 5. best_performer must not crown a degenerate bot ------------------
if "_best_eligible" not in code:
    failures.append(
        "best_performer no longer filters degenerate bots -- a ledger that "
        "cannot record a loss can win the ranking again")

# --- 6. worst_performer must NOT be filtered ----------------------------
# A degenerate rate always errs flattering, so it can only wrongly promote.
# Filtering the worst side too would hide a genuinely bad bot.
tree = ast.parse(src)
worst_seg = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "worst":
                worst_seg = ast.get_source_segment(src, node)
if worst_seg and "_best_eligible" in worst_seg:
    failures.append(
        "worst_performer is drawn from the best-eligible list -- a "
        "degenerate rate can only flatter, so filtering the worst side "
        "hides genuinely bad bots")

print("checks run against %d chars of executable source" % len(code))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: degenerate rates are excluded from the fleet win rate and from "
      "best_performer, the exclusion is disclosed, and worst_performer stays "
      "unfiltered")
