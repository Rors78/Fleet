"""
EXPECTANCY TRACKER -- The only number that matters.

E[V] = (Win Rate x Average Win) - (Loss Rate x Average Loss)

If E[V] > 0, you make money over time. If E[V] < 0, you lose.
Win rate alone is MEANINGLESS without knowing win/loss sizes.

Tracks per-bot and fleet-wide:
- expectancy_per_trade (net of fees)
- avg_win, avg_loss
- profit_factor (gross wins / gross losses)
- avg_r (average R-multiple if risk per trade is known)
- max_consecutive_losses
- time_in_winning_trades vs time_in_losing_trades
- fee burden (total fees paid, fees as % of gross P/L)
- best_trade, worst_trade

Kraken fee schedule (2026-04, tier 0: $0-$10K/month):
- Taker: 0.40% (0.0040)
- Maker: 0.25% (0.0025)
- Round-trip estimate: 0.80% (0.008) for taker/taker

Usage:
    from expectancy import ExpectancyTracker
    tracker = ExpectancyTracker()

    # Record each closed trade:
    tracker.record_trade('trekbot', 'BTC/USD', 'LONG',
                         entry_price=84000, exit_price=84500,
                         size_usd=500, duration=3600,
                         fee_rate=0.0040)

    # Get expectancy:
    stats = tracker.get_bot_stats('trekbot')
    fleet = tracker.get_fleet_stats()
"""

import json
import math
import os
import time


# Kraken fee schedule (2026-04, tier 0: $0-$10K/month)
KRAKEN_TAKER = 0.0040
KRAKEN_MAKER = 0.0025
KRAKEN_ROUNDTRIP_EST = 0.008  # taker both sides


class ExpectancyTracker:
    PERSIST_PATH = os.path.join(os.path.dirname(__file__),
                                'logs', 'expectancy.json')

    def __init__(self):
        self.trades = {}   # {bot_id: [trades]}
        self.max_trades_per_bot = 500
        self._load()

    # ── Trade Recording ─────────────────────────────────────────────

    def record_trade(self, bot_id, pair, direction, entry_price, exit_price,
                     size_usd, duration, fee_rate=KRAKEN_TAKER,
                     risk_per_trade=None, realized_pnl=None,
                     realized_fees=None, trade_id=None):
        """
        Record a completed trade with full cost accounting.

        Two recording modes:

        1. PRICE-DRIVEN (entry_price + exit_price > 0): the legacy path.
           Computes gross_pnl from price movement, applies fee_rate to both
           sides. Use when the bot reports prices but not realized PnL.

        2. PNL-DRIVEN (realized_pnl is not None): bypasses the formula and
           uses the broker-reported PnL directly. Use this for bots that
           report PnL net of fees (most central-portfolio bots). Avoids the
           ZeroDivisionError that silently dropped every trade where the
           bot omitted prices.

        Args:
            entry_price/exit_price: required for PRICE-DRIVEN mode
            realized_pnl: gross PnL (before fees) — triggers PNL-DRIVEN mode
            realized_fees: actual fees paid (defaults to size_usd * fee_rate * 2)
            trade_id: optional dedup key (e.g. reservation_id) — if a trade
                with the same trade_id already exists for this bot, the new
                call is a no-op. Prevents the snapshot-diff vs release
                double-recording bug.
            fee_rate: per-side fee rate (applied twice in PRICE-DRIVEN mode,
                or used as fallback for fees in PNL-DRIVEN mode)
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
            # PNL-DRIVEN: use the broker number directly. Most live bots
            # land here because pnl is what they actually know.
            gross_pnl = float(realized_pnl)
            if realized_fees is not None:
                total_fees = float(realized_fees)
            else:
                # Fallback: estimate fees from size and rate (round-trip)
                total_fees = size_usd * fee_rate * 2
            net_pnl = gross_pnl - total_fees
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
            entry_fee = size_usd * fee_rate
            exit_fee = size_usd * fee_rate
            total_fees = entry_fee + exit_fee
            net_pnl = gross_pnl - total_fees

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

        wins = [t for t in trades if t['won']]
        losses = [t for t in trades if not t['won']]

        total = len(trades)
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / total

        # Average win/loss (net of fees)
        avg_win = sum(t['net_pnl'] for t in wins) / win_count if wins else 0
        avg_loss = sum(t['net_pnl'] for t in losses) / loss_count if losses else 0

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

        # Fee burden
        total_fees = sum(t['fees'] for t in trades)
        total_gross = sum(abs(t['gross_pnl']) for t in trades)
        fee_pct = total_fees / total_gross * 100 if total_gross > 0 else 0

        # Best / worst
        best = max(trades, key=lambda t: t['net_pnl'])
        worst = min(trades, key=lambda t: t['net_pnl'])

        # Max consecutive losses
        max_consec_losses = self._max_consecutive_losses(trades)

        # Time analysis
        win_durations = [t['duration'] for t in wins]
        loss_durations = [t['duration'] for t in losses]
        avg_win_time = (sum(win_durations) / len(win_durations)
                       if win_durations else 0)
        avg_loss_time = (sum(loss_durations) / len(loss_durations)
                        if loss_durations else 0)

        # Total P/L
        total_net = sum(t['net_pnl'] for t in trades)
        total_gross_pnl = sum(t['gross_pnl'] for t in trades)

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
            'best_trade': round(best['net_pnl'], 2),
            'worst_trade': round(worst['net_pnl'], 2),
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
        for bot_id, trades in self.trades.items():
            if last_n:
                trades = trades[-last_n:]
            all_trades.extend(trades)

        if not all_trades:
            return {
                'total_trades': 0,
                'fleet_expectancy': 0,
                'total_net_pnl': 0,
                'total_fees': 0,
                'bot_stats': {},
            }

        # Per-bot stats
        bot_stats = {}
        for bot_id in self.trades:
            bot_stats[bot_id] = self.get_bot_stats(bot_id, last_n)

        # Fleet aggregate
        wins = [t for t in all_trades if t['won']]
        losses = [t for t in all_trades if not t['won']]
        total = len(all_trades)
        win_rate = len(wins) / total if total else 0

        avg_win = (sum(t['net_pnl'] for t in wins) / len(wins)
                  if wins else 0)
        avg_loss = (sum(t['net_pnl'] for t in losses) / len(losses)
                   if losses else 0)

        fleet_expectancy = (win_rate * avg_win + (1 - win_rate) * avg_loss)

        total_net = sum(t['net_pnl'] for t in all_trades)
        total_fees = sum(t['fees'] for t in all_trades)
        total_gross = sum(t['gross_pnl'] for t in all_trades)

        # Rank bots by expectancy
        ranked = sorted(
            bot_stats.items(),
            key=lambda x: x[1].get('expectancy_per_trade', 0),
            reverse=True,
        )

        return {
            'total_trades': total,
            'win_rate': round(win_rate * 100, 1),
            'fleet_expectancy': round(fleet_expectancy, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'total_net_pnl': round(total_net, 2),
            'total_gross_pnl': round(total_gross, 2),
            'total_fees': round(total_fees, 2),
            'fees_ate_pct': round(
                total_fees / abs(total_gross) * 100
                if total_gross else 0, 1
            ),
            'bot_rankings': [
                {'bot': bid, 'expectancy': s['expectancy_per_trade'],
                 'trades': s['total_trades'], 'verdict': (
                     'PROFITABLE' if s['expectancy_per_trade'] > 0
                     else 'LOSING')}
                for bid, s in ranked
            ],
            'bot_stats': dict(ranked),
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
        max_run = 0
        current_run = 0
        for t in trades:
            if not t['won']:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 0
        return max_run

    def _empty_stats(self, bot_id):
        return {
            'bot_id': bot_id,
            'total_trades': 0, 'wins': 0, 'losses': 0,
            'win_rate': 0, 'expectancy_per_trade': 0,
            'avg_win': 0, 'avg_loss': 0, 'profit_factor': 0,
            'avg_r': None, 'best_trade': 0, 'worst_trade': 0,
            'max_consecutive_losses': 0,
            'avg_win_duration_hours': 0, 'avg_loss_duration_hours': 0,
            'total_net_pnl': 0, 'total_gross_pnl': 0,
            'total_fees': 0, 'fees_pct_of_gross': 0,
            'is_profitable': False,
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
