"""A bot must trade the capital it was GRANTED, not the capital it asked for.

WHY THIS EXISTS. PortfolioManager.reserve applies a fleet-intel risk
multiplier and can scale a reservation DOWN before granting it
(command_center.py:739-745, logged as "Intel scaled ..."). It returns the
final figure in the response — but PortfolioClient.reserve discarded it and
handed back only (ok, reservation_id). No bot could learn it had been
scaled, so each kept its pre-scale size, opened at that size, and computed
P/L from it.

Measured live 2026-08-27:

    confluence ENA/USD   position size_usd $6.1816   reserved $1.59  = 3.89x
    confluence LINK/USD  booked +$1.0569 P/L against $1.77 reserved
                         -> a published +59.7% return on capital

50 "Intel scaled" events across gridzilla, turtlesue, nexusbrain and
confluence; worst ratio 4.8x.

Note the gate is defeated in BOTH directions: it intends to cut exposure,
but the bot trades at full size anyway, so the de-risking is cosmetic while
the accounting inflates. Any per-trade or percentage return computed against
reserved capital was indefensible while this stood.

This test pins both halves of the fix — the client must expose the granted
amount, and a bot must honour it.
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
ROOT = os.path.dirname(CC)

failures = []


def read(*parts):
    p = os.path.join(*parts)
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def code_only(text):
    """Executable text only — the comments here describe the defect."""
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


# --- 1. Command Center must still RETURN the granted amount --------------
cc = read(CC, "command_center.py")
if cc is None:
    failures.append("command_center.py not found")
else:
    c = code_only(cc)
    if '"amount": amount, "available"' not in c:
        failures.append(
            "PortfolioManager.reserve no longer returns the granted amount "
            "in its response — the client cannot report what it could not "
            "receive, and every bot goes back to trading its pre-scale size")

# --- 2. The client must EXPOSE it ---------------------------------------
pc = read(CC, "portfolio_client.py")
if pc is None:
    failures.append("portfolio_client.py not found")
else:
    p = code_only(pc)
    if "last_granted_amount" not in p:
        failures.append(
            "PortfolioClient no longer exposes last_granted_amount — the "
            "scaled figure is discarded again and no bot can learn it was "
            "scaled down")
    # It must be initialised in __init__, or the first read raises.
    tree = ast.parse(pc)
    init_ok = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            seg = ast.get_source_segment(pc, node) or ""
            if "last_granted_amount" in seg:
                init_ok = True
    if not init_ok:
        failures.append(
            "last_granted_amount is not initialised in PortfolioClient."
            "__init__ — a caller reading it before any reserve() would "
            "raise AttributeError")
    # It must be CLEARED on failure, or a stale value from a previous
    # reservation reads as this one's grant.
    if p.count("last_granted_amount = None") < 2:
        failures.append(
            "last_granted_amount is not cleared on the failure paths — a "
            "stale grant from an earlier reservation could be read as this "
            "one's, which is worse than not having the field at all")

# --- 3. Confluence must HONOUR it ---------------------------------------
# Confluence is the only bot with closed trades in the current era, so it
# is the one whose published returns this actually corrupts today.
conf = read(ROOT, "Confluence", "confluence.py")
if conf is None:
    failures.append("Confluence/confluence.py not found")
else:
    f = code_only(conf)
    if "last_granted_amount" not in f:
        failures.append(
            "confluence does not read last_granted_amount — it opens at the "
            "size it requested rather than the size the pool granted, so "
            "position size and backing capital diverge again")
    if "size = _granted" not in f:
        failures.append(
            "confluence reads the granted amount but never applies it to "
            "size — reading a value and not using it is not a fix")
    # The guard must be one-directional: only ever scale DOWN. Growing a
    # position to a granted amount larger than requested would be the
    # opposite bug.
    if "< size" not in f:
        failures.append(
            "confluence's granted-size guard is not bounded to scale DOWN "
            "only — it must never grow a position beyond what it asked for")

print("checked: CC returns the amount, the client exposes it, confluence honours it")

if failures:
    for x in failures:
        print("FAIL: %s" % x)
    sys.exit(1)

print("PASS: the granted amount survives the client and is what gets traded")
