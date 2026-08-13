"""A swallowed record_trade drops a trade from the durable store, silently.

Audit 2026-08-13 of code that only runs during FAILURE — paths that by
definition have never executed on this machine. 392 except handlers across
the fleet; 107 are exactly `pass`. Most are legitimate (event emission is
deliberately fire-and-forget), so this pins only the ones wrapping a
safety-relevant call.

THE RECORD_TRADE SHAPE (turtlesue, rubberband, arbitrageur, nexusbrain):
every one of these bots updates its OWN tally — trade_log.record(),
self.trades.append(), realized_pnl/equity — and then calls
_expectancy.record_trade() inside a try whose except was `pass`. A failure
there drops the trade from the DURABLE store while the local counters still
increment, so the bot's self-report and /api/expectancy diverge with
nothing indicating which one is short. That is the same two-sources-one-
truth problem the fleet already hit with win rates, one layer lower.

Recording must not BLOCK the close — that trade-off is deliberate and
unchanged. It just must not vanish.

THE SHUTDOWN RELEASE SHAPE (rubberband): the finally-block releases every
reservation on exit. A silent failure there strands capital in the pool for
a position the process is about to forget, leaving nothing to show it was
even attempted — the leak the 48h stale sweep exists to clean up hours
later.

This test asserts on SHIPPED SOURCE because these paths cannot be triggered
without breaking the expectancy tracker or the portfolio client of a live
bot.
"""
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


BOTS = {
    'turtlesue': 'D:/TurtleSue/turtlebot.py',
    'rubberband': 'D:/Rubberband/rubberband.py',
    'arbitrageur': 'D:/Arbitrageur/arbitrageur.py',
    'nexusbrain': 'D:/NexusBrain/nexus_brain.py',
}
SRC = {k: open(v, encoding='utf-8', errors='replace').read()
       for k, v in BOTS.items()}

# ── 1. No record_trade call may be followed by a bare `except: pass` ──
for bot, src in SRC.items():
    for m in re.finditer(r'_expectancy\.record_trade\(', src):
        tail = src[m.end():m.end() + 1400]
        # the handler that closes THIS call
        h = re.search(r'\n\s*except[^\n]*:\n(\s*)([^\n]+)', tail)
        check(h is not None,
              '[%s] could not locate the handler after record_trade' % bot)
        if h:
            body = h.group(2).strip()
            check(body != 'pass',
                  '[%s] record_trade failure is swallowed by a bare `pass` — '
                  'the trade drops out of the durable store while this bot\'s '
                  'own tally still counts it' % bot)
            check('EXPECTANCY RECORD FAILED' in tail,
                  '[%s] a failed record_trade must say so — the two sources '
                  'diverge silently otherwise' % bot)

# ── 2. The failure must be logged at ERROR, not swallowed or whispered ──
for bot, src in SRC.items():
    _idx = src.find('EXPECTANCY RECORD FAILED')
    check(_idx != -1, '[%s] missing the failure message entirely' % bot)
    if _idx != -1:
        # Look BOTH ways: two logging conventions are in use — a module
        # logger called before the message (logger.error("...FAILED...")),
        # and self._log(f"...FAILED...", "ERROR") where the severity is a
        # TRAILING argument. An earlier version of this check only looked
        # backwards and failed a correct implementation.
        _ctx = src[max(0, _idx - 260):_idx + 400]
        check(re.search(r'(log(ger)?\.error|logging\.error|["\']ERROR["\'])',
                        _ctx) is not None,
              '[%s] the record_trade failure must be ERROR level — a lost '
              'durable trade is not a debug detail' % bot)

# ── 3. The close must still proceed — reporting must not become blocking ──
for bot, src in SRC.items():
    _idx = src.find('EXPECTANCY RECORD FAILED')
    if _idx == -1:
        continue
    _after = src[_idx:_idx + 700]
    check('raise' not in _after.split('\n')[0:8].__str__(),
          '[%s] the handler must not re-raise — recording is best-effort by '
          'design and must never block a position close' % bot)

# ── 4. Rubberband's shutdown release must report failures ──
_rb = SRC['rubberband']
check('RELEASE FAILED' in _rb,
      'the shutdown release loop must report a failed release — a silent '
      'one strands capital in the pool for a position this process is about '
      'to forget')
check('NOT released' in _rb,
      'a shutdown that leaked reservations must summarise what was left '
      'committed, not just log line-by-line')
_i_rel = _rb.find('RELEASE FAILED')
# 700 chars back: the loop header sits ~7 lines above the report, past the
# try/except and the success print. A 400-char window missed it and failed
# a correct implementation.
_ctx = _rb[max(0, _i_rel - 700):_i_rel]
check('for pos in engine.positions' in _ctx,
      'the reporting must sit inside the per-position release loop, or a '
      'failure on one reservation would abort the rest')
# And the loop must CONTINUE after a failure rather than breaking out.
_loop = _rb[_i_rel:_i_rel + 500]
check('break' not in _loop.split('\n')[0].lower(),
      'a failed release must not abort the remaining releases')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  lost durable trades and stranded reservations announce themselves')
