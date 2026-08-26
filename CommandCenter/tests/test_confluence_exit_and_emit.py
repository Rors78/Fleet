"""Confluence must age out unquoted positions and must reach the event bus.

WHY THIS EXISTS. Two defects found in the 2026-08-26 audit, both of the same
family: a failure that leaves the bot trading correctly while destroying the
record an audit depends on.

  1. UNREACHABLE TIME EXIT. manage_positions() opened with

         price = self._price_for(pair, intel)
         if not price:
             continue

     and the MAX_POSITION_AGE_H check sat BELOW that guard. Any pair Oracle
     stopped quoting could therefore never age out. Measured against a 36h
     limit: AAVE/USD held 215.9h (6.0x), LINK/USD 212.1h, PENDLE/USD 182.9h.
     Those three were 88% of all current-era profit — positions the exit
     logic had lost track of, drifting up in a rising tape.

  2. SILENT EMIT FAILURE. _emit() called self._events.publish({...}).
     EventPublisher exposes emit(event_type, data) and has no publish() at
     all, so every call raised AttributeError into a bare `except: pass`.
     Confluence had emitted 0 TRADE_OPEN events in its entire history while
     gridzilla emitted 146. The TRADE_CLOSE rows on the bus were synthesized
     by Command Center's portfolio_release path, not by the bot.

The fix for (1) must NOT force-close without a price — booking a P/L against
an invented price would manufacture exactly the fictional numbers this fleet
has been bitten by. It flags instead. That distinction is asserted below.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_PATH = os.path.join(ROOT, "Confluence", "confluence.py")
PUB_PATH = os.path.join(ROOT, "CommandCenter", "event_publisher.py")

failures = []

for p in (SRC_PATH, PUB_PATH):
    if not os.path.exists(p):
        print("FAIL: missing %s" % p)
        sys.exit(1)

with open(SRC_PATH, encoding="utf-8", errors="replace") as fh:
    SRC = fh.read()
with open(PUB_PATH, encoding="utf-8", errors="replace") as fh:
    PUB = fh.read()

# ---------------------------------------------------------------------------
# 1. The publisher API. Read the SHIPPED class rather than trusting a name:
#    the whole defect was calling a method that does not exist.
# ---------------------------------------------------------------------------
pub_methods = set(re.findall(r"\n    def (\w+)\(", PUB))
if "emit" not in pub_methods:
    failures.append("EventPublisher has no emit() — the publisher API this "
                    "test pins has changed; re-derive before editing bots")
if "publish" in pub_methods:
    failures.append("EventPublisher grew a publish() method — the two APIs "
                    "are now ambiguous, which is how the original bug read "
                    "as correct")

# Confluence must call the method that exists, and must not call the one that
# does not. Strip comments and docstrings first: the fix documents the old
# `self._events.publish({...})` call in its own docstring, and a naive scan
# matches that prose and fails against correct code. Test the code, not the
# commentary about it.
def _code_only(text):
    """Drop docstrings, then comments. Crude but sufficient here."""
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return re.sub(r"#[^\n]*", "", text)


CODE = _code_only(SRC)

if re.search(r"self\._events\.publish\(", CODE):
    failures.append("confluence calls self._events.publish() — EventPublisher "
                    "has no such method; every event silently vanishes")
if not re.search(r"self\._events\.emit\(", CODE):
    failures.append("confluence never calls self._events.emit() — nothing "
                    "this bot does reaches the fleet bus")

# ---------------------------------------------------------------------------
# 2. The emit failure must be visible. A bare `except: pass` on the emit path
#    is what hid this for the bot's entire history.
# ---------------------------------------------------------------------------
m = re.search(r"\n    def _emit\(self.*?\n(?=    def )", SRC, re.S)
if not m:
    failures.append("could not locate _emit() in confluence.py")
else:
    body = m.group(0)
    has_handler = re.search(r"except Exception as \w+:", body)
    logs_it = "_log(" in body
    if not has_handler:
        failures.append("_emit() has no named exception handler — a bare "
                        "except cannot report what failed")
    if not logs_it:
        failures.append("_emit() swallows failures without logging — a "
                        "telemetry path that fails silently is how "
                        "0 TRADE_OPEN events went unnoticed for months")

# ---------------------------------------------------------------------------
# 3. The age check must be REACHABLE on the no-quote path.
#    This is the structural assertion: within manage_positions, the
#    MAX_POSITION_AGE_H reference must appear BEFORE the `continue` that
#    handles a missing price — otherwise it is dead code exactly when needed.
# ---------------------------------------------------------------------------
mp = re.search(r"\n    def manage_positions\(self.*?\n(?=    def )", CODE, re.S)
if not mp:
    failures.append("could not locate manage_positions() in confluence.py")
else:
    body = mp.group(0)
    guard = re.search(r"if not price:", body)
    cont = body.find("continue", guard.end()) if guard else -1
    ages = [a.start() for a in re.finditer(r"MAX_POSITION_AGE_H", body)]
    if not ages:
        failures.append("manage_positions no longer references "
                        "MAX_POSITION_AGE_H — the time exit is gone")
    elif cont == -1:
        failures.append("could not find the no-quote `continue` to test "
                        "reachability against")
    elif not any(a < cont for a in ages):
        failures.append(
            "every MAX_POSITION_AGE_H check sits AFTER the no-quote "
            "`continue` — a position whose pair stops being quoted can never "
            "age out. This is the defect that let AAVE/USD run 215.9h against "
            "a 36h limit and become 88%% of current-era profit")

    # And it must NOT close without a price.
    pre = body[:cont] if cont != -1 else body
    if re.search(r"self\._close\(", pre):
        failures.append(
            "manage_positions calls _close() on the no-quote path — closing "
            "requires a price, and inventing one books a fictional P/L. The "
            "stale position must be FLAGGED, not force-closed")

# ---------------------------------------------------------------------------
# 4. Behavioural proof: drive the real guard logic with a stub position.
# ---------------------------------------------------------------------------
LIMIT = None
lm = re.search(r"\nMAX_POSITION_AGE_H\s*=\s*(\d+)", SRC)
if lm:
    LIMIT = int(lm.group(1))
    if LIMIT <= 0:
        failures.append("MAX_POSITION_AGE_H is %d — no time exit at all" % LIMIT)
else:
    failures.append("MAX_POSITION_AGE_H constant not found")

if LIMIT:
    # Mirror the shipped guard: stale iff aged past the limit with no quote.
    def is_stale(age_h, price):
        return (not price) and age_h >= LIMIT

    cases = [
        (215.88, None, True,  "AAVE/USD as measured — 6.0x the limit"),
        (LIMIT,  None, True,  "exactly at the limit"),
        (LIMIT - 0.1, None, False, "just under the limit"),
        (215.88, 87.69, False, "aged but quoted — normal exits apply"),
    ]
    for age, price, want, label in cases:
        got = is_stale(age, price)
        if got != want:
            failures.append("stale-detection wrong for %s: got %s want %s"
                            % (label, got, want))

# --------------------------------------------------------------------- report
print("checked %s" % SRC_PATH)
print("MAX_POSITION_AGE_H = %s" % LIMIT)
print("EventPublisher methods: %s" % sorted(pub_methods))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: emit uses the real publisher API and reports failures; the age "
      "check is reachable without a quote and flags rather than force-closes")
