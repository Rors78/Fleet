import numpy as np
from typing import Dict, List, Any
from collections import deque
import logging
from datetime import datetime

class Brain:
    """
    Intelligence layer for whale detection.
    
    Maintains rolling memory of last N cycles per coin,
    computes adaptive whale likelihood, and generates
    human-readable explanations for each signal.
    """
    
    def __init__(self, memory_cycles: int = 10, signal_threshold: float = 65.0):
        self.memory_cycles = memory_cycles
        self.signal_threshold = signal_threshold
        self.memory: Dict[str, deque] = {}  # Rolling memory per coin
        self.signal_history: Dict[str, List[float]] = {}  # Track signal reliability
        self.logger = logging.getLogger(__name__)
        
        self.logger.info("Brain initialized with memory_cycles=%d, threshold=%.1f", 
                        memory_cycles, signal_threshold)
    
    def update_memory(self, pair: str, indicators: Dict[str, float], timestamp: datetime = None) -> None:
        """
        Update rolling memory with new indicator data for a coin.
        
        Args:
            pair: Trading pair (e.g., "BTC/USD")
            indicators: Dictionary of indicator values
            timestamp: Optional timestamp for the data point
        """
        if pair not in self.memory:
            self.memory[pair] = deque(maxlen=self.memory_cycles)
            self.signal_history[pair] = []
        
        # Add new data point with timestamp
        data_point = {
            'timestamp': timestamp or datetime.now(),
            'adx': indicators.get('adx', 0),
            # +DI / -DI: direction, where ADX is only strength. Stored as None
            # when absent so get_trend_direction can fall back cleanly rather
            # than mistaking a missing reading for DI parity. (2026-07-29)
            'plus_di': indicators.get('plus_di'),
            'minus_di': indicators.get('minus_di'),
            'chopiness': indicators.get('chopiness', 50),
            'volume_surge': indicators.get('volume_surge', 0),
            'whale_score': indicators.get('whale_score', 0),
            'close': indicators.get('close', 0),
            'volume': indicators.get('volume', 0)
        }
        
        self.memory[pair].append(data_point)
        
        # Track whale score history for reliability scoring
        if data_point['whale_score'] > self.signal_threshold:
            self.signal_history[pair].append(data_point['whale_score'])
        
        self.logger.debug("Updated memory for %s: whale_score=%.2f", pair, data_point['whale_score'])
    
    def get_trend_direction(self, pair: str) -> str:
        """
        Determine trend direction based on recent data.
        
        Args:
            pair: Trading pair
            
        Returns:
            "bullish", "bearish", or "neutral"
        """
        if pair not in self.memory or not self.memory[pair]:
            return "neutral"

        recent = list(self.memory[pair])
        latest = recent[-1]

        # ── Primary: directional indicators ──────────────────────────────
        # +DI vs -DI is the standard directional read, and ADX gates whether
        # there is enough trend strength for that direction to mean anything.
        # This replaces a price-average comparison that could never fire:
        # the old code needed a 1% move between two 3-sample means, but Deep
        # Blue cycles every ~13.7s, so that window is ~42 seconds. Measured
        # against 720 one-minute bars per pair (a LONGER window than it
        # actually uses), a >1% move occurred 0 times out of 2880 samples on
        # BTC, ETH, XRP and LINK. The field was dead by construction and
        # reported "neutral" on 20/20 pairs. (2026-07-29 audit)
        pdi = latest.get('plus_di')
        mdi = latest.get('minus_di')
        adx = latest.get('adx', 0) or 0
        if pdi is not None and mdi is not None and (pdi or mdi):
            # Below ADX 20 there is no trend to have a direction.
            if adx < 20:
                return "neutral"
            spread = pdi - mdi
            # Require a modest edge so noise around DI parity stays neutral.
            if spread > 2.0:
                return "bullish"
            if spread < -2.0:
                return "bearish"
            return "neutral"

        # ── Fallback: price-average comparison, correctly scaled ──────────
        # Only used when DI is unavailable. Needs 6 samples for a real
        # comparison; the old code silently set avg_older = avg_recent below
        # that, which made both tests arithmetically impossible rather than
        # defaulting gracefully.
        if len(recent) < 6:
            return "neutral"
        avg_recent = np.mean([r['close'] for r in recent[-3:]])
        avg_older = np.mean([r['close'] for r in recent[-6:-3]])
        if avg_older <= 0:
            return "neutral"
        change = (avg_recent - avg_older) / avg_older
        # 0.1% over a ~42s window, not 1%. See the measurement above.
        if change > 0.001:
            return "bullish"
        if change < -0.001:
            return "bearish"
        return "neutral"
    
    def calculate_signal_reliability(self, pair: str) -> float:
        """
        Calculate signal reliability score based on historical accuracy.
        
        Args:
            pair: Trading pair
            
        Returns:
            Reliability score (0-100)
        """
        if pair not in self.signal_history or len(self.signal_history[pair]) < 3:
            return 50.0  # Default conservative estimate
        
        history = self.signal_history[pair][-10:]  # Last 10 signals
        
        # Calculate consistency of signals
        mean_score = np.mean(history)
        std_score = np.std(history)
        
        # Higher mean and lower std = more reliable
        reliability = (mean_score * 0.7) + ((20 - min(std_score, 20)) * 1.5)
        
        return min(reliability, 100.0)
    
    def analyze_momentum(self, pair: str) -> Dict[str, Any]:
        """
        Analyze momentum indicators for a coin.
        
        Args:
            pair: Trading pair
            
        Returns:
            Dictionary with momentum analysis
        """
        if pair not in self.memory or len(self.memory[pair]) < 5:
            return {
                'momentum': 'unknown',
                'acceleration': 0,
                'volume_momentum': 0
            }
        
        recent = list(self.memory[pair])
        
        # Calculate momentum (rate of change in whale score)
        if len(recent) >= 3:
            scores = [r['whale_score'] for r in recent]
            acceleration = scores[-1] - scores[0]
            
            # Volume momentum
            volumes = [r['volume'] for r in recent if r['volume'] > 0]
            volume_momentum = 0
            if len(volumes) >= 3:
                recent_vol_avg = np.mean(volumes[-3:])
                older_vol_avg = np.mean(volumes[-6:-3]) if len(volumes) >= 6 else recent_vol_avg
                if older_vol_avg > 0:
                    volume_momentum = (recent_vol_avg - older_vol_avg) / older_vol_avg * 100
            
            # Determine momentum strength
            if acceleration > 10:
                momentum = 'strong_up'
            elif acceleration > 3:
                momentum = 'moderate_up'
            elif acceleration < -10:
                momentum = 'strong_down'
            elif acceleration < -3:
                momentum = 'moderate_down'
            else:
                momentum = 'stable'
            
            return {
                'momentum': momentum,
                'acceleration': acceleration,
                'volume_momentum': volume_momentum
            }
        
        return {
            'momentum': 'unknown',
            'acceleration': 0,
            'volume_momentum': 0
        }
    
    def generate_explanation(self, pair: str, indicators: Dict[str, float]) -> str:
        """
        Generate human-readable explanation for whale detection.
        
        Args:
            pair: Trading pair
            indicators: Current indicator values
            
        Returns:
            Human-readable explanation string
        """
        parts = []

        # ── Trend + strength, stated as ONE consistent clause ─────────────
        # These used to be emitted independently, so a pair could be described
        # as "Price is CONSOLIDATING | VERY STRONG trend (ADX=93)" in the same
        # sentence — 15 of 20 live pairs had ADX >= 25 while being labelled
        # consolidating, because get_trend_direction was structurally stuck on
        # "neutral". That text ships to Telegram. Deriving both from the same
        # ADX reading makes the sentence internally consistent even if the
        # direction is genuinely unclear. (2026-07-29 audit)
        trend = self.get_trend_direction(pair)
        adx = indicators.get('adx', 0)

        if adx >= 50:
            strength = "VERY STRONG"
        elif adx >= 25:
            strength = "STRONG"
        elif adx >= 20:
            strength = "WEAK"
        else:
            strength = None

        if strength is None:
            # No trend strength — direction is meaningless here.
            parts.append("Price is CONSOLIDATING (ADX={:.0f})".format(adx))
        elif trend == "bullish":
            parts.append("Price in UPTREND — {} (ADX={:.0f})".format(strength, adx))
        elif trend == "bearish":
            parts.append("Price in DOWNTREND — {} (ADX={:.0f})".format(strength, adx))
        else:
            # Strength without a clear direction: say exactly that, rather
            # than claiming consolidation while ADX says otherwise.
            parts.append("{} move, direction unclear (ADX={:.0f})".format(strength, adx))
        
        # Chopiness analysis
        chopiness = indicators.get('chopiness', 50)
        if chopiness <= 38.2:
            parts.append("LOW chopiness - TRENDING market")
        elif chopiness <= 61.8:
            parts.append("MODERATE chopiness - mixed signals")
        else:
            parts.append("HIGH chopiness - CHOPPY market")
        
        # Volume analysis
        volume_surge = indicators.get('volume_surge', 0)
        if volume_surge >= 2.5:
            parts.append("EXPLOSIVE volume surge!")
        elif volume_surge >= 1.5:
            parts.append("Significant volume increase")
        elif volume_surge >= 0.5:
            parts.append("Above average volume")
        else:
            parts.append("Normal volume levels")
        
        # Momentum analysis
        momentum = self.analyze_momentum(pair)
        mom = momentum.get('momentum', 'unknown')
        if mom in ['strong_up', 'moderate_up']:
            parts.append("Whale score ACCELERATING upward")
        elif mom in ['strong_down', 'moderate_down']:
            parts.append("Whale score declining")
        
        # Signal reliability
        reliability = self.calculate_signal_reliability(pair)
        if reliability >= 80:
            parts.append("HIGH signal reliability ({:.0f}%)".format(reliability))
        elif reliability >= 60:
            parts.append("MODERATE signal reliability ({:.0f}%)".format(reliability))
        else:
            parts.append("LOW signal reliability - verify with other sources")
        
        # Final whale assessment
        whale_score = indicators.get('whale_score', 0)
        if whale_score >= 80:
            assessment = "EXTREME whale likelihood - ACT NOW"
        elif whale_score >= 65:
            assessment = "HIGH whale likelihood"
        elif whale_score >= 50:
            assessment = "MODERATE whale potential"
        else:
            assessment = "LOW whale probability"
        
        parts.append(assessment)
        
        return " | ".join(parts)
    
    def should_alert(self, pair: str, indicators: Dict[str, float]) -> bool:
        """
        Determine if we should alert on this coin.
        
        Args:
            pair: Trading pair
            indicators: Current indicator values
            
        Returns:
            True if alert should be triggered
        """
        whale_score = indicators.get('whale_score', 0)
        
        # Must exceed threshold
        if whale_score < self.signal_threshold:
            return False
        
        # Check momentum - only alert if gaining momentum
        momentum = self.analyze_momentum(pair)
        if momentum.get('momentum', '').endswith('down'):
            return False
        
        # Check reliability
        reliability = self.calculate_signal_reliability(pair)
        if reliability < 40 and whale_score < 75:
            return False
        
        return True
    
    def get_whale_candidates(self, all_indicators: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        """
        Get list of all whale candidates sorted by score.
        
        Args:
            all_indicators: Dictionary mapping pair to indicator values
            
        Returns:
            List of candidate dictionaries with explanations
        """
        candidates = []
        
        for pair, indicators in all_indicators.items():
            # Note: memory already updated by scan_pair() — don't double-update
            # Check if should alert
            if self.should_alert(pair, indicators):
                explanation = self.generate_explanation(pair, indicators)
                reliability = self.calculate_signal_reliability(pair)
                
                candidates.append({
                    'pair': pair,
                    'whale_score': indicators.get('whale_score', 0),
                    'reliability': reliability,
                    'explanation': explanation,
                    'trend': self.get_trend_direction(pair),
                    'momentum': self.analyze_momentum(pair)
                })
        
        # Sort by whale score
        candidates.sort(key=lambda x: x['whale_score'], reverse=True)
        
        return candidates
    
    def get_summary(self, pair: str) -> Dict[str, Any]:
        """
        Get summary of brain analysis for a coin.
        
        Args:
            pair: Trading pair
            
        Returns:
            Summary dictionary
        """
        if pair not in self.memory:
            return {'status': 'No data available'}
        
        recent = list(self.memory[pair])[-5:] if len(self.memory[pair]) >= 5 else list(self.memory[pair])
        
        if not recent:
            return {'status': 'No recent data'}
        
        return {
            'pair': pair,
            'data_points': len(self.memory[pair]),
            'trend': self.get_trend_direction(pair),
            'reliability': self.calculate_signal_reliability(pair),
            'momentum': self.analyze_momentum(pair),
            'latest_score': recent[-1]['whale_score'],
            'avg_score': np.mean([r['whale_score'] for r in recent])
        }

# Research notes:
# - Whale detection heuristics:
#   1. Strong trend (ADX > 25) indicates institutional interest
#   2. Low chopiness (< 38.2) means directional movement
#   3. Volume surge (> 2 std) often precedes large moves
#   4. Rolling memory helps filter false signals
# - Dynamic thresholds based on historical success rate
# - Conservative estimate when insufficient data

if __name__ == "__main__":
    # Test the brain
    brain = Brain(memory_cycles=10, signal_threshold=65.0)
    
    # Simulate some data
    test_pairs = ["BTC/USD", "ETH/USD", "XRP/USD"]
    
    for i in range(15):
        for pair in test_pairs:
            indicators = {
                'adx': 20 + np.random.random() * 40,
                'chopiness': 30 + np.random.random() * 40,
                'volume_surge': np.random.random() * 3 - 0.5,
                'whale_score': 40 + np.random.random() * 40,
                'close': 1000 + np.random.random() * 100,
                'volume': 1000000 + np.random.random() * 500000
            }
            brain.update_memory(pair, indicators)
    
    # Get candidates
    all_indicators = {}
    for pair in test_pairs:
        all_indicators[pair] = {
            'adx': 35,
            'chopiness': 35,
            'volume_surge': 2.0,
            'whale_score': 72,
            'close': 1050,
            'volume': 1500000
        }
    
    candidates = brain.get_whale_candidates(all_indicators)
    
    print("Whale Candidates:")
    for c in candidates:
        print(f"\n{c['pair']}: Score={c['whale_score']:.1f}, Reliability={c['reliability']:.1f}%")
        print(f"  Explanation: {c['explanation']}")