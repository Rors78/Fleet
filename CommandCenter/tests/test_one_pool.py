"""One bot, one pool. No bot holds a balance of its own.

OPERATOR RULE, stated 2026-08-17: "THIS IS ONE BOT. ONE BOT ONLY. NOBODY GETS
A FUCKING DEMO BALANCE FOR ANYTHING. TREAT THE BOT AS ONE MIND BOT."

The fleet is 18 processes and ONE mind. There is exactly one pool, tracked by
Command Center. A per-bot "equity", "paper balance", or "starting equity" is
not a smaller truth or a safe default -- it is a second, fake source of truth
that competes with the real one, and every place it appeared it eventually
got used for something real.

WHAT WAS FOUND, four bots each with a private copy of the same defect:

    TurtleSue     CONFIG["starting_equity"] = 10000.00
                  _sizing_basis falls back to self.equity
                  drawdown = (starting_equity - equity)/starting_equity
                  -- a REAL size cut computed from a FAKE denominator
    Rubberband    PAPER_BALANCE = 10_000.0, same fallback
                  comment at 884: equity "reset to PAPER_BALANCE, restoring
                  full size after a drawdown" -- a brake that forgives itself
                  on every restart
    Arbitrageur   INITIAL_EQUITY = 10_000.0, same fallback
    Confluence    FALLBACK_EQUITY_USD = 10000.0 (removed), then
                  NOTIONAL_PNL_BASE = 10000.0 left behind in its place

Measured cost of the Confluence one against the live $209.88 pool: basis
$10,000 instead of $20.99, position $2,500 instead of $5.25 -- 476x, a single
position at 12x the entire pool, with MAX_POSITION_PCT_OF_POOL inflating in
lockstep because the cap is back-derived from the basis.

Summed across five bots these fake balances made Command Center publish
aggregate.total_equity = $49,631.58 against a $209.88 pool, which three
dashboard sites then rendered as the POOL figure.

THE RULE THIS TEST ENFORCES:

  1. No bot module defines a hardcoded starting/paper/initial/demo balance.
  2. No sizing path falls back to a bot-local equity when the pool is
     unreadable. Unreadable fails ARMED: return None, skip the trade.
  3. Drawdown and risk fractions are measured against the pool, never
     against a per-bot balance.

This test reads SHIPPED SOURCE. It does not re-implement the logic, and it
covers every trader at once so a fix cannot land for one bot and miss its
siblings -- which is exactly how this defect survived: it was fixed in
Confluence first and the other three were never looked at.
"""
import os
import re
import sys

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# Traders that size positions. Read from fleet_config so a new bot is covered
# automatically rather than needing this list updated.
sys.path.insert(0, 'D:/CommandCenter')
try:
    import fleet_config as _fc
    _BOTS = {bid: b for bid, b in _fc.BOTS.items() if b.get("role") == "trader"}
except Exception as _e:                                    # pragma: no cover
    _BOTS = {}
    FAIL.append('could not read fleet_config.BOTS: %r' % (_e,))


def _entry_sources(bot):
    """Every .py in the bot's directory -- the balance may not be in main."""
    d = bot.get("dir")
    if not d or not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".py"):
            p = os.path.join(d, fn)
            try:
                out.append((p, open(p, encoding='utf-8', errors='replace').read()))
            except Exception:
                pass
    return out


def _strip(src):
    """Drop comments and docstrings: prose explaining the removed constant
    must not fail the test that removed it."""
    src = re.sub(r'"""(?:.|\n)*?"""', '', src)
    src = re.sub(r"'''(?:.|\n)*?'''", '', src)
    return re.sub(r'#[^\n]*', '', src)


# A hardcoded balance: an assignment of a round four-plus-figure literal to a
# name that means "money this bot has". Catches 10000, 10_000, 100000.0 etc.
_BAL_NAME = (r'(?:PAPER_BALANCE|INITIAL_EQUITY|STARTING_EQUITY|DEFAULT_EQUITY'
             r'|FALLBACK_EQUITY(?:_USD)?|NOTIONAL_PNL_BASE|DEMO_BALANCE'
             r'|START_BALANCE|INITIAL_BALANCE|PAPER_EQUITY)')
_BAL_ASSIGN = re.compile(
    r'^\s*%s\s*=\s*[0-9_]{4,}(?:\.[0-9]+)?' % _BAL_NAME, re.M)
# Same thing in lower case as a dataclass field or default arg. NexusBrain
# spelled it `initial_capital: float = 10000.0` -- a fifth name for the fifth
# copy of this defect, which the upper-case list above did not catch. Match on
# what it MEANS, not on one project's chosen spelling.
_BAL_FIELD = re.compile(
    r'^\s*(?:initial_capital|starting_capital|paper_capital|initial_equity'
    r'|starting_equity|paper_balance|start_equity)\s*'
    r'(?::\s*float\s*)?=\s*[0-9_]{4,}(?:\.[0-9]+)?', re.M)
_BAL_KEY = re.compile(
    r'["\'](?:starting_equity|paper_balance|initial_equity|default_equity)["\']'
    r'\s*:\s*[0-9_]{4,}(?:\.[0-9]+)?')

for _bid, _bot in sorted(_BOTS.items()):
    for _path, _raw in _entry_sources(_bot):
        _src = _strip(_raw)
        _n = os.path.basename(_path)

        # ── 1. No hardcoded balance constant ──
        for _m in _BAL_ASSIGN.finditer(_src):
            check(False,
                  '%s/%s defines a hardcoded balance: %r. No bot holds money '
                  'of its own -- there is ONE pool, read via '
                  'PortfolioClient.pool_total()'
                  % (_bid, _n, _m.group(0).strip()))
        for _m in _BAL_KEY.finditer(_src):
            check(False,
                  '%s/%s carries a hardcoded balance in config: %r'
                  % (_bid, _n, _m.group(0).strip()))
        for _m in _BAL_FIELD.finditer(_src):
            check(False,
                  '%s/%s declares a hardcoded starting capital: %r -- the '
                  'pool is the only capital there is'
                  % (_bid, _n, _m.group(0).strip()))

        # ── 2. Sizing must not fall back to a bot-local equity ──
        _sb = re.search(
            r'def _sizing_basis\(self[^\n]*\n(.*?)(?=\n    def |\nclass )',
            _raw, re.S)
        if _sb:
            _body = _strip(_sb.group(1))
            check(not re.search(r'return\s+self\.(?:equity|starting_equity'
                                r'|notional_equity|peak_equity)', _body),
                  '%s/%s _sizing_basis falls back to a bot-local equity when '
                  'the pool is unreadable. An unreadable pool is not a pool '
                  'of some other size -- it is unknown, and unknown must '
                  'refuse to size (return None), never size off a stand-in'
                  % (_bid, _n))
            check('None' in _body,
                  '%s/%s _sizing_basis cannot express "pool unreadable". It '
                  'must be able to return None so the caller skips the trade'
                  % (_bid, _n))

        # ── 3. Drawdown must not be measured against a fake denominator ──
        # TurtleSue computed (starting_equity - equity)/starting_equity off a
        # $10,000 constant and applied the result to REAL position sizing.
        for _m in re.finditer(
                r'[^\n]*\/\s*self\.starting_equity[^\n]*', _src):
            check(False,
                  '%s/%s divides by self.starting_equity: %r -- a real size '
                  'cut computed from a fake denominator'
                  % (_bid, _n, _m.group(0).strip()[:90]))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  one bot, one pool -- no bot carries a balance of its own')
