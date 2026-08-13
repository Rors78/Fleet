"""One minimum-trade floor for the fleet, not a copy per bot.

When the operator set the pool to $210.53 (2026-08-13), the fleet stopped
trading. MIN_TRADE_USD was 100.00 — an absolute dust filter, deliberately
not a percentage (a percentage is what silently broke the fleet the LAST
time the pool was resized, per the comment on the constant).

At $210.53 the real sizing rules produce:
    per-trade cap (20%)        $42.11
    per-pair cap  (20%)        $42.11
    TurtleSue 10% pool share   $21.05
All BELOW a $100 floor, so every reserve was refused before any risk rule
could bind. The dust filter had become the binding constraint.

Lowering the Command Center constant was not enough: NexusBrain and
Arbitrageur each carried their OWN hardcoded `if size_usd < 100`, so they
would have kept refusing entries the pool would have accepted — the bot
never even asks. Three copies of one constant, two of them stale.

Both now import the fleet value, with a fallback to the OLD value if the
import fails: a path problem must not silently REMOVE a floor.
"""
import sys

sys.path.insert(0, 'D:/CommandCenter')

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


import command_center as cc
import fleet_config as fc

FLOOR = cc.MIN_TRADE_USD
POOL = fc.PORTFOLIO_TOTAL
LIM = fc.PORTFOLIO_LIMITS

# ── 1. The floor must sit below what the pool can actually size ──
_per_trade = POOL * LIM["max_per_trade_pct"] / 100.0
check(FLOOR < _per_trade,
      'the dust floor ($%.2f) must be BELOW the per-trade cap ($%.2f) or it '
      'becomes the binding constraint and the fleet cannot trade at all'
      % (FLOOR, _per_trade))

# TurtleSue sizes off a share of the pool — that share must clear the floor.
_share = 10.0          # turtlebot CONFIG["equity_pool_share_pct"]
_basis = POOL * _share / 100.0
check(_basis >= FLOOR,
      'TurtleSue sizes off %.0f%% of the pool = $%.2f, which must clear the '
      '$%.2f floor or it is refused before any Turtle rule applies'
      % (_share, _basis, FLOOR))

# ── 2. The floor must still filter genuine dust ──
check(FLOOR > 0,
      'a zero/negative floor is not a filter — dust positions produce '
      'signals too small to be actionable')

# ── 2b. The floor must survive fleet-intel RISK SCALING ──
# The floor is checked AFTER intel scaling, not before. A $5 floor looked
# fine against the $42.11 per-trade cap but refused TurtleSue's real $21.05
# basis once a live 0.21 risk multiplier reduced it to $4.42 — and it did so
# only on SOME pairs, because the multiplier is per-pair, so the refusal
# read as random rather than as a threshold. Verified against the running
# pool: live multipliers ran 0.15-0.39 on 2026-08-13.
_worst_mult = 0.15          # lowest live multiplier observed
_turtle_basis = POOL * 10.0 / 100.0
check(_turtle_basis * _worst_mult >= FLOOR,
      "TurtleSue's $%.2f basis scaled by the worst live risk multiplier "
      "(%.2f) is $%.2f, which must still clear the $%.2f floor — the floor "
      "is applied AFTER scaling"
      % (_turtle_basis, _worst_mult, _turtle_basis * _worst_mult, FLOOR))

# ── 3. No bot may carry its OWN hardcoded copy ──
for _bot, _path in (("nexusbrain", "D:/NexusBrain/nexus_brain.py"),
                    ("arbitrageur", "D:/Arbitrageur/arbitrageur.py")):
    _src = open(_path, encoding="utf-8", errors="replace").read()
    check("size_usd < 100:" not in _src,
          '[%s] still has a hardcoded `size_usd < 100` floor — lowering the '
          'fleet constant would not reach it, and the bot would refuse '
          'entries the pool accepts' % _bot)
    check("_MIN_TRADE_USD" in _src,
          '[%s] must read the fleet floor rather than its own copy' % _bot)
    # The fallback must match the CURRENT fleet floor, and must never be 0.
    # Pinning the old 100.0 would silently re-park a $210 pool if the import
    # failed; a 0 would remove the dust filter entirely. Both are wrong in
    # opposite directions, so assert the actual value.
    import re as _re
    _fb = _re.search(r'_MIN_TRADE_USD = ([0-9.]+)', _src)
    check(_fb is not None, '[%s] no import fallback found' % _bot)
    if _fb:
        _fbv = float(_fb.group(1))
        check(_fbv == FLOOR,
              '[%s] the import fallback ($%.2f) must match the fleet floor '
              '($%.2f) — a stale fallback re-parks the fleet the moment the '
              'import fails' % (_bot, _fbv, FLOOR))
        check(_fbv > 0,
              '[%s] the fallback must not be 0 — that removes the dust '
              'filter entirely' % _bot)

# ── 4. The floor must remain ABSOLUTE, not a fraction of the pool ──
# The constant's own comment records that a percentage is what broke the
# fleet the last time the pool was resized.
_cc_src = open("D:/CommandCenter/command_center.py", encoding="utf-8",
               errors="replace").read()
_i = _cc_src.find("MIN_TRADE_USD = ")
_decl = _cc_src[_i:_i + 60].split("\n")[0]
check("PORTFOLIO_TOTAL" not in _decl and "%" not in _decl,
      'MIN_TRADE_USD must stay an absolute dollar figure; got %r' % _decl)

# ── 5. Config and live state must agree on the pool size ──
import json
_pf = json.load(open("D:/CommandCenter/portfolio.json", encoding="utf-8"))
check(abs(_pf.get("total", 0) - POOL) < 0.01,
      'portfolio.json total ($%.2f) and fleet_config.PORTFOLIO_TOTAL '
      '($%.2f) must match — the file wins at runtime, so a divergence '
      'means deleting it silently changes the pool size'
      % (_pf.get("total", 0), POOL))

# ── 6. Long-only, set by the operator 2026-08-13 ──
check(fc.FLEET_LONG_ONLY is True,
      'the fleet is configured long-only; FLEET_LONG_ONLY must be True')
check(fc.direction_allowed("LONG")[0] is True,
      'LONG entries must still be allowed')
check(fc.direction_allowed("BUY")[0] is True,
      'BUY is a long synonym and must be allowed')
for _d in ("SHORT", "SELL"):
    _ok, _why = fc.direction_allowed(_d)
    check(_ok is False,
          'a %s entry must be refused while the fleet is long-only' % _d)
    check("LONG_ONLY" in _why,
          'the refusal must name the policy so the denial is diagnosable; '
          'got %r' % _why)

# The re-entry escape hatch must survive: the policy is "open no new
# shorts", not "liquidate open ones". Blocking a re-reservation makes a bot
# read its own open position as unfunded and force-close it at market —
# turning a config change into an unintended liquidation.
for _d in ("SHORT", "SELL"):
    check(fc.direction_allowed(_d, is_reentry=True)[0] is True,
          'an already-open %s must be able to re-claim its capital on a '
          'restart, or flipping this flag liquidates live positions' % _d)

# ── 7. Every trading bot must size off the POOL, not a local balance ──
# Bots sizing off a fixed $10,000 paper balance asked for the same dollars
# whatever the shared pool held — 6 of the denials in the 20 minutes after
# the $210.53 resize were bots requesting more than the pool could ever
# grant. A share of the pool scales with it; a hardcoded balance goes stale
# at every resize.
_POOL_SIZED = {
    "turtlesue":   ("D:/TurtleSue/turtlebot.py",    "equity_pool_share_pct"),
    "confluence":  ("D:/Confluence/confluence.py",  "RISK_POOL_SHARE_PCT"),
    "rubberband":  ("D:/Rubberband/rubberband.py",  "POOL_SHARE_PCT"),
    "nexusbrain":  ("D:/NexusBrain/nexus_brain.py", "POOL_SHARE_PCT"),
    "arbitrageur": ("D:/Arbitrageur/arbitrageur.py","POOL_SHARE_PCT"),
}
for _bot, (_path, _const) in _POOL_SIZED.items():
    _src = open(_path, encoding="utf-8", errors="replace").read()
    check(_const in _src,
          '[%s] must declare a pool share constant (%s)' % (_bot, _const))
    check("pool_total()" in _src,
          '[%s] must read pool_total() — available() moves every time '
          'another bot opens a position, so sizing off it would resize this '
          'bot because something unrelated happened' % _bot)
    check("_sizing_basis" in _src or "_risk_basis" in _src
          or "sizing_basis" in _src,
          '[%s] must route sizing through a basis helper so the pool read '
          'has ONE fallback path, not one per call site' % _bot)

# ── 8. Every bot's smallest POST-SCALE request must clear the floor ──
# Computed from each bot's real formula at the worst live multiplier. This
# is the check that would have caught both of my wrong floor values.
_WORST_MULT = 0.15
_sizes = {
    "turtlesue":   POOL * 0.10,                  # share, then risk/N
    "confluence":  POOL * 0.10 * 0.005 / 0.03,   # share * risk / stop_pct
    "rubberband":  POOL * 1.00 * 0.05,           # share * flat risk pct
    "nexusbrain":  POOL * 1.00 * 0.05,           # max_position_pct of basis
    "arbitrageur": POOL * 1.00 * 0.05,           # share * flat risk pct
}
_cap = POOL * LIM["max_per_trade_pct"] / 100.0
for _bot, _req in _sizes.items():
    check(_req * _WORST_MULT >= FLOOR,
          '[%s] requests $%.2f, which fleet intel scales to $%.2f at the '
          'worst live multiplier — below the $%.2f floor, so this bot '
          'cannot trade' % (_bot, _req, _req * _WORST_MULT, FLOOR))
    check(_req <= _cap,
          '[%s] requests $%.2f, above the $%.2f per-trade cap — it would be '
          'refused before sizing even applies' % (_bot, _req, _cap))

# ── 9. Per-bot position BOUNDS must scale with the pool too ──
# Confluence clamped its computed size with MIN_POSITION_USD = 500.0 and
# MAX_POSITION_USD = 150000.0 — dollar figures sized for a $1M pool. At
# $210.53 the minimum FORCED every position to $500, which the pool then
# refused ("0.00 deployed + 500.00 requested > 126.32 cap"). The bot
# computed a correct $3.51 and its own floor overrode it: 10 denials in
# four minutes and no trades. Same lesson as MIN_TRADE_USD, one layer up.
_cf = open("D:/Confluence/confluence.py", encoding="utf-8",
           errors="replace").read()
check("MIN_POSITION_USD = " not in _cf,
      "Confluence still clamps to a hardcoded dollar minimum — it goes "
      "stale at every pool resize and overrides the pool-share sizing")
check("MIN_POSITION_PCT_OF_POOL" in _cf and "MAX_POSITION_PCT_OF_POOL" in _cf,
      "Confluence's position bounds must be fractions of the pool")

# The bounds must not fight the fleet caps at the current pool size.
_min_pos = POOL * 0.005
_max_pos = POOL * 0.15
check(_min_pos >= FLOOR,
      "Confluence's minimum position ($%.2f) must clear the fleet dust "
      "floor ($%.2f)" % (_min_pos, FLOOR))
check(_max_pos <= POOL * LIM["max_per_trade_pct"] / 100.0,
      "Confluence's maximum position ($%.2f) must sit under the per-trade "
      "cap ($%.2f), or its own ceiling is unreachable"
      % (_max_pos, POOL * LIM["max_per_trade_pct"] / 100.0))

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  one floor, below the caps, absolute, and no stale bot copies')
