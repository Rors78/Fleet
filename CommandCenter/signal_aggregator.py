"""
JIM'S ENSEMBLE -- One decision from many signals.

Every bot PROPOSES. One engine DECIDES.

Each proposal is: {pair, direction, confidence, source, timestamp}
The aggregator:
1. Collects all proposals within a time window
2. Weights each by the source's HISTORICAL accuracy (not theoretical importance)
3. Computes a single BUY/SELL/HOLD score per pair
4. Sizes the position using the ENSEMBLE's track record
5. Executes through ONE pathway

No more 6 independent traders competing for capital.
One unified intelligence making optimal decisions.

Usage:
    from signal_aggregator import SignalAggregator
    agg = SignalAggregator()

    # Bots submit proposals:
    agg.submit_proposal('trekbot', 'BTC/USD', 'LONG', 0.72, {'signal': 'goldeneye_breakout'})
    agg.submit_proposal('oracle', 'BTC/USD', 'LONG', 0.65, {'signal': 'regime_bull'})
    agg.submit_proposal('sentinel', 'BTC/USD', 'SHORT', 0.40, {'signal': 'forecast_down'})

    # Aggregator decides:
    decision = agg.decide('BTC/USD')
    # → {'action': 'TRADE', 'direction': 'LONG', 'confidence': 0.58, ...}

    # After trade closes, record outcome:
    agg.record_outcome('BTC/USD', 'LONG', won=True, pnl=23.50)
"""

import json
import math
import os
import time


class SignalAggregator:
    PERSIST_PATH = os.path.join(os.path.dirname(__file__), 'aggregator_state.json')

    def __init__(self, window=300, min_confidence=0.30):
        self.window = window              # seconds to collect proposals
        self.min_confidence = min_confidence
        self.proposals = {}               # {pair: [proposals]}
        self.source_accuracy = {}         # {source: {wins, losses, total_pnl}}
        self.decision_log = []            # recent decisions for audit
        self.max_log = 500
        # Snapshot of proposals at decision time, so record_outcome can
        # credit sources even after proposals expire from the live buffer.
        self._decision_snapshots = {}     # {pair: [proposals]}
        self._load_state()

    # ── Proposal Collection ─────────────────────────────────────────

    def submit_proposal(self, source, pair, direction, confidence,
                        metadata=None):
        """
        Any bot submits a trade proposal.
        direction: 'LONG', 'SHORT', or 'NEUTRAL'
        confidence: 0.0 to 1.0
        """
        if pair not in self.proposals:
            self.proposals[pair] = []

        self.proposals[pair].append({
            'source': source,
            'direction': direction,
            'confidence': max(0, min(1, confidence)),
            'timestamp': time.time(),
            'metadata': metadata or {},
        })

        # Evict stale proposals
        cutoff = time.time() - self.window
        self.proposals[pair] = [
            p for p in self.proposals[pair] if p['timestamp'] > cutoff
        ]

    # ── Decision Engine ─────────────────────────────────────────────

    def decide(self, pair):
        """
        Combine all proposals into a single decision.
        Returns: {action, direction, confidence, contributors, ...}
        """
        proposals = self.proposals.get(pair, [])

        # Filter stale
        cutoff = time.time() - self.window
        proposals = [p for p in proposals if p['timestamp'] > cutoff]

        if not proposals:
            return {
                'action': 'HOLD', 'direction': 'NEUTRAL',
                'confidence': 0, 'reason': 'no_proposals',
            }

        long_score = 0.0
        short_score = 0.0
        total_weight = 0.0
        contributors = []

        for p in proposals:
            weight = self._source_weight(p['source'])
            age = time.time() - p['timestamp']
            freshness = math.exp(-age / self.window)  # decay with age

            weighted = p['confidence'] * weight * freshness

            if p['direction'] == 'LONG':
                long_score += weighted
            elif p['direction'] == 'SHORT':
                short_score += weighted

            total_weight += weight * freshness

            contributors.append({
                'source': p['source'],
                'direction': p['direction'],
                'raw_confidence': round(p['confidence'], 3),
                'weight': round(weight, 3),
                'freshness': round(freshness, 3),
                'effective': round(weighted, 4),
            })

        if total_weight <= 0:
            return {
                'action': 'HOLD', 'direction': 'NEUTRAL',
                'confidence': 0, 'reason': 'zero_weight',
            }

        # Normalize
        long_norm = long_score / total_weight
        short_norm = short_score / total_weight
        net_score = long_norm - short_norm
        raw_confidence = abs(net_score)

        # Agreement bonus: unanimous sources get a boost
        directions = [
            p['direction'] for p in proposals
            if p['direction'] != 'NEUTRAL'
        ]
        if directions:
            majority = max(
                directions.count('LONG'),
                directions.count('SHORT'),
            )
            agreement = majority / len(directions)
            # 70% base + 30% agreement bonus
            confidence = raw_confidence * (0.7 + 0.3 * agreement)
        else:
            agreement = 0
            confidence = raw_confidence

        confidence = min(1.0, confidence)

        # Threshold gate
        if confidence < self.min_confidence:
            decision = {
                'action': 'HOLD',
                'direction': 'NEUTRAL',
                'confidence': round(confidence, 4),
                'long_score': round(long_norm, 4),
                'short_score': round(short_norm, 4),
                'agreement': round(agreement, 3),
                'n_proposals': len(proposals),
                'contributors': contributors,
                'reason': f'confidence {confidence:.1%} < threshold {self.min_confidence:.0%}',
            }
        else:
            direction = 'LONG' if net_score > 0 else 'SHORT'
            decision = {
                'action': 'TRADE',
                'direction': direction,
                'confidence': round(confidence, 4),
                'long_score': round(long_norm, 4),
                'short_score': round(short_norm, 4),
                'agreement': round(agreement, 3),
                'n_proposals': len(proposals),
                'contributors': contributors,
            }
            # Snapshot proposals so record_outcome works after they expire
            self._decision_snapshots[pair] = [p.copy() for p in proposals]

        # Log decision
        self.decision_log.append({
            **decision, 'pair': pair, 'timestamp': time.time(),
        })
        self.decision_log = self.decision_log[-self.max_log:]

        return decision

    # ── Outcome Recording ───────────────────────────────────────────

    def record_outcome(self, pair, direction, won, pnl=0):
        """
        After a trade closes, update source accuracy.
        Every source that proposed this direction gets credit/blame.
        Uses the snapshot taken at decision time so proposals that expired
        during the trade's lifetime are still credited.
        """
        proposals = self._decision_snapshots.pop(pair, None)
        if proposals is None:
            # Fallback to live buffer (for trades opened before snapshot feature)
            proposals = self.proposals.get(pair, [])

        for p in proposals:
            source = p['source']
            if source not in self.source_accuracy:
                self.source_accuracy[source] = {
                    'wins': 0, 'losses': 0, 'total_pnl': 0,
                    'trades': 0,
                }

            acc = self.source_accuracy[source]
            aligned = p['direction'] == direction

            if aligned:
                acc['trades'] += 1
                if won:
                    acc['wins'] += 1
                else:
                    acc['losses'] += 1
                acc['total_pnl'] += pnl
            else:
                # Source voted AGAINST this trade — inverse credit
                acc['trades'] += 1
                if won:
                    # Trade won but source voted against — source was wrong
                    acc['losses'] += 1
                    # Don't subtract the full winning PnL — source didn't cause a loss,
                    # it just missed the call. Zero PnL impact for this trade.
                else:
                    # Trade lost and source voted against — source was right
                    acc['wins'] += 1
                    # Partial credit: correctly avoided a loser, but don't
                    # award the full inverse PnL (source wasn't short the trade).
                    acc['total_pnl'] += abs(pnl) * 0.5

        self._save_state()

    # ── Source Weight Calculation ────────────────────────────────────

    def _source_weight(self, source):
        """
        Bayesian weight: (wins + 1) / (wins + losses + 2)
        Laplace smoothing: new sources start at 0.5 (neutral).
        Proven sources rise. Bad sources fall.
        """
        acc = self.source_accuracy.get(source, {})
        wins = acc.get('wins', 0)
        losses = acc.get('losses', 0)
        return (wins + 1) / (wins + losses + 2)

    # ── Reporting ───────────────────────────────────────────────────

    def get_source_rankings(self):
        """Rank all sources by accuracy and P/L contribution."""
        rankings = []
        for source, acc in self.source_accuracy.items():
            trades = acc.get('trades', 0)
            wins = acc.get('wins', 0)
            losses = acc.get('losses', 0)
            total_pnl = acc.get('total_pnl', 0)

            win_rate = wins / max(1, wins + losses)
            weight = self._source_weight(source)
            avg_pnl = total_pnl / max(1, trades)

            rankings.append({
                'source': source,
                'trades': trades,
                'wins': wins,
                'losses': losses,
                'win_rate': round(win_rate * 100, 1),
                'weight': round(weight, 3),
                'total_pnl': round(total_pnl, 2),
                'avg_pnl': round(avg_pnl, 2),
                'verdict': ('TRUSTED' if weight > 0.55 and trades >= 10
                           else 'NEUTRAL' if trades < 10
                           else 'DISTRUSTED' if weight < 0.45
                           else 'AVERAGE'),
            })

        rankings.sort(key=lambda r: r['weight'], reverse=True)
        return rankings

    def get_pending_proposals(self):
        """Return all active (non-expired) proposals."""
        cutoff = time.time() - self.window
        result = {}
        for pair, props in self.proposals.items():
            active = [p for p in props if p['timestamp'] > cutoff]
            if active:
                result[pair] = active
        return result

    def get_recent_decisions(self, n=20):
        """Return recent decisions for audit."""
        return self.decision_log[-n:]

    # ── Persistence ─────────────────────────────────────────────────

    def _save_state(self):
        """Persist source accuracy across restarts."""
        try:
            state = {'source_accuracy': self.source_accuracy}
            tmp = self.PERSIST_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self.PERSIST_PATH)
        except Exception:
            pass

    def _load_state(self):
        """Load persisted source accuracy."""
        try:
            if os.path.exists(self.PERSIST_PATH):
                with open(self.PERSIST_PATH) as f:
                    state = json.load(f)
                self.source_accuracy = state.get('source_accuracy', {})
        except Exception:
            pass
