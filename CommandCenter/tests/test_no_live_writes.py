"""No test may write to live fleet state.

I wrote a test that built ExpectancyTracker with __new__ "to avoid disk",
recorded two probe bots, and did not notice that record_trade() calls
_save() itself. It wrote logs/expectancy.json with only

    {"probe_wins_only": [...], "probe_loss_only": [...]}

erasing the real trade history for gridzilla, confluence and turtlesue. The
newest backup was 11 days stale, so the store had to be rebuilt from the
durable event_bus TRADE_CLOSE record (54 trades recovered; confluence's
5W/11L matched the pre-clobber figures exactly, which is what proved the
rebuild faithful).

The damage was invisible at the time: the test PASSED, and the loss only
surfaced when a later fleet restart made the dashboard read 28 trades where
it had read 43.

A test that constructs a persistent object must redirect its persistence
BEFORE writing anything. This checks that every test doing so has done it,
by looking for the pattern rather than trusting memory:

  - any test that instantiates a class with a PERSIST_PATH-style class
    attribute must also assign that attribute to a temp path, or must never
    call a method that persists.

Kept deliberately simple and source-based: the alternative is running each
test under a filesystem sandbox, which this suite has no harness for.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# Live state files a test must never write.
LIVE = (
    'logs/expectancy.json',
    'logs/portfolio.json',
    'portfolio.json',
    'logs/daily_state.json',
    'logs/cc_state.json',
)

# Classes known to persist on their own, and the attribute to redirect.
PERSISTERS = {
    'ExpectancyTracker': 'PERSIST_PATH',
}

for _fn in sorted(os.listdir(HERE)):
    if not _fn.startswith('test_') or not _fn.endswith('.py'):
        continue
    if _fn == os.path.basename(__file__):
        continue
    _path = os.path.join(HERE, _fn)
    try:
        _src = open(_path, encoding='utf-8', errors='replace').read()
    except Exception:
        continue
    _code = re.sub(r'"""(?:.|\n)*?"""', '', _src)
    _code = re.sub(r'#[^\n]*', '', _code)

    # 1. A persisting class must have its path redirected.
    for _cls, _attr in PERSISTERS.items():
        if not re.search(r'\b%s\b' % _cls, _code):
            continue
        _redirected = re.search(
            r'%s\.%s\s*=' % (_cls, _attr), _code) or re.search(
            r'\b%s\s*=\s*(?:os\.path\.join\()?[^\n]*(?:tmp|temp)' % _attr,
            _code, re.I)
        check(bool(_redirected),
              '%s uses %s without redirecting %s.%s to a temp path. That '
              'class persists on its own inside record_trade(), so the test '
              'writes LIVE fleet state -- this is exactly how '
              'logs/expectancy.json was erased.'
              % (_fn, _cls, _cls, _attr))

    # 2. No test may open a live state file for writing.
    for _live in LIVE:
        _base = _live.split('/')[-1]
        for _m in re.finditer(
                r"open\(\s*[^)]*%s[^)]*['\"]\s*,\s*['\"][wa]" % re.escape(_base),
                _code):
            check(False,
                  '%s opens %s for writing: %r'
                  % (_fn, _base, _m.group(0)[:70]))
        for _m in re.finditer(
                r"json\.dump\([^)]*%s" % re.escape(_base), _code):
            check(False, '%s json.dumps into %s' % (_fn, _base))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  no test writes live fleet state')
