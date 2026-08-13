"""A forced exit at a fabricated price is not a trade, even when priced.

TurtleSue's reconcile closes positions whose reservations cannot be
re-reserved, and sets the exit price TO pos.avg_entry — so P/L is zero by
construction. On 2026-08-13 the CC boot-race sweep released two live XRP
reservations; the next restart's re-reserve was capacity-denied and the
forced close landed in the durable expectancy store as a "flat" with
exit_price 1.0157 (== the weighted avg entry, a tautology) while the
position's real unrealized P/L was discarded unmeasured.

The writer guard already refused UNPRICED closes (entry 0 / size 0). This
pins its second clause: an exit whose price is fabricated (stale_reservation)
is unmeasurable no matter how real the entry and size are.

The test execs the SHIPPED guard lines from turtlebot.py — it does not
reimplement them, so a reverted guard fails here, not a stale copy.
"""
import re
import sys
import textwrap
from types import SimpleNamespace

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


src = open('D:/TurtleSue/turtlebot.py', encoding='utf-8', errors='replace').read()

# ── 1. Extract and run the shipped guard ──
m = re.search(
    r'^[ ]*_fabricated_exit = .*\n(?:.*\n)*?[ ]*_measurable = \((?:.*\n)*?.*\)\n',
    src, re.M)
check(m is not None, 'could not locate the _fabricated_exit/_measurable guard '
                     'in turtlebot.py — was it renamed or removed?')

if m:
    guard = textwrap.dedent(m.group(0))

    def run_guard(avg_entry, total_size, exit_type):
        ns = {
            'pos': SimpleNamespace(avg_entry=avg_entry, total_size=total_size),
            'exit_info': {'type': exit_type},
        }
        exec(guard, ns)
        return ns['_measurable']

    check(not run_guard(1.0157, 44858.36, 'stale_reservation'),
          'a stale_reservation exit must be UNMEASURABLE even with a real '
          'entry and size — its exit price IS the avg entry, so the $0.00 '
          'is a tautology, not a break-even')
    check(run_guard(1.0157, 44858.36, 'stop_hit'),
          'a normal priced exit must still be measurable — the guard must '
          'not neuter real trades')
    check(not run_guard(0, 0, 'stop_hit'),
          'an unpriced close must stay unmeasurable regardless of reason '
          '(the original 2026-08-08 guard)')
    check(not run_guard(0, 0, 'stale_reservation'),
          'unpriced AND fabricated must stay unmeasurable')

# ── 2. The refusal must be visible: WARNING, not INFO ──
# The first version logged at INFO; turtlesue's root logger sits at WARNING,
# so the line never reached disk and the refusal was invisible.
check('logging.warning("[turtlesue] %s close not recorded' in src,
      'the not-recorded log line must be WARNING level — at INFO it never '
      'reaches disk under the root logger and the refusal is invisible')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  fabricated-price exits are refused by the shipped writer guard')
