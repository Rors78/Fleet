"""Every character NEXUS emits must reach a branch in Confluence.

NEXUS._classify() emits exactly five values. Confluence mapped only two of
them: SYSTEMIC (penalty) and MIXED (neutral). INSTITUTIONAL, RETAIL_NOISE and
STRUCTURAL_SHIFT all fell through to the "unmapped" branch and were silently
discarded — 208 `NEXUS character not mapped: 'INSTITUTIONAL'` warnings in a
single log window.

Worse, the four risk-on keywords Confluence tested for (TRENDING, EXPANSION,
STABLE, ORDERED) match NOTHING nexus can produce. The NEXUS component of the
confluence score was therefore structurally incapable of ever being positive:
its only reachable outcomes were a penalty and a half-weight neutral.

This test reads BOTH sides from source, so the two drifting apart fails here
rather than silently in production.
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── Producer: every literal NEXUS._classify() can return ──
nx = open("D:/Nexus/nexus.py", encoding="utf-8", errors="replace").read()
m = re.search(r"def _classify\(self, ordered, spread\):(.*?)(?=\n    def |\n\nclass )",
              nx, re.S)
check(m is not None, "could not locate NEXUS._classify in nexus.py")
produced = sorted(set(re.findall(r'return "([A-Z_]+)"', m.group(1)))) if m else []
check(len(produced) >= 5,
      "expected at least 5 NEXUS characters, found %r" % produced)

# ── Consumer: replicate the shipped branch order from confluence.py ──
cf = open("D:/Confluence/confluence.py", encoding="utf-8", errors="replace").read()
check('if "INSTITUTIONAL" in character:' in cf,
      "confluence.py must handle INSTITUTIONAL explicitly — it is the most "
      "common live NEXUS character and was being discarded")
check('elif "RETAIL_NOISE" in character:' in cf,
      "confluence.py must handle RETAIL_NOISE explicitly")
check('elif "STRUCTURAL_SHIFT" in character:' in cf,
      "confluence.py must handle STRUCTURAL_SHIFT explicitly")

W = 1.0


def classify(character):
    """Mirrors the shipped branch order and weights in confluence.py."""
    weighted, contributed, mapped = 0.0, 0.0, True
    if "INSTITUTIONAL" in character:
        weighted, contributed = W * 0.85, W
    elif "RETAIL_NOISE" in character:
        weighted = -W * 0.5
    elif "STRUCTURAL_SHIFT" in character:
        weighted, contributed = W * 0.45, W
    elif any(k in character for k in ("TRENDING", "EXPANSION", "STABLE", "ORDERED")):
        weighted, contributed = W * 0.85, W
    elif any(k in character for k in ("CHAOTIC", "SYSTEMIC", "STRESSED", "TURBULENT")):
        weighted = -W * 0.5
    elif any(k in character for k in ("MIXED", "NEUTRAL", "RANGING", "TRANSITION")):
        weighted, contributed = W * 0.45, W
    else:
        mapped = False
    return weighted, contributed, mapped


# ── 1. Nothing NEXUS emits may be discarded ──
unmapped = [c for c in produced if not classify(c)[2]]
check(not unmapped,
      "NEXUS emits these characters but Confluence discards them: %r" % unmapped)

# ── 2. The component must be able to score positive ──
positive = [c for c in produced if classify(c)[0] > 0]
check(len(positive) >= 1,
      "no NEXUS character can contribute positively — the component is a "
      "penalty-only input, which is what the unmapped bug caused")

# ── 3. A penalty must never enlarge the denominator ──
for c in produced:
    w, d, _ = classify(c)
    check(not (w < 0 and d > 0),
          "%s penalises the numerator AND adds to the denominator — that "
          "double-counts the penalty" % c)

# ── 4. Direction semantics must match the producer's own interpretation ──
# nexus._interpret(): INSTITUTIONAL = "likely to continue" (confirming),
# RETAIL_NOISE = "likely to revert" (not confirming).
check(classify("INSTITUTIONAL")[0] > 0,
      "INSTITUTIONAL is whale-led and 'likely to continue' — it must confirm")
check(classify("RETAIL_NOISE")[0] < 0,
      "RETAIL_NOISE is 'likely to revert' — it must not confirm a "
      "continuation thesis")
check(classify("INSTITUTIONAL")[0] > classify("RETAIL_NOISE")[0],
      "INSTITUTIONAL must rank above RETAIL_NOISE")

if FAIL:
    for f in FAIL:
        print("FAIL  " + f)
    sys.exit(1)
print("ok  all %d NEXUS characters reach a branch (%s)" % (len(produced), ",".join(produced)))
