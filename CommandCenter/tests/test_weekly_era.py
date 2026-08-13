"""A dollar average across a pool resize describes neither era.

The weekly scorecard publishes "Avg P/L $X per trade" to subscribers. It is
computed by averaging per-day expectancies weighted by trade count — which
is correct only if the days averaged were traded at comparable position
sizes.

On 2026-08-13 the pool went $1M -> $210.53. Days before it carry $7,516 to
$66,711 positions; days after carry $5.78 to $13.92. A week spanning that
boundary averages both into one figure that describes neither, and sends it
to subscribers as the fleet's per-trade result.

The daily records hold no position sizes, so this cannot be normalised the
way the trade cards are (those compute a return % from entry and exit).
Instead the discontinuity is detected from the per-day expectancies
themselves — a >50x spread between the largest and smallest non-zero
magnitude is a scale change, not a good week — and the figure is OMITTED
with a stated reason.

Three distinct states, deliberately:
    a real average        -> "Avg P/L  $+0.03 per trade"
    omitted as misleading -> "Avg P/L  n/a — sizing changed mid-week"
    genuinely no data     -> row absent entirely
A bare em dash for the middle case would read as lost data, which is a
different claim from "this number would mislead you".
"""
import re
import sys

sys.path.insert(0, 'D:/CommandCenter')

import signal_broadcaster as sb

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def strip(s):
    return re.sub(r'<[^>]+>', '', s or '')


fmt = sb.CardFormatter.__new__(sb.CardFormatter)

BASE = {"total_pnl": 571.29, "total_trades": 27, "avg_win_rate": 0.783,
        "best_day": "Mon", "worst_day": "Tue", "days_positive": 4,
        "days_total": 7, "days_with_trades": 5}

# ── 1. A week spanning a resize omits the figure and says why ──
_span = strip(fmt.format_weekly_report(
    dict(BASE, avg_expectancy=None,
         expectancy_omitted_reason="position sizes changed during the week")))
check("n/a" in _span and "sizing changed" in _span,
      'a week whose position sizes changed must SAY the average is '
      'unavailable, not print a misleading number or a bare dash; got:\n%s'
      % _span)
# Split defensively: without the fix the row is absent entirely, and
# indexing [1] raises IndexError before any assertion prints — the red
# proof then reads as "no failures". Eighth time this flaw has bitten a
# test in this session.
_parts = _span.split("Avg P/L")
_row = _parts[1].split("\n")[0] if len(_parts) > 1 else "ROW ABSENT"
check("per trade" not in _row,
      '"per trade" only makes sense after a number — "n/a this week per '
      'trade" is malformed; got: %r' % _row)

# ── 2. A normal week still publishes the real figure ──
_norm = strip(fmt.format_weekly_report(
    dict(BASE, avg_expectancy=0.03, expectancy_omitted_reason=None)))
check("$+0.03 per trade" in _norm,
      'a week with comparable sizing must still publish its average — the '
      'guard must not suppress every figure; got:\n%s' % _norm)

# ── 3. Genuinely absent data omits the row entirely ──
_none = strip(fmt.format_weekly_report(
    dict(BASE, avg_expectancy=None, expectancy_omitted_reason=None)))
check("Avg P/L" not in _none,
      'with no expectancy and no reason, the row must be ABSENT — "missing "'
      'data" and "omitted because misleading" are different claims; got:\n%s'
      % _none)

# ── 4. The detector must be in the job, keyed on spread not a date ──
_src = open('D:/CommandCenter/signal_broadcaster.py', encoding='utf-8',
            errors='replace').read()
check("_ev_spans_eras" in _src,
      'the weekly job must detect a position-size discontinuity')
check("/ _mags[0] > 50" in _src,
      'the detector must key on the SPREAD between per-day magnitudes, not '
      'a hardcoded resize date — a future resize must disclose itself')
check("expectancy_omitted_reason" in _src,
      'the omission must carry a reason through to the card')

# ── 5. Behavioural: the threshold separates a resize from a good week ──
def spans(mags):
    m = sorted(abs(x) for x in mags if x)
    return bool(m and m[-1] / m[0] > 50)

check(spans([53.20, 0.03]) is True,
      'a $53/trade day beside a $0.03/trade day is a pool resize (1773x) '
      'and must be detected')
check(spans([12.0, 3.0, 45.0]) is False,
      'a normal week with a 15x spread between good and bad days must NOT '
      'be suppressed — that is variance, not a scale change')
check(spans([]) is False and spans([0.0, 0.0]) is False,
      'no data and all-flat days must not trip the detector')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  a dollar average across a resize is omitted, with its reason')
