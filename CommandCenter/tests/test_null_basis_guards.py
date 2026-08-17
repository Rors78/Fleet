"""Every consumer of a nullable sizing function must guard against None.

Making _sizing_basis / _adjusted_equity return None when the pool is
unreadable fixed a 476x oversizing bug, but it converted a WRONG NUMBER into
a POSSIBLE CRASH at every call site that did arithmetic on the result. A
snapshot endpoint that raises takes the bot's whole API down, which is worse
than the display bug it replaced.

This is the sibling problem in its usual form. Fixing the producer is the
easy half; the call sites are scattered and easy to miss. TurtleSue alone had
THREE _adjusted_equity() consumers -- entry, pyramid, and a market-display
loop -- and the third was found only after the first two were already fixed.
Rubberband had a round(pnl_pct, 2) left behind on a value that had just
become None.

So: find every call to a nullable function across every trader, and require
that the result is either type-checked or explicitly compared to None before
anything arithmetic happens to it. Reads shipped source; covers all traders
from fleet_config so a new bot is included automatically.
"""
import os
import re
import sys

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


try:
    import fleet_config as _fc
    _BOTS = {b: v for b, v in _fc.BOTS.items() if v.get("role") == "trader"}
except Exception as _e:
    _BOTS = {}
    FAIL.append('could not read fleet_config.BOTS: %r' % (_e,))

# Functions that may now return None when the pool is unreadable.
NULLABLE = ("_sizing_basis", "_adjusted_equity")

# A guard looks like one of these within a few lines of the assignment.
GUARD = re.compile(
    r'is None|is not None|isinstance\(\s*(?:_?\w+)\s*,\s*\(int, float\)\)'
    r'|not isinstance')


def _strip(src):
    src = re.sub(r'"""(?:.|\n)*?"""', '', src)
    src = re.sub(r"'''(?:.|\n)*?'''", '', src)
    return re.sub(r'#[^\n]*', '', src)


for _bid, _bot in sorted(_BOTS.items()):
    _d = _bot.get("dir")
    if not _d or not os.path.isdir(_d):
        continue
    for _fn in sorted(os.listdir(_d)):
        if not _fn.endswith(".py"):
            continue
        _path = os.path.join(_d, _fn)
        try:
            _raw = open(_path, encoding='utf-8', errors='replace').read()
        except Exception:
            continue
        _lines = _strip(_raw).split('\n')

        for _i, _line in enumerate(_lines):
            for _f in NULLABLE:
                # Only assignments; the def itself and guarded calls are fine.
                _m = re.search(r'(\w+)\s*=\s*(?:self\.)?%s\(\)' % _f, _line)
                if not _m:
                    continue
                _var = _m.group(1)
                # Every later mention of the variable in this function must be
                # guarded somewhere. The window has to be generous: in a
                # snapshot dict the assignment and its guarded uses can sit 25
                # lines apart, and a tight window flags correct code (it fired
                # on Confluence, whose uses are all isinstance-checked).
                _window = '\n'.join(_lines[_i:_i + 40])
                _uses = len(re.findall(re.escape(_var), _window)) - 1
                if _uses == 0:
                    check(False,
                          '%s/%s:%d assigns %s = %s() and never uses it -- a '
                          'dead network call to Command Center on every '
                          'invocation: %r'
                          % (_bid, _fn, _i + 1, _var, _f, _line.strip()[:80]))
                    continue
                check(bool(GUARD.search(_window)),
                      '%s/%s:%d assigns %s = %s() and does not guard it. '
                      'That call returns None when the pool is unreadable, so '
                      'the next arithmetic or round() on it raises and takes '
                      'the endpoint down: %r'
                      % (_bid, _fn, _i + 1, _var, _f, _line.strip()[:80]))

        # round() on a bare name that was assigned from a nullable call
        # anywhere in the file -- round(None) is a TypeError.
        for _i, _line in enumerate(_lines):
            _m = re.search(r'round\(\s*(_?\w*(?:basis|equity|pool)\w*)\s*,', _line)
            if not _m:
                continue
            _var = _m.group(1)
            if not re.search(r'%s\s*=\s*(?:self\.)?(?:%s)\(\)'
                             % (re.escape(_var), '|'.join(NULLABLE)), '\n'.join(_lines)):
                continue
            _window = '\n'.join(_lines[max(0, _i - 3):_i + 2])
            check(bool(GUARD.search(_window)),
                  '%s/%s:%d calls round() on %s, which may be None: %r'
                  % (_bid, _fn, _i + 1, _var, _line.strip()[:80]))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  every nullable sizing consumer guards against an unreadable pool')
