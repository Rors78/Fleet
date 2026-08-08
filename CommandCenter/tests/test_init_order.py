"""Attributes used during startup recovery must exist before recovery runs.

TurtleSue crash-looped on 2026-08-08. Chain: the fleet sat exactly at its
AEGIS-tightened 30% deployment cap, so when __init__ ran
_reconcile_positions() and found a stale UNI/USD reservation, the re-reserve
was DENIED (correct), reconcile took the exit path (correct), and
_execute_exit touched self._event_pub — assigned four lines LATER in
__init__. AttributeError before the constructor finished, exit code 1, and
the watchdog respawned it into the identical state: a crash loop at the
worst possible time, mid-recovery, with positions on the books.

The bot's own comment already knew the rule — "init before position
persistence so reconcile works" sits above the PortfolioClient — it was
just never applied to the event publisher.

Fix is two layers: the publisher is now assigned BEFORE reconcile (source
order asserted here), and _execute_exit uses getattr so a future reorder
degrades to "event not emitted" instead of "bot never starts".
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/TurtleSue/turtlebot.py', encoding='utf-8', errors='replace').read()

# ── 1. Source order: publisher assigned before reconcile is called ──
pub = src.find('self._event_pub = ')
rec = src.find('self._reconcile_positions()')
check(pub != -1 and rec != -1,
      'could not locate the publisher assignment or the reconcile call')
check(pub < rec,
      '_event_pub must be assigned BEFORE _reconcile_positions() runs — '
      'reconcile can take the exit path (stale reservation + cap denial) '
      'and the exit path emits TRADE_CLOSE; assigning it after is the '
      'crash-loop of 2026-08-08')

# The bus listener rides the same rule.
bus = src.find('self._bus = ')
check(bus != -1 and bus < rec,
      '_bus must also be assigned before reconcile')

# ── 2. Defence in depth: the access itself must tolerate absence ──
check('if getattr(self, "_event_pub", None):' in src,
      '_execute_exit must access the publisher via getattr — a bare '
      'self._event_pub turns a future init reorder back into a bot that '
      'never starts, at the exact moment it is recovering positions')
check(re.search(r'\n\s+if self\._event_pub:', src) is None,
      'a bare `if self._event_pub:` access remains — every publisher check '
      'in this file must use the getattr form')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  reconcile runs with its dependencies constructed; access is guarded')
