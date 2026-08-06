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
        wins = [t for t in trades if t['gross_pnl'] > 0]
        losses = [t for t in trades if t['gross_pnl'] <= 0]

        total = len(trades)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total

        # Average win/loss (gross price movement)
        avg_win = sum(t['gross_pnl'] for t in wins) / win_count if wins else 0
        avg_loss = sum(t['gross_pnl'] for t in losses) / loss_count if losses else 0

        # EXPECTANCY: THE NUMBER
        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

        # Profit factor
        gross_wins = sum(t['gross_pnl'] for t in wins)
        gross_losses = abs(sum(t['gross_pnl'] for t in losses))
        profit_factor = (gross_wins / gross_losses
                        if gross_losses > 0 else
                        float('inf') if gross_wins > 0 else 0)

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
            'win_rate': round(win_rate * 100, 1),
            'expectancy_per_trade': round(expectancy, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'profit_factor': round(min(profit_factor, 999), 2),
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
            'is_profitable': expectancy > 0,
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
        wins = [t for t in all_trades if t['gross_pnl'] > 0]
        losses = [t for t in all_trades if t['gross_pnl'] <= 0]
        total = len(all_trades)
        win_rate = len(wins) / total if total else 0

        avg_win = (sum(t['gross_pnl'] for t in wins) / len(wins)
                  if wins else 0)
        avg_loss = (sum(t['gross_pnl'] for t in losses) / len(losses)
                   if losses else 0)

        fleet_expectancy = (win_rate * avg_win + (1 - win_rate) * avg_loss)

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
            'win_rate': round(win_rate * 100, 1),
            'fleet_expectancy': round(fleet_expectancy, 2),
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
            'bot_rankings': [
                {'bot': bid,
                 'expectancy': (s['expectancy_per_trade']
                                if s['total_trades'] else None),
                 'trades': s['total_trades'],
                 'verdict': ('UNMEASURED' if not s['total_trades']
                             else 'PROFITABLE' if s['expectancy_per_trade'] > 0
                             else 'LOSING')}
                for bid, s in ranked
            ],
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
