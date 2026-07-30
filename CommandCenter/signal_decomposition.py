"""
JIM'S DECOMPOSITION -- Which signals actually make money?

For each signal source, compute its UNIQUE contribution to portfolio P/L.

GROSS SEMANTICS (since 2026-07-30): the fleet is a signal product — all
P/L here is GROSS price movement. No fees are calculated or deducted;
subscribers pay whatever their own exchange charges. Trades logged before
2026-07-30 carry a net-of-fees `net_pnl` field; that history is not
rewritten — all analysis reads the stored `gross_pnl` field instead, so
old and new trades are evaluated under the same gross semantics.

Tracks every signal emitted and every trade executed, then attributes value:
- trades_influenced: how many trades this signal contributed to
- accuracy: % of influenced trades that were profitable (gross)
- marginal_value: avg P/L when present minus avg P/L when absent
- expectancy: expected gross profit per trade
- verdict: KEEP, EVALUATE, or CUT

Usage:
    from signal_decomposition import SignalDecomposition
    decomp = SignalDecomposition()

    # Log every signal:
    decomp.log_signal('oracle', 'BTC/USD', 'LONG', 0.72)

    # Log every trade with which signals contributed:
    decomp.log_trade('BTC/USD', 'LONG', gross_pnl=35.20,
                     duration=7200, contributing_signals=['oracle', 'trekbot'])

    # Analyze:
    report = decomp.compute_signal_value()
    print(report['rankings'])
"""

import json
import os
import time


class SignalDecomposition:
    PERSIST_PATH = os.path.join(os.path.dirname(__file__),
                                'logs', 'signal_decomposition.json')

    def __init__(self):
        self.signal_log = []
        self.trade_log = []
        self.max_signals = 5000
        self.max_trades = 2000
        self._load()

    # ── Logging ─────────────────────────────────────────────────────

    def log_signal(self, source, pair, direction, confidence,
                   metadata=None):
        """Record a signal emission."""
        self.signal_log.append({
            'source': source,
            'pair': pair,
            'direction': direction,
            'confidence': confidence,
            'timestamp': time.time(),
            'metadata': metadata or {},
        })
        self.signal_log = self.signal_log[-self.max_signals:]

    def log_trade(self, pair, direction, gross_pnl, fees=0.0, duration=0,
                  contributing_signals=None, metadata=None):
        """
        Record a completed trade with which signals contributed.

        contributing_signals: list of source names that influenced this trade
        fees: LEGACY, accepted but ignored (signal product, 2026-07-30 —
            P/L is gross; subscribers pay their own exchange's fees). Kept
            in the signature so old callers don't break.
        """
        self.trade_log.append({
            'pair': pair,
            'direction': direction,
            'gross_pnl': gross_pnl,
            'fees': 0.0,               # always 0 — record shape kept for readers
            'net_pnl': gross_pnl,      # equals gross since 2026-07-30
            'duration': duration,
            'signals': contributing_signals or [],
            'timestamp': time.time(),
            'metadata': metadata or {},
        })
        self.trade_log = self.trade_log[-self.max_trades:]
        self._save()

    # ── Core Analysis ───────────────────────────────────────────────

    def compute_signal_value(self):
        """
        For each signal source, compute marginal value.

        Marginal value = average P/L of trades WHERE this signal was active
                       - average P/L of trades WHERE this signal was NOT active

        Positive marginal value = this signal HELPS.
        Negative marginal value = this signal HURTS.
        """
        # Collect all sources
        sources = set()
        for trade in self.trade_log:
            sources.update(trade.get('signals', []))

        if not sources:
            return {
                'rankings': {},
                'total_signals_evaluated': 0,
                'profitable_sources': 0,
                'destructive_sources': 0,
                'total_trades': len(self.trade_log),
                'total_fees': 0.0,  # signal product — fees not tracked; key kept for shape
                'recommendation': ['No signal data yet. Log trades with contributing_signals.'],
            }

        results = {}
        for source in sources:
            present = [t for t in self.trade_log
                       if source in t.get('signals', [])]
            absent = [t for t in self.trade_log
                      if source not in t.get('signals', [])]

            present_count = len(present)
            absent_count = len(absent)

            # P/L analysis — gross semantics (2026-07-30): read gross_pnl
            # for every trade so pre-conversion (net-recorded) history is
            # evaluated under the same gross convention as new trades.
            present_net = sum(t['gross_pnl'] for t in present)
            absent_net = sum(t['gross_pnl'] for t in absent)

            avg_present = present_net / present_count if present_count else 0
            avg_absent = absent_net / absent_count if absent_count else 0
            marginal = avg_present - avg_absent

            # Win rates (gross)
            present_wins = sum(1 for t in present if t['gross_pnl'] > 0)
            present_wr = (present_wins / present_count * 100
                         if present_count else 0)

            # Average win / loss sizes (gross price movement)
            wins = [t['gross_pnl'] for t in present if t['gross_pnl'] > 0]
            losses = [t['gross_pnl'] for t in present if t['gross_pnl'] <= 0]
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = sum(losses) / len(losses) if losses else 0

            # Profit factor
            gross_wins = sum(t['gross_pnl'] for t in present
                           if t['gross_pnl'] > 0)
            gross_losses = abs(sum(t['gross_pnl'] for t in present
                                  if t['gross_pnl'] <= 0))
            profit_factor = (gross_wins / gross_losses
                            if gross_losses > 0 else
                            float('inf') if gross_wins > 0 else 0)

            # Duration
            avg_duration = (sum(t['duration'] for t in present) / present_count
                          if present_count else 0)

            # Average confidence from signal log
            source_sigs = [s for s in self.signal_log
                          if s['source'] == source]
            avg_conf = (sum(s['confidence'] for s in source_sigs)
                       / len(source_sigs) if source_sigs else 0)

            # Fees: always 0 — signal product; key kept for output shape.
            avg_fees = 0.0

            # Expectancy (the number Jim cares about)
            if present_count > 0:
                expectancy = present_net / present_count
            else:
                expectancy = 0

            # Verdict
            if present_count < 10:
                verdict = 'EVALUATE'
            elif marginal > 0 and expectancy > 0:
                verdict = 'KEEP'
            elif marginal < 0:
                verdict = 'CUT'
            else:
                verdict = 'EVALUATE'

            results[source] = {
                'trades_influenced': present_count,
                'win_rate': round(present_wr, 1),
                'avg_win': round(avg_win, 2),
                'avg_loss': round(avg_loss, 2),
                'profit_factor': round(min(profit_factor, 99), 2),
                'total_net_pnl': round(present_net, 2),
                'avg_pnl_per_trade': round(avg_present, 2),
                'marginal_value': round(marginal, 2),
                'avg_confidence': round(avg_conf, 3),
                'avg_duration_hours': round(avg_duration / 3600, 1),
                'avg_fees_per_trade': round(avg_fees, 2),
                'expectancy': round(expectancy, 2),
                'verdict': verdict,
            }

        # Sort by marginal value descending
        ranked = dict(sorted(results.items(),
                            key=lambda x: x[1]['marginal_value'],
                            reverse=True))

        profitable = sum(1 for d in results.values()
                        if d['marginal_value'] > 0
                        and d['trades_influenced'] >= 10)
        destructive = sum(1 for d in results.values()
                         if d['marginal_value'] < 0
                         and d['trades_influenced'] >= 10)

        return {
            'rankings': ranked,
            'total_signals_evaluated': len(sources),
            'profitable_sources': profitable,
            'destructive_sources': destructive,
            'total_trades': len(self.trade_log),
            # Fees permanently 0 — signal product; key kept so consumers
            # (dashboard fee panels) render zeros instead of erroring.
            'total_fees': 0.0,
            'recommendation': self._recommend(ranked),
        }

    def _recommend(self, ranked):
        recs = []

        cut = [s for s, d in ranked.items()
               if d['verdict'] == 'CUT']
        keep = [s for s, d in ranked.items()
                if d['verdict'] == 'KEEP']

        if cut:
            recs.append(
                f"CUT these sources (negative marginal value): "
                f"{', '.join(cut)}"
            )
        if keep:
            recs.append(
                f"INCREASE weight of top performers: "
                f"{', '.join(keep[:3])}"
            )

        # Gross P/L (2026-07-30): read gross_pnl so legacy net-recorded
        # trades are judged under the same convention as new ones.
        fleet_net = sum(t['gross_pnl'] for t in self.trade_log)
        if fleet_net < 0:
            recs.append(
                f"FLEET GROSS P/L: ${fleet_net:.2f} -- "
                f"signals are losing money. Prioritize CUT recommendations."
            )

        if not recs:
            recs.append("Insufficient data. Need 10+ trades per source.")

        return recs

    # ── Persistence ─────────────────────────────────────────────────

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.PERSIST_PATH), exist_ok=True)
            state = {
                'signal_log': self.signal_log[-self.max_signals:],
                'trade_log': self.trade_log,
            }
            tmp = self.PERSIST_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f)
            os.replace(tmp, self.PERSIST_PATH)
        except Exception:
            pass

    def _load(self):
        try:
            if os.path.exists(self.PERSIST_PATH):
                with open(self.PERSIST_PATH) as f:
                    state = json.load(f)
                self.signal_log = state.get('signal_log', [])
                self.trade_log = state.get('trade_log', [])
        except Exception:
            pass
