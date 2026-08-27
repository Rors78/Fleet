"""Gridzilla must size off the real pool, and its exposure cap must bind.

TWO DEFECTS, both found 2026-08-27, both the project's documented shapes.

1. A $10,000 HARDCODED POOL FALLBACK. gridzilla.py read
   `portfolio_balance = 10000` and only overwrote it when
   `portfolio.available()` returned non-None. available() returns None on a
   Command Center outage, so the default stood SILENTLY -- a ~47x
   overstatement against the real ~$215 pool. Rubberband and Arbitrageur
   both removed their equivalents by name in an earlier pass; this one was
   missed. Fix-one-sibling, again.

   The consequence was worse than the number. Gridzilla skips any pair
   whose allocation is under $500. 5% of the real pool is ~$10, so with a
   CORRECT reading it deploys nothing -- while the $10,000 fiction yields
   exactly $500 and clears the floor. A DEPENDENCY FAILURE WAS THE ONLY
   CONDITION UNDER WHICH THIS BOT TRADED, at ~12x the fleet per-trade cap,
   on capital the pool had never granted.

2. AN EXPOSURE CAP READING A KEY NOTHING WRITES. The 30% portfolio ceiling
   summed `_g.get("allocation")` -- a key deploy_grid never puts in
   grid_state. The only "allocation" in the file is inside the
   GRID_DEPLOYED event payload. So current_exposure was PERMANENTLY 0.0 and
   the ceiling could never fire; ten pairs at 5% each would have reached
   50% unopposed.

   The comment directly above that line documented the PREVIOUS version of
   the same bug and asserted the fix -- written against a field name that
   does not exist. Unmeasured exposure defaulting to zero and reading as
   healthy, twice in one expression.

Neither was live: gridzilla has deployed no grids since the pool resize.
Both were armed the moment it traded again.
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "Gridzilla", "gridzilla.py")

failures = []

if not os.path.exists(SRC):
    print("FAIL: Gridzilla/gridzilla.py not found -- this test is checking nothing")
    sys.exit(1)

with io.open(SRC, encoding="utf-8", errors="replace") as fh:
    src = fh.read()


def code_only(text):
    """Executable text only -- the comments here quote the defects."""
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


code = code_only(src)

# --- 1. no hardcoded pool ------------------------------------------------
if "portfolio_balance = 10000" in code:
    failures.append(
        "gridzilla has its $10,000 hardcoded pool fallback back -- against "
        "the real ~$215 pool that is a 47x overstatement, reached silently "
        "whenever Command Center is unreachable, and it is the ONLY way "
        "this bot clears its own $500 per-pair floor")

# --- 2. it must refuse rather than guess --------------------------------
if "pool_total()" not in code:
    failures.append(
        "gridzilla no longer reads pool_total() -- available() makes it "
        "resize because an UNRELATED bot traded, which is not a property "
        "of its own strategy")

# --- 3. the exposure cap must read a field that EXISTS ------------------
if '_g.get("allocation")' in code:
    failures.append(
        "gridzilla's exposure sum reads _g.get(\"allocation\") again -- "
        "deploy_grid never writes that key into grid_state, so "
        "current_exposure is permanently 0.0 and the 30% portfolio ceiling "
        "can never fire")

if "allocation_usd" not in code:
    failures.append(
        "gridzilla's exposure sum no longer reads allocation_usd -- that "
        "is the field the design dict actually carries (written at the "
        "GridArchitect.design return)")

# --- 4. the field it reads must really be produced ----------------------
# Guard against fixing one phantom key by inventing another.
if '"allocation_usd": round(allocation_usd, 2)' not in code:
    failures.append(
        "GridArchitect.design no longer emits allocation_usd -- the "
        "exposure sum would be reading a phantom key again, which is the "
        "exact defect this test exists to prevent")

# --- 5. an unmeasurable grid must not count as zero ---------------------
if "_exposure_unknown" not in code:
    failures.append(
        "a grid with no readable allocation is silently counted as zero "
        "exposure again -- that is how this cap died the first time")

print("gridzilla sizing + exposure checks run against %d chars of source"
      % len(code))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: gridzilla sizes off the real pool or refuses, and its exposure "
      "cap reads a field that is actually written")
