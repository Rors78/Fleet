"""The fleet win rate must be POOLED, and its n must be its own denominator.

The always-on dashboard header rendered "WR: 66.7% (n=66)" to investors on
2026-08-25. Both halves were wrong, for two independent reasons.

1. THE DENOMINATOR WAS NOT THE RATE'S POPULATION.
   A bot's win_rate is computed over DECIDED trades (wins+losses). Its
   total_trades additionally counts FLAT trades ($0.00 moves), which decide
   nothing. _compute_aggregate reconstructed each bot's wins as
   win_rate x total_trades, so every flat trade was credited as a win at the
   bot's win rate.

   Live at the time: gridzilla reported win_rate 100.0 with total_trades 33,
   of which 16 were decided and 17 were FLAT. 100% x 33 = 33 wins for a bot
   that had won 16. Those 17 invented wins were on their own enough to carry
   the fleet figure above 50%.

2. INTEL BOTS WERE COUNTED AS TRADERS.
   trinity is role="support" -- an intel-only scanner that never trades. Its
   wins/losses are SIGNAL TRACK resolutions, and its normalizer maps them onto
   win_rate/total_trades, so "2 trades at 100%" entered the fleet TRADING win
   rate. A signal that resolved favourably is not a trade that made money.

And the displayed n was a third lie on top: 66 was a SUM OF TRADE COUNTS
pasted beside a rate never computed over those 66 trades.

These pins are behavioural (they run the real function against constructed
bot payloads), not string greps, so they survive refactoring.
"""
import sys

sys.path.insert(0, 'D:/CommandCenter')

import command_center as cc  # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def _bot(norm, alive=True):
    return {"alive": alive, "normalized": norm}


# ── The live 2026-08-25 shape that produced "66.7% (n=66)" ──
LIVE = {
    # 100% over 16 DECIDED; 17 of its 33 trades were flat.
    "gridzilla": _bot({"win_rate": 100.0, "total_trades": 33,
                       "win_rate_decided": 16}),
    "confluence": _bot({"win_rate": 29.6, "total_trades": 27}),
    "turtlesue": _bot({"win_rate": 25.0, "total_trades": 4}),
    # role="support" -- intel only. Signal resolutions, not trades.
    "trinity": _bot({"win_rate": 100.0, "total_trades": 2}),
}

agg = cc._compute_aggregate(LIVE)
wr = agg.get("avg_win_rate")
n = agg.get("win_rate_n")

# ── 1. The exact defect must not come back ──
check(not (wr is not None and abs(wr - 66.666666) < 0.01),
      'the fleet win rate is 66.67% again -- that is the exact figure the '
      'flat-inflated reconstruction produced (44 "wins" / 66 trades, where '
      '17 of those wins were gridzilla flats and 2 were trinity signals)')

check(n != 66,
      'win_rate_n is 66 -- that is the SUM OF TRADE COUNTS, not the number '
      'of decided trades the rate was computed over')

# ── 2. n must be the rate's actual denominator ──
check(n is not None,
      'the aggregate must publish win_rate_n; a rate whose denominator the '
      'display cannot name has to render without an n, and nothing can do '
      'that if the backend never says what the denominator was')

wins = agg.get("win_rate_wins")
check(isinstance(wins, int) and isinstance(n, int) and n > 0,
      'win_rate_wins and win_rate_n must both be published so the rate is '
      'reconstructible and auditable, not asserted')

if isinstance(wins, int) and isinstance(n, int) and n > 0:
    check(abs(wr - (wins / n * 100.0)) < 1e-6,
          'avg_win_rate must equal win_rate_wins / win_rate_n -- if it does '
          'not, the published n is not the denominator of the published '
          'rate, which is the whole defect this file pins')

# ── 3. Flats must be out of the denominator ──
# gridzilla contributes its 16 decided, NOT its 33 total.
# confluence 27 + turtlesue 4 + gridzilla 16 = 47. trinity excluded.
check(n == 47,
      'expected win_rate_n == 47 (gridzilla 16 DECIDED + confluence 27 + '
      'turtlesue 4; trinity excluded as intel-only), got %r -- 64 would mean '
      "gridzilla's 17 flats are still inside the denominator, 66 would mean "
      'trinity is still being counted too' % (n,))

check(wins == 25,
      'expected 25 pooled wins (gridzilla 16 + confluence 8 + turtlesue 1), '
      'got %r -- 33 for gridzilla would mean flats are still counted as '
      'wins at its 100%% rate' % (wins,))

# ── 4. Intel/support bots must never enter the TRADING win rate ──
NO_TRINITY = {k: v for k, v in LIVE.items() if k != "trinity"}
agg_nt = cc._compute_aggregate(NO_TRINITY)
check(agg_nt.get("win_rate_n") == n and agg_nt.get("win_rate_wins") == wins,
      'removing trinity changed the fleet win rate, so an intel-only bot is '
      'still contributing signal resolutions to the fleet TRADING win rate')

check("trinity" not in cc._TRADER_IDS,
      'trinity (role="support", an intel-only scanner) must not be in the '
      'trader set used for the fleet win rate')

# ── 5. A bot that decided nothing is UNMEASURED, not 0% ──
ALL_FLAT = {"turtlesue": _bot({"win_rate": 0.0, "total_trades": 5,
                               "win_rate_decided": 0})}
agg_flat = cc._compute_aggregate(ALL_FLAT)
check(agg_flat.get("avg_win_rate") is None
      and agg_flat.get("win_rate_n") is None,
      'a fleet whose only trader decided nothing (5 trades, all flat) must '
      'report win rate UNMEASURED -- None, not 0%% over n=5. Got wr=%r n=%r'
      % (agg_flat.get("avg_win_rate"), agg_flat.get("win_rate_n")))

# ── 6. An assumed denominator must be disclosed, not presented as exact ──
check(agg.get("win_rate_n_exact") is False,
      'confluence and turtlesue publish no decided count, so their flats may '
      'still sit inside n -- win_rate_n_exact must be False to say so. '
      'Presenting an assumed denominator as an exact one is the same class '
      'of error as the original defect')

EXACT = {"gridzilla": _bot({"win_rate": 100.0, "total_trades": 33,
                            "win_rate_decided": 16})}
check(cc._compute_aggregate(EXACT).get("win_rate_n_exact") is True,
      'when every contributing bot reports a decided count, win_rate_n_exact '
      'must be True -- otherwise the caveat is permanent and stops carrying '
      'information')

# ── 7. Empty fleet reports unmeasured, never a fabricated zero ──
agg_empty = cc._compute_aggregate({})
check(agg_empty.get("avg_win_rate") is None
      and agg_empty.get("win_rate_n") is None,
      'an empty fleet must report None/None, not 0%% over n=0')

# ── 8. The dashboard must pair the rate with win_rate_n, never total_trades ──
_html = open('D:/CommandCenter/command_center_v4.html',
             encoding='utf-8', errors='replace').read()

check('agg.win_rate_n' in _html,
      'the dashboard never reads win_rate_n, so whatever n it prints beside '
      'the fleet win rate is not that rate\'s denominator')

# The header chip specifically: find the hWR textContent assignment and prove
# it does not build its "(n=" from total_trades. The n may be held in a local,
# so look at the statement plus the lines just above that bind it.
_i = _html.find('wrEl.textContent')
check(_i > 0, 'could not locate the header WR chip assignment (wrEl.textContent)')
_chip = _html[_i:_i + 260] if _i > 0 else ''
_lead = _html[max(0, _i - 200):_i] if _i > 0 else ''

check('total_trades' not in _chip,
      'the header WR chip still builds its sample size from total_trades -- '
      'that is the "(n=66)" defect exactly; it must use win_rate_n')

# Whatever identifier supplies the n must trace back to win_rate_n.
_ok = 'win_rate_n' in _chip or 'win_rate_n' in _lead
check(_ok,
      'the header WR chip must take its n from agg.win_rate_n, directly or '
      'via a local bound to it immediately above')

if FAIL:
    for f in FAIL:
        print('FAIL  ' + f)
    sys.exit(1)
print('ok  fleet win rate is pooled over decided trades and n is its '
      'real denominator')
