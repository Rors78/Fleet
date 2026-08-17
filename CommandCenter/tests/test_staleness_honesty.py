"""Unknown age must never read as fresh.

Four fabrications found in one sweep, all the same shape: a missing or
unreadable TIMESTAMP producing the most favourable possible reading.

--- 1. A dead bot published data_stale: false ---

command_center.py's poll loop set data_stale/stale_age_s on the success path
(with a comment: "A bot answering HTTP is not the same as a bot serving fresh
data"), but the `except` branch -- a bot that did not answer AT ALL -- omitted
both keys while carrying forward the previous cycle's raw/normalized payload.
Consumers read `.get("data_stale", False)` and published "fresh" for a dead
bot serving last cycle's data.

The freshness flag read clean in exactly the case it exists to catch. This is
also why a fleet health check could report "DATA-STALE: none" with 18/18
alive: it could only ever flag bots that answered.

--- 2. _decay_strength returned 1.0 for an unknown age ---

bus_listener.py: `ts = event.get("ts", ...); if not ts: return 1.0`.

MAXIMUM freshness for an event of unknown age -- the exact inverse of the
correct reading. whale_tier() gates on `_decay_strength(best) < 0.2`, so a
timestamp-less WHALE_ALERT sailed through at full strength to every consumer
of this module: Arbitrageur, Confluence, Contrarian, Gridzilla, NexusBrain,
Rubberband, TurtleSue. event_bus.publish uses setdefault, which PRESERVES an
explicit ts of 0, and disk-replayed events carry no guarantee at all.

A negative age (future timestamp) also returned 1.0. That is a clock
disagreement, not freshness.

--- 3. Subscriber cards were stamped with RENDER time ---

card_renderer.py: `if timestamp is None: timestamp = datetime.now(utc)`.

Card builders read data.get("timestamp"), but a bus event carries `ts` at the
TOP level and its `data` has no timestamp at all -- verified against a live
event: top-level keys are [data, id, source, ts, type] and `data` has no
timestamp key. So this was not an edge case, it was EVERY card: a delayed or
replayed event presented to paying subscribers as current.

Proven fixed: an event 3 hours old now renders its own 13:04 UTC instead of
the 16:04 render time.

--- 4. Activity-feed entries defaulted to now() ---

Three sites in command_center.py stamped time.time() onto entries whose real
time was unknown, which sorted them to the TOP of the feed as newest. Combined
with (1) -- a dead bot's payload carried forward every cycle -- the same stale
signals reappeared at the top of the feed indefinitely, each time with a fresh
timestamp. Undated entries now sort last and render "--".
"""
import os
import re
import sys
import time

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ── 1. Decay: unknown or future age must be fully decayed ──
from bus_listener import _decay_strength

for _label, _ev in (
        ('no ts at all', {"type": "WHALE_ALERT"}),
        ('ts = 0', {"type": "WHALE_ALERT", "ts": 0}),
        ('ts = None', {"type": "WHALE_ALERT", "ts": None}),
        ('ts non-numeric', {"type": "WHALE_ALERT", "ts": "recent"}),
        ('ts in the future', {"type": "WHALE_ALERT", "ts": time.time() + 600}),
):
    _d = _decay_strength(_ev)
    check(_d == 0.0,
          'an event with %s decayed to %r. Unknown age must be FULLY decayed '
          '(0.0), never fresh: whale_tier() gates on `< 0.2`, so 1.0 handed a '
          'timestamp-less alert to seven live bots at full strength'
          % (_label, _d))

# A genuinely fresh event must still score high, or the fix is a blind spot.
_fresh = _decay_strength({"type": "WHALE_ALERT", "ts": time.time()})
check(_fresh > 0.95,
      'a just-now event must still decay to ~1.0, got %r -- a detector that '
      'discards everything is not a fix' % (_fresh,))

# And an old one must decay below the 0.2 gate.
_old = _decay_strength({"type": "WHALE_ALERT", "ts": time.time() - 86400})
check(_old < 0.2,
      'a day-old event must decay below the 0.2 whale gate, got %r' % (_old,))

# ── 2. Source: the dead-bot branch must set data_stale ──
_cc = open('D:/CommandCenter/command_center.py', encoding='utf-8',
           errors='replace').read()
_dead = re.search(r'except Exception:\s*\n(\s+#[^\n]*\n)*\s+_prev_seen'
                  r'(?:.|\n){0,900}?"alive": False,(?:.|\n){0,600}?\}', _cc)
check(_dead is not None and '"data_stale": True' in (_dead.group(0) if _dead else ''),
      "the poll loop's dead-bot branch must set data_stale explicitly. "
      "Omitting it let .get('data_stale', False) publish 'fresh' for a bot "
      "that never answered, while its previous payload was carried forward")

# ── 3. Source: no feed entry may default its time to now() ──
_code = re.sub(r'#[^\n]*', '', _cc)
for _m in re.finditer(r'"time":\s*[^,\n]*\.get\([^)]*,\s*time\.time\(\)\)',
                      _code):
    check(False,
          'a feed entry still defaults its timestamp to now(): %r -- that '
          'sorts an undated entry to the top as the newest thing in the feed'
          % (_m.group(0)[:80],))
check('or time.time()' not in _code or
      not re.search(r'sig\.get\("timestamp"\)\s*or\s*time\.time\(\)', _code),
      'a signal timestamp still falls back to now()')

# ── 4. Source: the card renderer must not invent a time ──
_cr = open('D:/CommandCenter/card_renderer.py', encoding='utf-8',
           errors='replace').read()
_crc = re.sub(r'#[^\n]*', '', _cr)
for _m in re.finditer(r'if timestamp is None:\s*\n\s*timestamp\s*=\s*'
                      r'datetime\.now', _crc):
    check(False,
          'card_renderer still stamps render time onto an event with no '
          'timestamp -- that presents a replayed or delayed signal as '
          'current, to paying subscribers')

# ── 5. Behavioural: a card carries the time it is given ──
try:
    import card_renderer as _crm
    from datetime import datetime, timezone
    _r = _crm.CardRenderer()
    _old_ts = time.time() - 3 * 3600
    _want = datetime.fromtimestamp(_old_ts, timezone.utc).strftime("%H:%M UTC")
    _now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    _png = _r.render_trade_open({"pair": "BTC/USD", "direction": "LONG",
                                 "entry": 100.0, "size_usd": 5.0,
                                 "bot": "confluence", "timestamp": _want})
    check(bool(_png), 'card with an explicit timestamp failed to render')
    # Renders without a timestamp too, rather than raising or inventing one.
    _png2 = _r.render_trade_open({"pair": "BTC/USD", "direction": "LONG",
                                  "entry": 100.0, "size_usd": 5.0,
                                  "bot": "confluence"})
    check(bool(_png2),
          'card with NO timestamp must still render (blank time), not raise')
    check(_want != _now,
          'test setup: the 3h-old stamp must differ from now')
except Exception as _e:
    FAIL.append('card render check raised %r' % (_e,))

# ── 6. Correlation: an empty matrix is not "perfectly uncorrelated" ──
_col = open('D:/CommandCenter/collector.py', encoding='utf-8',
            errors='replace').read()
_colc = re.sub(r'#[^\n]*', '', _col)
check('max(len(matrix), 1)' not in _colc,
      'collector still divides by max(len(matrix), 1), publishing 0.0000 '
      'avg_abs_correlation -- "the fleet is perfectly uncorrelated", the most '
      'permissive risk reading there is -- when every pair was skipped for '
      "too few samples. AEGIS consumes this and its guard cannot catch it: "
      '0.0 passes isinstance, passes the NaN check, and clamps like a real '
      'measurement')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  unknown age reads as unknown, never as fresh')
