"""
EXPECTANCY TRACKER -- The only number that matters.

E[V] = (Win Rate x Average Win) - (Loss Rate x Average Loss)

If E[V] > 0, the signal makes money over time. If E[V] < 0, it loses.
Win rate alone is MEANINGLESS without knowing win/loss sizes.

GROSS SEMANTICS (since 2026-07-30): the fleet is a signal product — it
never trades real money, and subscribers pay whatever fees their own
exchange charges. All P/L and expectancy here are GROSS price movement,
the signal's raw truth. No fee is calculated or deducted anywhere in
this module. Trades recorded before 2026-07-30 in logs/expectancy.json
were recorded net-of-fees (their stored `net_pnl` has fees subtracted);
that history is NOT rewritten — stats methods read the stored
`gross_pnl` field for every trade, old or new, so all reported numbers
are gross.

Tracks per-bot and fleet-wide:
- expectancy_per_trade (gross price movement)
- avg_win, avg_loss
- profit_factor (gross wins / gross losses)
- avg_r (average R-multiple if risk per trade is known)
- max_consecutive_losses
- time_in_winning_trades vs time_in_losing_trades
- best_trade, worst_trade

Usage:
    from expectancy import ExpectancyTracker
    tracker = ExpectancyTracker()

    # Record each closed trade:
    tracker.record_trade('trekbot', 'BTC/USD', 'LONG',
                         entry_price=84000, exit_price=84500,
                         size_usd=500, duration=3600)

    # Get expectancy:
    stats = tracker.get_bot_stats('trekbot')
    fleet = tracker.get_fleet_stats()
"""

import json
import os
import time

try:
    from fleet_config import bot_registry_list
except ImportError:  # pragma: no cover - CC always has it on the path
    bot_registry_list = None


def _fleet_member_ids() -> set:
    """Current fleet membership, from the ONE source of truth.

    `fleet_config.bot_registry_list()` already defines who is in the fleet
    (it excludes port-less entries like bot_responder). Expectancy used to
    iterate whatever had trade history, which kept counting `trekbot` long
    after it was retired to standalone GoldenEye — its 6 trades polluted both
    the rankings and the fleet-wide expectancy headline.

    Routing through the registry rather than a skip-list means the next
    retirement drops out automatically. A second exclusion list would make
    three answers to "who is in the fleet"; there must be one.

    Returns an empty set if the registry is unavailable, and callers then
    fall back to unfiltered behavior rather than silently reporting zero.
    """
    if bot_registry_list is None:
        return set()
    try:
        return {b["id"] for b in bot_registry_list()}
    except Exception:
        return set()


def _wilson_ci_95(wins, n):
    """95% Wilson score interval for a win rate, as (lo_pct, hi_pct).

    Exists because the verdicts here were being read as measurements:
    gridzilla's "PROFITABLE" at 5W/0L carries a CI of [56.6%, 100%], and a
    fair coin produces the fleet's 5W/1L 10.9% of the time. The interval is
    what n=5 actually establishes — shipping the verdict without it invites
    certainty the sample cannot support.
    """
    if not n:
        return None
    z = 1.959964  # 95%
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / denom
    return (round(max(0.0, centre - margin) * 100, 1),
            round(min(1.0, centre + margin) * 100, 1))


# Below this many DECIDED trades a PROFITABLE/LOSING verdict is a coin-read,
# not a measurement: 'LOSING' was being published from n=1 and 'PROFITABLE'
# from 5W/0L, where profit_factor 999 is a division-by-zero placeholder.
# ~188 trades are needed to resolve an effect of the currently observed size
# at 80% power; 10 is merely where a verdict stops being embarrassing.
MIN_VERDICT_N = 10


def _rank_row(bid, s):
    """One bot_rankings row, verdict scaled to what the sample supports.

    UNMEASURED covers two cases that must not be told apart by a verdict:
    zero trades, and trades that were ALL flat ($0.00 capital movements —
    decided == 0 either way). Live defect this closes: turtlesue's two
    stale-reservation cleanups rendered as "2 losses, 0% win rate,
    EARLY_NEGATIVE" — a losing verdict from closes that measured nothing.
    Below MIN_VERDICT_N decided trades the verdict is EARLY_* (the sign of
    the expectancy without the certainty); the Wilson CI says the rest.
    """
    wins = s.get('wins', 0)
    losses = s.get('losses', 0)
    decided = wins + losses
    ev = s.get('expectancy_per_trade')
    if decided == 0 or ev is None:
        verdict = 'UNMEASURED'
    elif decided >= MIN_VERDICT_N:
        verdict = 'PROFITABLE' if ev > 0 else 'LOSING'
    else:
        verdict = 'EARLY_POSITIVE' if ev > 0 else 'EARLY_NEGATIVE'
    return {'bot': bid,
            'expectancy': ev if s.get('total_trades') else None,
            'trades': s.get('total_trades', 0),
            'flat': s.get('flat', 0),
            'verdict': verdict,
            'n_decided': decided,
            'win_rate_ci_95': _wilson_ci_95(wins, decided)}


class ExpectancyTracker:
    PERSIST_PATH = os.path.join(os.path.dirname(__file__),
                                'logs', 'expectancy.json')

    def __init__(self):
        self.trades = {}   # {bot_id: [trades]}
        self.max_trades_per_bot = 500
        self._load()

    # ── Trade Recording ─────────────────────────────────────────────

    def record_trade(self, bot_id, pair, direction, entry_price, exit_price,
                     size_usd, duration, fee_rate=0.0,
                     risk_per_trade=None, realized_pnl=None,
                     realized_fees=None, trade_id=None):
        """
        Record a completed trade. GROSS semantics — no fee deduction.

        Two recording modes:

        1. PRICE-DRIVEN (entry_price + exit_price > 0): the legacy path.
           Computes gross_pnl from raw price movement.

        2. PNL-DRIVEN (realized_pnl is not None): bypasses the formula and
           uses the reported PnL directly. Use this for bots that report
           PnL but not entry/exit prices. Avoids the ZeroDivisionError
           that silently dropped every trade where the bot omitted prices.

        Args:
            entry_price/exit_price: required for PRICE-DRIVEN mode
            realized_pnl: gross PnL from price movement — triggers
                PNL-DRIVEN mode
            trade_id: optional dedup key (e.g. reservation_id) — if a trade
                with the same trade_id already exists for this bot, the new
                call is a no-op. Prevents the snapshot-diff vs release
                double-recording bug.
            fee_rate/realized_fees: LEGACY, accepted but ignored. The fleet
                is a signal product — subscribers pay their own exchange's
                fees, so nothing is deducted here. Kept in the signature so
                existing callers don't break.
            risk_per_trade: optional initial risk in USD (for R-multiple calc)
        """
        if bot_id not in self.trades:
            self.trades[bot_id] = []

        # Dedup by trade_id (typically reservation_id) — same trade can hit
        # this method twice when both _handle_release and the snapshot-diff
        # bridge fire. The dedup key has to be inside the bot's trade list
        # so we can find it without a global index.
        if trade_id is not None:
            for existing in self.trades[bot_id]:
                if existing.get('trade_id') == trade_id:
                    return existing  # already recorded — silent no-op

        if realized_pnl is not None:
            # PNL-DRIVEN: use the reported number directly. Most bots
            # land here because pnl is what they actually know.
            gross_pnl = float(realized_pnl)
        else:
            # PRICE-DRIVEN: legacy path. Guard divide-by-zero — if entry
            # is 0/missing, this caller has a bug; record a zero trade
            # instead of silently raising and losing the trade entirely.
            if not entry_price or entry_price == 0:
                gross_pnl = 0.0
            elif direction == 'LONG':
                gross_pnl = (exit_price - entry_price) / entry_price * size_usd
            else:
                gross_pnl = (entry_price - exit_price) / entry_price * size_usd
        # Gross semantics (2026-07-30): no fees are deducted. net_pnl is
        # kept as a field name for record-shape compatibility only and is
        # always equal to gross_pnl for trades recorded after this date.
        total_fees = 0.0
        net_pnl = gross_pnl

        # R-multiple (if risk is known)
        if risk_per_trade and risk_per_trade > 0:
            r_multiple = net_pnl / risk_per_trade
        else:
            r_multiple = None

        trade = {
            'pair': pair,
            'direction': direction,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'size_usd': size_usd,
            'gross_pnl': round(gross_pnl, 4),
            'fees': round(total_fees, 4),
            'net_pnl': round(net_pnl, 4),
            'r_multiple': round(r_multiple, 3) if r_multiple is not None else None,
            'duration': duration,
            'won': net_pnl > 0,
            'timestamp': time.time(),
        }
        if trade_id is not None:
            trade['trade_id'] = trade_id

        self.trades[bot_id].append(trade)
        self.trades[bot_id] = self.trades[bot_id][-self.max_trades_per_bot:]
        self._save()

        return trade

    # ── Per-Bot Statistics ──────────────────────────────────────────

    def get_bot_stats(self, bot_id, last_n=None):
        """
        Complete expectancy statistics for a single bot.
        Returns the numbers Jim Simons actually cares about.
        """
        trades = self.trades.get(bot_id, [])
        if last_n:
            trades = trades[-last_n:]

        if not trades:
            return self._empty_stats(bot_id)

        # Gross semantics (2026-07-30): classify and average on gross_pnl.
        # Pre-2026-07-30 records carry a net-of-fees net_pnl and a `won`
        # flag derived from it — reading gross_pnl here re-reads that
        # history under gross semantics without rewriting the file.
        # A gross_pnl of exactly 0 is a capital movement (a stale-reservation
        # cleanup, a cancelled entry), neither a win nor a loss. The old
        # `<= 0` bucketed flats as LOSSES: live, turtlesue's two $0.00
        # reconcile-closes rendered as "2 losses, 0% win rate,
        # EARLY_NEGATIVE" — a losing verdict from trades that measured
        # nothing. Same defect already fixed in weekly_analysis and
        # fleet_logger; this was the third sibling.
        wins = [t for t in trades if t['gross_pnl'] > 0]
        losses = [t for t in trades if t['gross_pnl'] < 0]
        flats = [t for t in trades if t['gross_pnl'] == 0]

        total = len(trades)
        win_count = len(wins)
        loss_count = len(losses)
        flat_count = len(flats)
        decided = win_count + loss_count
        # Rate over DECIDED trades; None when nothing was decided — 0.0
        # reads as "measured, and it lost every trade".
        win_rate = (win_count / decided) if decided else None

        # Average win/loss (gross price movement)
        avg_win = sum(t['gross_pnl'] for t in wins) / win_count if wins else 0
        avg_loss = sum(t['gross_pnl'] for t in losses) / loss_count if losses else 0

        # EXPECTANCY: THE NUMBER — per decided trade; None when nothing
        # was decided (a flat-only bot has not been measured).
        expectancy = ((win_rate * avg_win) + ((1 - win_rate) * avg_loss)
                      if win_rate is not None else None)

        # Profit factor: gross wins / gross losses. With ZERO gross losses
        # the ratio has no denominator — the old 999 placeholder rendered on
        # the scoreboard as "999.00", a number that reads as a measurement.
        # None means "no losing side measured yet"; wins/losses counts carry
        # the actual information.
        gross_wins = sum(t['gross_pnl'] for t in wins)
        gross_losses = abs(sum(t['gross_pnl'] for t in losses))
        profit_factor = (gross_wins / gross_losses
                         if gross_losses > 0 else None)

        # R-multiples
        r_values = [t['r_multiple'] for t in trades
                    if t['r_multiple'] is not None]
        avg_r = sum(r_values) / len(r_values) if r_values else None

        # Fees: always 0 — signal product, subscribers pay their own venue.
        # Fields kept in the output shape so consumers don't break.
        total_fees = 0.0
        fee_pct = 0.0

        # Best / worst (gross)
        best = max(trades, key=lambda t: t['gross_pnl'])
        worst = min(trades, key=lambda t: t['gross_pnl'])

        # Max consecutive losses
        max_consec_losses = self._max_consecutive_losses(trades)

        # Time analysis
        win_durations = [t['duration'] for t in wins]
        loss_durations = [t['duration'] for t in losses]
        avg_win_time = (sum(win_durations) / len(win_durations)
                       if win_durations else 0)
        avg_loss_time = (sum(loss_durations) / len(loss_durations)
                        if loss_durations else 0)

        # Total P/L — gross only. total_net_pnl is kept as an output key
        # for consumer compatibility but now equals the gross total.
        total_gross_pnl = sum(t['gross_pnl'] for t in trades)
        total_net = total_gross_pnl

        return {
            'bot_id': bot_id,
            'total_trades': total,
            'wins': win_count,
            'losses': loss_count,
            'flat': flat_count,
            'decided': decided,
            'win_rate': (round(win_rate * 100, 1)
                         if win_rate is not None else None),
            'expectancy_per_trade': (round(expectancy, 2)
                                     if expectancy is not None else None),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': (round(profit_factor, 2)
                              if profit_factor is not None else None),
            'avg_r': round(avg_r, 3) if avg_r is not None else None,
            'best_trade': round(best['gross_pnl'], 2),
            'worst_trade': round(worst['gross_pnl'], 2),
            'max_consecutive_losses': max_consec_losses,
            'avg_win_duration_hours': round(avg_win_time / 3600, 1),
            'avg_loss_duration_hours': round(avg_loss_time / 3600, 1),
            'total_net_pnl': round(total_net, 2),
            'total_gross_pnl': round(total_gross_pnl, 2),
            'total_fees': round(total_fees, 2),
            'fees_pct_of_gross': round(fee_pct, 1),
            'is_profitable': (expectancy > 0
                              if expectancy is not None else None),
        }

    # ── Fleet-Wide Statistics ───────────────────────────────────────

    def get_fleet_stats(self, last_n=None):
        """Aggregate expectancy across all bots."""
        all_trades = []
        # Only current fleet members count toward fleet-wide numbers.
        # Retired bots (trekbot -> standalone GoldenEye) keep their trade
        # history on disk but must not skew the aggregate. Empty set means
        # the registry was unavailable — fall back to unfiltered rather than
        # silently reporting zero trades.
        members = _fleet_member_ids()
        self._excluded_bots = (sorted(set(self.trades) - members)
                               if members else [])

        for bot_id, trades in self.trades.items():
            if members and bot_id not in members:
                continue
            if last_n:
                trades = trades[-last_n:]
            all_trades.extend(trades)

        if not all_trades:
            # Same SHAPE as the populated return. This branch used to drop
            # participating_bots / fleet_members / bot_rankings, so a consumer
            # that reads them got a bare figure with no denominator the moment
            # history was empty — the daily card rendered "Avg P/L $+0.00 per
            # trade" from zero trades, asserting a measurement nobody made.
            # A payload whose keys change with its values is a trap.
            _members = _fleet_member_ids()
            return {
                'total_trades': 0,
                'fleet_expectancy': None,   # not 0 — nothing was measured
                'win_rate': None,
                'total_net_pnl': 0,
                'total_gross_pnl': 0,
                'total_fees': 0,
                'bot_rankings': [],
                'participating_bots': 0,
                'fleet_members': len(_members) if _members else None,
                'silent_members': len(_members) if _members else None,
                'coverage_note': 'no closed trades yet',
                'excluded_non_members': [],
                'bot_stats': {},
            }

        # Per-bot stats
        bot_stats = {}
        for bot_id in self.trades:
            if members and bot_id not in members:
                continue
            bot_stats[bot_id] = self.get_bot_stats(bot_id, last_n)

        # Fleet aggregate — gross semantics (2026-07-30): classify and
        # average on gross_pnl, same convention as get_bot_stats.
        # Same flat-is-not-a-loss rule as get_bot_stats (see the note there).
        wins = [t for t in all_trades if t['gross_pnl'] > 0]
        losses = [t for t in all_trades if t['gross_pnl'] < 0]
        total = len(all_trades)
        _fleet_decided = len(wins) + len(losses)
        win_rate = (len(wins) / _fleet_decided) if _fleet_decided else None

        avg_win = (sum(t['gross_pnl'] for t in wins) / len(wins)
                  if wins else 0)
        avg_loss = (sum(t['gross_pnl'] for t in losses) / len(losses)
                   if losses else 0)

        fleet_expectancy = ((win_rate * avg_win + (1 - win_rate) * avg_loss)
                            if win_rate is not None else None)

        # Size-normalized expectancy. The dollar mean weights each trade by
        # its notional — stored sizes span $8k to $47k (5.7x), so one large
        # loser outweighs five small winners, and on 2026-08-07 the two
        # measures DISAGREED IN SIGN: -$47.04/trade in dollars vs +0.23%%
        # per trade on notional. Publishing only dollars presents a
        # size-weighted artefact as the fleet's edge. Only trades with a
        # real positive size can be normalized; n is disclosed.
        _returns = [t['gross_pnl'] / t['size_usd'] for t in all_trades
                    if isinstance(t.get('size_usd'), (int, float))
                    and t['size_usd'] > 0]
        if _returns:
            _mean_r = sum(_returns) / len(_returns)
            _var_r = (sum((r - _mean_r) ** 2 for r in _returns)
                      / len(_returns)) if len(_returns) > 1 else 0.0
            expectancy_pct = round(_mean_r * 100, 4)
            expectancy_pct_sd = round((_var_r ** 0.5) * 100, 4)
        else:
            expectancy_pct = None
            expectancy_pct_sd = None

        total_gross = sum(t['gross_pnl'] for t in all_trades)
        total_net = total_gross  # key kept for consumers; equals gross now
        total_fees = 0.0         # signal product — no fees tracked

        # Rank bots by expectancy. An UNMEASURED bot sorts last rather than
        # being treated as a 0 — it has not underperformed, it has not been
        # measured. The two-key sort keeps measured bots ordered among
        # themselves and cannot raise on a None (which the old
        # `.get(..., 0)` did once expectancy stopped fabricating zeros:
        # TypeError: '<' not supported between NoneType and float).
        def _rank_key(item):
            ev = item[1].get('expectancy_per_trade')
            measured = isinstance(ev, (int, float))
            return (1 if measured else 0, ev if measured else 0.0)

        ranked = sorted(bot_stats.items(), key=_rank_key, reverse=True)

        return {
            'total_trades': total,
            'win_rate': (round(win_rate * 100, 1)
                         if win_rate is not None else None),
            'fleet_decided': _fleet_decided,
            'fleet_expectancy': (round(fleet_expectancy, 2)
                                 if fleet_expectancy is not None else None),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'total_net_pnl': round(total_net, 2),
            'total_gross_pnl': round(total_gross, 2),
            # Fee fields report 0 permanently (signal product) — kept in
            # the shape so dashboard fee panels render zeros, not 500s.
            'total_fees': 0.0,
            'fees_ate_pct': 0.0,
            # A bot with no closed trades has not been judged. Calling it
            # LOSING is a verdict from zero measurements — it read as a
            # failing bot beside bots that had actually traded.
            'bot_rankings': [_rank_row(bid, s) for bid, s in ranked],
            'expectancy_pct': expectancy_pct,
            'expectancy_pct_sd': expectancy_pct_sd,
            'expectancy_pct_n': len(_returns),
            'min_verdict_n': MIN_VERDICT_N,
            'bot_stats': dict(ranked),
            # Named, not silent: a bot dropped from the numbers must be
            # visible in them. Retired members with residual trade history
            # land here (e.g. trekbot after the GoldenEye split).
            'excluded_non_members': getattr(self, '_excluded_bots', []),
            # THE DENOMINATOR. "fleet_expectancy" is named for a population it
            # does not cover: on 2026-08-05 it was 12 trades from 2 of 18
            # members, with 16 having produced no closed trades at all. A
            # reader takes -$35.42/trade as "the fleet is losing money" when it
            # means "two bots have taken twelve trades between them". Same
            # class as the membership bug above — a statistic labelled for a
            # population wider than its sample. Ship the denominator beside
            # the figure so the label cannot outrun the data.
            'participating_bots': len(bot_stats),
            'fleet_members': len(members) if members else None,
            'silent_members': (len(members) - len(bot_stats)
                               if members else None),
            'coverage_note': (
                f"{len(bot_stats)} of {len(members)} members have closed "
                f"trades; {len(members) - len(bot_stats)} are silent"
                if members else "membership registry unavailable"),
        }

    # ── Snapshot for Bot API ────────────────────────────────────────

    def bot_snapshot_fields(self, bot_id):
        """
        Returns the fields Jim says every bot MUST display.
        Designed to be merged into existing bot snapshots.
        """
        stats = self.get_bot_stats(bot_id)
        return {
            'expectancy_per_trade': stats['expectancy_per_trade'],
            'avg_win': stats['avg_win'],
            'avg_loss': stats['avg_loss'],
            'profit_factor': stats['profit_factor'],
            'avg_r': stats['avg_r'],
            'best_trade': stats['best_trade'],
            'worst_trade': stats['worst_trade'],
            'max_consecutive_losses': stats['max_consecutive_losses'],
            'time_in_winning_trades_avg': f"{stats['avg_win_duration_hours']}h",
            'time_in_losing_trades_avg': f"{stats['avg_loss_duration_hours']}h",
            'total_fees': stats['total_fees'],
        }

    # ── Helpers ─────────────────────────────────────────────────────

    def _max_consecutive_losses(self, trades):
        # Gross classification (matches get_bot_stats, not the stored
        # `won` flag, which is net-based for pre-2026-07-30 records).
        max_run = 0
        current_run = 0
        for t in trades:
            if not t['gross_pnl'] > 0:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 0
        return max_run

    def evict_by_pair_prefix(self, prefix: str) -> int:
        """Drop in-memory trades whose pair starts with `prefix`. Returns count.

        Exists so a test that exercises the live release path can remove what
        it created. Without it the disk store could be cleaned while Command
        Center kept serving the phantom from memory — a probe trade appeared
        as a real turtlesue result on /api/expectancy and the BOT SCOREBOARD
        for exactly that reason.

        Deliberately prefix-scoped: it cannot be used to delete real history,
        only rows whose pair carries a test marker no real market uses.
        """
        if not prefix:
            return 0
        removed = 0
        for bot_id, rows in list(self.trades.items()):
            if not isinstance(rows, list):
                continue
            keep = [t for t in rows if not str(t.get("pair", "")).startswith(prefix)]
            removed += len(rows) - len(keep)
            self.trades[bot_id] = keep
        if removed:
            try:
                self._save()
            except Exception:
                pass
        return removed

    def repair_fabricated_prices(self) -> dict:
        """Null stored entry/exit prices of exactly 0, and drop double-counts.

        Three writers used to record `entry_price=data.get(..., 0)` into this
        DURABLE store, so a trade whose bot reported P/L but no prices was
        persisted claiming an entry of $0.00 — a price nobody measured,
        indistinguishable from a real one, and the reason r_multiple is null
        on those rows. All three writers were fixed (8cac8f7, e1763b2,
        f7b0481) but the rows they already wrote remain.

        Separately, the release path keyed its dedup on the reservation id
        while the bus path keyed on the event id, so one close arriving by
        both routes was stored TWICE under different keys. A double-count
        inflates total_trades and halves the true per-trade expectancy.

        This repairs in MEMORY and then saves, because Command Center loads
        this store once at construction and _save() writes the in-memory copy
        — editing the file under a running process is silently overwritten by
        the next trade.

        Deliberately conservative: it only nulls price fields that are exactly
        0 and only drops a row that duplicates another's (pair, gross_pnl)
        while carrying strictly less price information. gross_pnl, size_usd,
        duration, direction, won and timestamps are never touched, so no
        derived statistic changes.
        """
        nulled = 0
        dropped = 0
        details = []
        for bot_id, rows in list(self.trades.items()):
            if not isinstance(rows, list):
                continue
            seen = {}
            keep = []
            for t in rows:
                try:
                    key = (t.get("pair"), round(float(t.get("gross_pnl") or 0), 4))
                except (TypeError, ValueError):
                    keep.append(t)
                    continue
                info = sum(1 for f in ("entry_price", "exit_price")
                           if isinstance(t.get(f), (int, float)) and t.get(f) > 0)
                if key in seen:
                    prev_info, prev_idx = seen[key]
                    if info > prev_info:
                        keep[prev_idx] = t          # keep the richer row
                        seen[key] = (info, prev_idx)
                    dropped += 1
                    details.append("dropped duplicate %s %s gross=%s"
                                   % (bot_id, key[0], key[1]))
                    continue
                seen[key] = (info, len(keep))
                keep.append(t)

            for t in keep:
                for f in ("entry_price", "exit_price"):
                    if t.get(f) == 0:
                        t[f] = None
                        nulled += 1

            # Rows that measure NOTHING: gross_pnl exactly 0 AND no size.
            # These are capital movements (stale-reservation cleanups) that
            # slipped past the writers' guards; they inflate total_trades
            # and, before the flat-is-not-a-loss classifier fix, rendered as
            # losses. A flat close with a REAL size survives — that is a
            # measured break-even trade, which is a different thing.
            _before = len(keep)
            keep = [t for t in keep
                    if not (t.get("gross_pnl") == 0
                            and not t.get("size_usd"))]
            if len(keep) != _before:
                dropped += _before - len(keep)
                details.append("dropped %d unmeasurable zero-P/L zero-size "
                               "row(s) for %s" % (_before - len(keep), bot_id))
            self.trades[bot_id] = keep

        if nulled or dropped:
            try:
                self._save()
            except Exception:
                pass
        return {"nulled": nulled, "dropped": dropped, "details": details}

    def _empty_stats(self, bot_id):
        """Stats for a bot with no closed trades.

        Counts are genuinely 0 — nothing closed, and that IS the measurement.
        Every RATE and RATIO is None, because a rate over zero samples was
        never measured: a 0 there renders as "loses every trade" beside bots
        that have actually traded. The BOT SCOREBOARD read win_rate from here
        in preference to the normalized value, so it printed "0%" for
        TurtleSue even after the normalizer was fixed to send null.

        is_profitable is None for the same reason — False is a verdict, and
        no verdict was reached.
        """
        return {
            'bot_id': bot_id,
            # Real counts: nothing closed.
            'total_trades': 0, 'wins': 0, 'losses': 0,
            'max_consecutive_losses': 0,
            # Never measured — not zero.
            'win_rate': None, 'expectancy_per_trade': None,
            'avg_win': None, 'avg_loss': None, 'profit_factor': None,
            'avg_r': None, 'best_trade': None, 'worst_trade': None,
            'avg_win_duration_hours': None, 'avg_loss_duration_hours': None,
            'total_net_pnl': None, 'total_gross_pnl': None,
            'fees_pct_of_gross': None,
            # Fees are structurally 0 fleet-wide (signal product), not absent.
            'total_fees': 0,
            'is_profitable': None,
        }

    # ── Persistence ─────────────────────────────────────────────────

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.PERSIST_PATH), exist_ok=True)
            tmp = self.PERSIST_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.trades, f)
            os.replace(tmp, self.PERSIST_PATH)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.exists(self.PERSIST_PATH):
                with open(self.PERSIST_PATH) as f:
                    self.trades = json.load(f)
        except Exception:
            pass


if __name__ == "__main__":
    # CLAUDE.md promises `python expectancy.py` shows current fleet state.
    # Loads the persisted trade store (logs/expectancy.json) — the same data
    # the live tracker in command_center.py serves at /api/expectancy.
    tracker = ExpectancyTracker()
    print(json.dumps(tracker.get_fleet_stats(), indent=2, default=str))
