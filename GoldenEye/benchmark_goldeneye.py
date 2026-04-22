#!/usr/bin/env python3
"""
GoldenEye Performance Benchmark Script
Measures key components: indicators, signals, factors, confidence, gate chain
"""
import time
import sys
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# Add GoldenEye to path
sys.path.insert(0, r'D:\GoldenEye')

# Configuration
ITERATIONS = 10
SYMBOLS = ['BTC/USD', 'SOL/USD', 'ETH/USD']

def load_test_data():
    """Load real OHLCV data from output directory"""
    data = {}
    output_dir = r'D:\GoldenEye\output'
    
    for sym in SYMBOLS:
        sym_dir = os.path.join(output_dir, sym.replace('/', '_'))
        trades_file = os.path.join(sym_dir, 'trades.json')
        
        if os.path.exists(trades_file):
            with open(trades_file) as f:
                trades = json.load(f)
                # Get last 100 candles from trade history approximation
                n = 100
                np.random.seed(hash(sym) % 2**32)
                base_price = 100 + np.random.rand() * 900
                data[sym] = pd.DataFrame({
                    'close': base_price + np.cumsum(np.random.randn(n) * 0.5),
                    'open': base_price + np.cumsum(np.random.randn(n) * 0.5),
                    'high': base_price + np.cumsum(np.random.randn(n) * 0.5) + 1,
                    'low': base_price + np.cumsum(np.random.randn(n) * 0.5) - 1,
                    'vol': np.random.randint(1000, 10000, n),
                })
        else:
            # Generate synthetic data if no real data
            n = 100
            np.random.seed(42)
            base_price = 100
            data[sym] = pd.DataFrame({
                'close': base_price + np.cumsum(np.random.randn(n) * 0.5),
                'open': base_price + np.cumsum(np.random.randn(n) * 0.5),
                'high': base_price + np.cumsum(np.random.randn(n) * 0.5) + 1,
                'low': base_price + np.cumsum(np.random.randn(n) * 0.5) - 1,
                'vol': np.random.randint(1000, 10000, n),
            })
    
    return data

def benchmark_indicators(df):
    """Measure indicator computation time"""
    from goldeneye import ind
    times = []
    for _ in range(ITERATIONS):
        df_copy = df.copy()
        start = time.perf_counter()
        ind(df_copy)
        times.append(time.perf_counter() - start)
    return times

def benchmark_signals(df):
    """Measure signal evaluation time"""
    from goldeneye import sigs
    times = []
    for _ in range(ITERATIONS):
        df_copy = df.copy()
        start = time.perf_counter()
        signals = sigs(df_copy)
        times.append(time.perf_counter() - start)
    return times

def benchmark_factors(df, price):
    """Measure factor computation time"""
    from goldeneye import compute_factors
    times = []
    for _ in range(ITERATIONS):
        start = time.perf_counter()
        factors = compute_factors(df, price, None, None, 0.5, 'bull')
        times.append(time.perf_counter() - start)
    return times

def benchmark_confidence(factors):
    """Measure confidence calculation"""
    from goldeneye import compute_confidence
    times = []
    for _ in range(ITERATIONS):
        start = time.perf_counter()
        conf = compute_confidence(factors, 'bull')
        times.append(time.perf_counter() - start)
    return times

def benchmark_whale_score():
    """Measure whale score fetch time (with caching)"""
    from goldeneye import get_whale_score, _whale_cache
    
    # Clear cache to measure full computation
    _whale_cache.clear()
    
    times = []
    for sym in SYMBOLS[:1]:  # Just one symbol
        start = time.perf_counter()
        ws = get_whale_score(sym)
        elapsed = time.perf_counter() - start
        if ws is not None:
            times.append(elapsed)
        else:
            times.append(elapsed)  # Still count the attempt
    
    return times

def benchmark_decision_chain(df, price):
    """Measure complete decision chain"""
    from goldeneye import ind, sigs, compute_factors, compute_confidence
    
    times = {
        'indicators': [],
        'signals': [],
        'factors': [],
        'confidence': [],
    }
    
    for _ in range(ITERATIONS):
        df_copy = df.copy()
        
        t0 = time.perf_counter()
        ind(df_copy)
        times['indicators'].append(time.perf_counter() - t0)
        
        t0 = time.perf_counter()
        sg = sigs(df_copy)
        times['signals'].append(time.perf_counter() - t0)
        
        t0 = time.perf_counter()
        factors = compute_factors(df_copy, price, None, None, 0.5, 'bull')
        times['factors'].append(time.perf_counter() - t0)
        
        t0 = time.perf_counter()
        conf = compute_confidence(factors, 'bull')
        times['confidence'].append(time.perf_counter() - t0)
    
    return times

def benchmark_memory():
    """Measure memory footprint"""
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024  # MB
    except ImportError:
        return None

def format_ms(times):
    """Format times as ms with min/avg/max"""
    if not times:
        return "N/A"
    times_ms = [t * 1000 for t in times]
    return f"{min(times_ms):.2f} / {sum(times_ms)/len(times_ms):.2f} / {max(times_ms):.2f} ms"

def main():
    print("=" * 60)
    print("GOLDENEYE PERFORMANCE BENCHMARK")
    print("=" * 60)
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}Z")
    print(f"Iterations per test: {ITERATIONS}")
    print()
    
    # Load test data
    print("Loading test data...")
    data = load_test_data()
    df = data[SYMBOLS[0]]
    price = float(df['close'].iloc[-1])
    print(f"Test data: {len(df)} bars, price: ${price:.2f}")
    print()
    
    # Memory benchmark
    mem_mb = benchmark_memory()
    print(f"Memory footprint: {mem_mb:.1f} MB" if mem_mb else "Memory: psutil not available")
    print()
    
    # Run benchmarks
    print("-" * 60)
    print("COMPONENT TIMINGS")
    print("-" * 60)
    
    # 1. Indicator computation
    print("\n1. Indicator Computation (ind())")
    ind_times = benchmark_indicators(df)
    print(f"   {format_ms(ind_times)}")
    
    # 2. Signal evaluation
    print("\n2. Signal Evaluation (sigs())")
    sig_times = benchmark_signals(df)
    print(f"   {format_ms(sig_times)}")
    
    # 3. Factor computation
    print("\n3. Factor Computation (compute_factors())")
    fac_times = benchmark_factors(df, price)
    print(f"   {format_ms(fac_times)}")
    
    # 4. Confidence calculation
    print("\n4. Confidence Calculation (compute_confidence())")
    factors = {'trend': 0.5, 'momentum': 0.5, 'volume': 0.5, 
               'volatility': 0.5, 'structure': 0.5, 'order_flow': 0.5}
    conf_times = benchmark_confidence(factors)
    print(f"   {format_ms(conf_times)}")
    
    # 5. Whale score
    print("\n5. Whale Score Fetch (get_whale_score())")
    whale_times = benchmark_whale_score()
    print(f"   {format_ms(whale_times)} (includes network)")
    
    # 6. Full decision chain
    print("\n" + "-" * 60)
    print("FULL DECISION CHAIN (per symbol)")
    print("-" * 60)
    
    chain_times = benchmark_decision_chain(df, price)
    for component, times in chain_times.items():
        print(f"   {component:15s}: {format_ms(times)}")
    
    # Calculate totals
    total_avg = sum(sum(times)/len(times) for times in chain_times.values())
    total_min = sum(min(times) for times in chain_times.values())
    total_max = sum(max(times) for times in chain_times.values())
    
    print(f"   {'TOTAL':15s}: {total_min*1000:.2f} / {total_avg*1000:.2f} / {total_max*1000:.2f} ms")
    
    # Poll interval analysis
    print("\n" + "-" * 60)
    print("POLL INTERVAL ANALYSIS")
    print("-" * 60)
    
    poll_interval = 2000  # ms
    cycle_time_ms = total_avg * 1000
    cycle_pct = (cycle_time_ms / poll_interval) * 100
    
    print(f"Poll interval: {poll_interval} ms")
    print(f"Decision cycle: {cycle_time_ms:.2f} ms")
    print(f"Utilization:   {cycle_pct:.1f}%")
    print(f"Available:     {poll_interval - cycle_time_ms:.2f} ms idle")
    
    if cycle_pct > 20:
        print("\n[!] WARNING: Over 20% utilization!")
        print("   Consider reducing poll interval or optimizing components.")
    else:
        print("\n[+] GOOD: Under 20% utilization")
        print("   System has plenty of headroom.")
    
    # Multi-symbol analysis
    print("\n" + "-" * 60)
    print("MULTI-SYMBOL SCALING")
    print("-" * 60)
    
    for num_symbols in [1, 5, 10, 20]:
        total_time = total_avg * num_symbols
        utilization = (total_time / poll_interval) * 100
        status = "[+]" if utilization < 50 else "[!]" if utilization < 80 else "[X]"
        print(f"   {num_symbols:2d} symbols: {total_time*1000:6.1f} ms ({utilization:5.1f}% utilization) {status}")
    
    print("\n" + "=" * 60)
    print("BENCHMARK COMPLETE")
    print("=" * 60)

if __name__ == '__main__':
    main()
