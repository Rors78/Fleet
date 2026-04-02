"""
JIM'S DECOMPOSITION -- Which signals actually make money?

For each signal source, compute its UNIQUE contribution to portfolio P/L.

Tracks every signal emitted and every trade executed, then attributes value:
- trades_influenced: how many trades this signal contributed to
- accuracy: % of influenced trades that were profitable (net of fees)
- marginal_value: avg P/L when present minus avg P/L when absent
- cost_adjusted_expectancy: expected profit minus fees per trade
- verdict: KEEP, EVALUATE, or CUT

Usage:
    from signal_decomposition import SignalDecomposition
    decomp = SignalDecomposition()

    # Log every signal:
    decomp.log_signal('oracle', 'BTC/USD', 'LONG', 0.72)

    # Log every trade with which signals contributed:
    decomp.log_trade('BTC/USD', 'LONG', gross_pnl=35.20, fees=4.80,
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

    def log_trade(self, pair, direction, gross_pnl, fees, duration,
                  contributing_signals, metadata=None):
        """
        Record a completed trade with which signals contributed.

        contributing_signals: list of source names that influenced this trade
        """
        self.trade_log.append({
            'pair': pair,
            'direction': direction,
            'gross_pnl': gross_pnl,
            'fees': fees,
            'net_pnl': gross_pnl - fees,
            'duration': duration,
            'signals': contributing_signals,
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
                'total_fees': round(sum(t['fees'] for t in self.trade_log), 2),
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

            # P/L analysis
            present_net = sum(t['net_pnl'] for t in present)
            absent_net = sum(t['net_pnl'] for t in absent)
            present_gross = sum(t['gross_pnl'] for t in present)
            present_fees = sum(t['fees'] for t in present)

            avg_present = present_net / present_count if present_count else 0
            avg_absent = absent_net / absent_count if absent_count else 0
            marginal = avg_present - avg_absent

            # Win rates
            present_wins = sum(1 for t in present if t['net_pnl'] > 0)
            present_wr = (present_wins / present_count * 100
                         if present_count else 0)

            # Average win / loss sizes (net of fees)
            wins = [t['net_pnl'] for t in present if t['net_pnl'] > 0]
            losses = [t['net_pnl'] for t in present if t['net_pnl'] <= 0]
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

            # Fee burden
            avg_fees = present_fees / present_count if present_count else 0

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

        total_fees = sum(t['fees'] for t in self.trade_log)

        return {
            'rankings': ranked,
            'total_signals_evaluated': len(sources),
            'profitable_sources': profitable,
            'destructive_sources': destructive,
            'total_trades': len(self.trade_log),
            'total_fees': round(total_fees, 2),
            'recommendation': self._recommend(ranked, total_fees),
        }

    def _recommend(self, ranked, total_fees):
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
        if total_fees > 50:
            recs.append(
                f"TOTAL FEES: ${total_fees:.2f} -- "
                f"consider reducing trade frequency"
            )

        fleet_net = sum(t['net_pnl'] for t in self.trade_log)
        if fleet_net < 0:
            recs.append(
                f"FLEET NET P/L: ${fleet_net:.2f} -- "
                f"system is losing money. Prioritize CUT recommendations."
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
