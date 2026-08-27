"""A protection that needs no price must not be skipped when the price is gone.

THE SHAPE. Every trader guards its position-management loop with some form
of `if not price: continue`. That is correct for stop-loss and take-profit,
which genuinely need a quote. It is WRONG for any protection that reads only
stored state -- a max-age exit, a drawdown kill -- because those could still
have fired. Losing the quote must not also lose the protections that never
needed one.

CONFIRMED INSTANCES (2026-08-27 fleet audit):

  Confluence   `if not price: continue` sat above the 36h MAX_POSITION_AGE
               check. BLUR/USD was held 37.8h while Oracle stopped covering
               the pair; stop, target AND time exit were blind at once. When
               a fallback quote was added the position closed IMMEDIATELY at
               exit_reason=STOP -- it had been through its stop the whole
               time. Fixed: flag-and-warn, then a held-pairs-only fallback.

  Gridzilla    `if not candles or len(candles) < 50: continue` sat above
               check_fills, check_range_break AND check_drawdown_kill.
               check_drawdown_kill compares grid["max_drawdown"] against a
               fraction of allocation -- it needs NO price. Gridzilla has no
               time exit, so a blind grid had no bound of any kind. The
               trigger is not rare: 347 "Kraken request failed" and 1273
               "Kraken public error" in gridzilla_v2.log. Fixed.

STILL OPEN at the time of writing (documented, not yet fixed -- each needs
its own change and its own verification):
  Arbitrageur  falls back to pos.entry_price, a FABRICATED neutral price
               that books a real loss as $0.00. Bounded only by TIME_STOP.
  NexusBrain   `if not pd or not pd.candles: continue` above check_exits,
               whose 72h time check needs no price. Also carries a dead
               `time_exit_hours = 48` that reads as live policy.
  Rubberband   `if price is None: continue`, no max-age exit at all, and a
               dropped pair keeps a stale price so exits compare to a dead
               number.
  TurtleSue    same guard, but owns no price-free exit, so nothing is made
               unreachable. Its issue is display honesty, not exits.

This test pins the two that are FIXED so they cannot regress, and asserts
the fleet-wide rule that a price guard must not swallow a price-free
protection silently.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

failures = []


def read(*parts):
    p = os.path.join(ROOT, *parts)
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def code_only(text):
    """Executable text only -- comments describe defects and would match."""
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


# --- Confluence: the age check must be inside the no-quote branch --------
conf = read("Confluence", "confluence.py")
if conf is None:
    failures.append("Confluence/confluence.py not found")
else:
    c = code_only(conf)
    m = re.search(r"def manage_positions.*?(?=\n    def )", c, re.S)
    if not m:
        failures.append("manage_positions not found in confluence.py")
    else:
        body = m.group(0)
        guard = body.find("if not price")
        age = body.find("MAX_POSITION_AGE_H")
        if guard < 0:
            failures.append("confluence lost its no-quote guard entirely")
        elif age < 0 or age > body.find("continue", guard) and body.find(
                "MAX_POSITION_AGE_H", guard, body.find("continue", guard)) < 0:
            failures.append(
                "confluence's MAX_POSITION_AGE_H check is no longer inside "
                "the no-quote branch -- a held position whose pair loses "
                "coverage becomes unmanageable again")

# --- Gridzilla: the drawdown kill must be reachable while blind ---------
gz = read("Gridzilla", "gridzilla.py")
if gz is None:
    failures.append("Gridzilla/gridzilla.py not found")
else:
    g = code_only(gz)
    m = re.search(r"if not candles or len\(candles\) < 50:(.*?)current_price =",
                  g, re.S)
    if not m:
        failures.append(
            "gridzilla's candle guard is gone or reshaped -- re-verify that "
            "check_drawdown_kill is still reachable when pricing fails")
    else:
        branch = m.group(1)
        if "check_drawdown_kill" not in branch:
            failures.append(
                "gridzilla's no-price branch no longer runs "
                "check_drawdown_kill -- the 5%-of-allocation circuit breaker "
                "is disabled by any failed Kraken fetch, and gridzilla has "
                "no time exit to fall back on")

# --- the fleet-wide rule: going blind must not be silent ----------------
# A bot that cannot price a position it HOLDS must say so. Four of six were
# silent when this was written; silence is how a blind position renders as
# a healthy one (the 'unmeasured defaults to healthy' scar).
for bot, path, marker in (
        ("confluence", ("Confluence", "confluence.py"),
         "position_unmanaged_no_quote"),
        ("gridzilla", ("Gridzilla", "gridzilla.py"), "_blind_pairs"),
):
    src = read(*path)
    if src is None:
        continue
    if marker not in code_only(src):
        failures.append(
            "%s no longer reports the blind state (%s missing) -- an "
            "unpriceable held position is indistinguishable from a healthy "
            "one on the dashboard" % (bot, marker))


# --- Arbitrageur: must not fabricate a price ----------------------------
arb = read("Arbitrageur", "arbitrageur.py")
if arb is not None:
    a = code_only(arb)
    if "closes[-1] if closes else pos.entry_price" in a:
        failures.append(
            "arbitrageur still falls back to pos.entry_price -- price==entry "
            "makes stop and take-profit both evaluate False while TIME_STOP "
            "still fires, booking a real loss as exactly $0.00")
    if "PRICE_CACHE_TTL_S" not in a:
        failures.append(
            "arbitrageur's price cache has no TTL again -- it refetches only "
            "when a pair is absent or short, so a startup price is served "
            "for the whole process lifetime and exits compare to a frozen "
            "number (measured: scans dropped from 4.2s to 0.0s, no network)")

# --- NexusBrain: dead config must stay dead, blind state must warn -------
nb = read("NexusBrain", "nexus_brain.py")
if nb is not None:
    n = code_only(nb)
    if "time_exit_hours" in n:
        failures.append(
            "nexus_brain has a live time_exit_hours again -- it was defined "
            "once and never read while max_hold_hours was the real setting, "
            "so it read as policy while changing nothing")
    if "_blind_pairs" not in n:
        failures.append(
            "nexus_brain no longer flags a held position it cannot price -- "
            "its 72h max_hold check sits inside the function the price guard "
            "skips entirely")

# --- Rubberband: stale prices must be rejected --------------------------
rb = read("Rubberband", "rubberband.py")
if rb is not None:
    r = code_only(rb)
    if "PRICE_MAX_AGE_S" not in r:
        failures.append(
            "rubberband no longer ages its price cache -- self.prices has "
            "one writer and no invalidation, and the universe is a rolling "
            "top-15, so a dropped pair keeps a frozen price that exits "
            "still compare against. It has NO max-age exit as a backstop")
    if "self.prices.get(pos.pair, pos.entry_price)" in r:
        failures.append(
            "rubberband defaults a missing price to the entry price again -- "
            "that renders a blind position as exactly break-even")

# --- TurtleSue: noise is not a win --------------------------------------
ts = read("TurtleSue", "turtlebot.py")
if ts is not None:
    t = code_only(ts)
    if "_PNL_EPSILON" not in t:
        failures.append(
            "turtlesue counts wins without an epsilon again -- all four of "
            "its stored trades are forced closes booked at entry, with P/L "
            "1.78e-14 / 0.0 / -7.36e-12 / 0.0, and `pnl > 0` scored the "
            "1.78e-14 as a WIN (published 25.0%, one fabricated win in the "
            "fleet numerator)")
    # Scope this to the win_rate property specifically. Other properties
    # (avg_win, pnl_in_n) legitimately return 0.0 for an empty list; the
    # first draft of this check matched those and failed correct code.
    # Scope this to the win_rate property specifically. Other properties
    # (avg_win, pnl_in_n) legitimately return 0.0 for an empty list; the
    # first draft matched those and failed against correct code.
    _i = t.find("def win_rate(self)")
    _j = t.find("def decided_trades", _i) if _i >= 0 else -1
    _wr_body = t[_i:_j] if (_i >= 0 and _j > _i) else ""
    if _wr_body and "return 0.0" in _wr_body:
        failures.append(
            "turtlesue's win_rate returns 0.0 for an empty book again -- "
            "zero is a measurement, an empty book is the absence of one")

print("price-free-exit + fabricated-price checks run across all six traders")

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: price-free protections stay reachable when the quote is gone, "
      "and going blind is reported rather than silent")
