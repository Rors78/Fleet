#!/usr/bin/env python3
"""
Factor Calibration Analysis
============================
Reads TrekBot's goldeneye_factors.log and produces a calibration curve:
does higher confidence actually predict higher win rate?

Usage:
    python factor_calibration.py
    python factor_calibration.py --log D:\\TrekBot\\goldeneye_factors.log
"""

import argparse
import json
import os
import sys

DEFAULT_LOG = os.path.join(os.path.dirname(__file__), "..", "TrekBot", "goldeneye_factors.log")
FACTORS = ["trend", "momentum", "volume", "volatility", "structure", "order_flow"]


def load_records(path):
    records = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("confidence") is not None and r.get("pnl") is not None:
                    records.append(r)
            except json.JSONDecodeError:
                continue
    return records


def calibration_table(records, buckets=10):
    """Bucket by confidence decile, compute realized WR and avg PnL per bucket."""
    rows = []
    for i in range(buckets):
        lo = i / buckets
        hi = (i + 1) / buckets
        subset = [r for r in records if lo <= r["confidence"] < hi]
        if i == buckets - 1:  # include 1.0 in last bucket
            subset = [r for r in records if lo <= r["confidence"] <= hi]
        if not subset:
            rows.append({"decile": f"{lo:.1f}-{hi:.1f}", "n": 0, "wr": None, "avg_pnl": None, "avg_r": None})
            continue
        wins = sum(1 for r in subset if r["pnl"] > 0)
        wr = wins / len(subset)
        avg_pnl = sum(r["pnl"] for r in subset) / len(subset)
        avg_r = sum(r.get("r", 0) for r in subset) / len(subset)
        rows.append({
            "decile": f"{lo:.1f}-{hi:.1f}",
            "n": len(subset),
            "wr": wr,
            "avg_pnl": avg_pnl,
            "avg_r": avg_r,
        })
    return rows


def factor_win_loss_comparison(records):
    """Compare average factor scores for wins vs losses."""
    wins = [r for r in records if r["pnl"] > 0]
    losses = [r for r in records if r["pnl"] <= 0]
    if not wins or not losses:
        return {}
    result = {}
    for f in FACTORS + ["confidence"]:
        w_vals = [r[f] for r in wins if r.get(f) is not None]
        l_vals = [r[f] for r in losses if r.get(f) is not None]
        w_avg = sum(w_vals) / len(w_vals) if w_vals else 0
        l_avg = sum(l_vals) / len(l_vals) if l_vals else 0
        result[f] = {"win_avg": w_avg, "loss_avg": l_avg, "delta": w_avg - l_avg}
    return result


def regime_breakdown(records):
    """WR and count by regime."""
    regimes = {}
    for r in records:
        reg = r.get("regime", "unknown")
        if reg not in regimes:
            regimes[reg] = {"n": 0, "wins": 0, "total_pnl": 0}
        regimes[reg]["n"] += 1
        if r["pnl"] > 0:
            regimes[reg]["wins"] += 1
        regimes[reg]["total_pnl"] += r["pnl"]
    return regimes


def signal_performance(records):
    """Per-signal win rate and avg PnL."""
    sigs = {}
    for r in records:
        for s in r.get("sigs", []):
            if s not in sigs:
                sigs[s] = {"n": 0, "wins": 0, "total_pnl": 0}
            sigs[s]["n"] += 1
            if r["pnl"] > 0:
                sigs[s]["wins"] += 1
            sigs[s]["total_pnl"] += r["pnl"]
    return sigs


def main():
    parser = argparse.ArgumentParser(description="Factor calibration analysis")
    parser.add_argument("--log", default=DEFAULT_LOG, help="Path to goldeneye_factors.log")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of table")
    args = parser.parse_args()

    records = load_records(args.log)
    if not records:
        print(f"No records found in {args.log}")
        sys.exit(1)

    total = len(records)
    wins = sum(1 for r in records if r["pnl"] > 0)
    fleet_wr = wins / total

    cal = calibration_table(records)
    factors = factor_win_loss_comparison(records)
    regimes = regime_breakdown(records)
    signals = signal_performance(records)

    if args.json:
        print(json.dumps({
            "total_records": total,
            "overall_wr": round(fleet_wr, 4),
            "calibration": cal,
            "factor_comparison": factors,
            "regime_breakdown": regimes,
            "signal_performance": signals,
        }, indent=2, default=str))
        return

    # Pretty print
    print(f"\n{'='*70}")
    print(f"  FACTOR CALIBRATION — {total} trades, WR {fleet_wr:.1%}")
    print(f"{'='*70}\n")

    print("  CONFIDENCE CALIBRATION CURVE")
    print(f"  {'Decile':<12} {'N':>5} {'WR':>8} {'Avg PnL':>10} {'Avg R':>8}")
    print(f"  {'-'*12} {'-'*5} {'-'*8} {'-'*10} {'-'*8}")
    for row in cal:
        if row["n"] == 0:
            print(f"  {row['decile']:<12} {row['n']:>5}      —          —        —")
        else:
            wr_str = f"{row['wr']:.0%}"
            pnl_str = f"${row['avg_pnl']:+.2f}"
            r_str = f"{row['avg_r']:+.3f}"
            print(f"  {row['decile']:<12} {row['n']:>5} {wr_str:>8} {pnl_str:>10} {r_str:>8}")

    # Monotonicity check
    wr_vals = [row["wr"] for row in cal if row["wr"] is not None]
    is_monotonic = all(wr_vals[i] <= wr_vals[i+1] for i in range(len(wr_vals)-1))
    is_anti = all(wr_vals[i] >= wr_vals[i+1] for i in range(len(wr_vals)-1)) if len(wr_vals) > 1 else False

    print()
    if is_monotonic:
        print("  CALIBRATION: GOOD — higher confidence = higher WR")
    elif is_anti:
        print("  CALIBRATION: ANTI-PREDICTIVE — higher confidence = LOWER WR")
    else:
        print("  CALIBRATION: UNCALIBRATED — no monotonic relationship")

    print(f"\n  FACTOR WIN/LOSS COMPARISON")
    print(f"  {'Factor':<14} {'Win Avg':>8} {'Loss Avg':>9} {'Delta':>8} {'Verdict'}")
    print(f"  {'-'*14} {'-'*8} {'-'*9} {'-'*8} {'-'*12}")
    for f in FACTORS + ["confidence"]:
        d = factors.get(f, {})
        w = d.get("win_avg", 0)
        l = d.get("loss_avg", 0)
        delta = d.get("delta", 0)
        verdict = "GOOD" if delta > 0.01 else ("BAD" if delta < -0.01 else "NEUTRAL")
        print(f"  {f:<14} {w:>8.3f} {l:>9.3f} {delta:>+8.3f} {verdict}")

    print(f"\n  REGIME BREAKDOWN")
    print(f"  {'Regime':<15} {'N':>5} {'WR':>8} {'Total PnL':>12}")
    print(f"  {'-'*15} {'-'*5} {'-'*8} {'-'*12}")
    for reg, data in sorted(regimes.items(), key=lambda x: -x[1]["n"]):
        wr = data["wins"] / data["n"] if data["n"] > 0 else 0
        print(f"  {reg:<15} {data['n']:>5} {wr:>7.0%} ${data['total_pnl']:>+10.2f}")

    print(f"\n  SIGNAL PERFORMANCE (top 10 by trade count)")
    print(f"  {'Sig':<5} {'N':>5} {'WR':>8} {'Total PnL':>12} {'Avg PnL':>10}")
    print(f"  {'-'*5} {'-'*5} {'-'*8} {'-'*12} {'-'*10}")
    sorted_sigs = sorted(signals.items(), key=lambda x: -x[1]["n"])[:10]
    for sig, data in sorted_sigs:
        wr = data["wins"] / data["n"] if data["n"] > 0 else 0
        avg = data["total_pnl"] / data["n"] if data["n"] > 0 else 0
        print(f"  {sig:<5} {data['n']:>5} {wr:>7.0%} ${data['total_pnl']:>+10.2f} ${avg:>+8.2f}")

    # Minimum buy math
    TAKER_FEE = 0.0040  # Kraken tier 0 taker
    RT_FEE = TAKER_FEE * 2  # 0.80% round-trip

    # Compute from actual data
    avg_win_pct = []
    avg_loss_pct = []
    for r in records:
        if r.get("pnl") is not None and r.get("confidence") is not None:
            # R-multiple gives directional magnitude; use it as proxy for move %
            pass

    print(f"\n  MINIMUM BUY MATH (Kraken tier 0: {TAKER_FEE:.2%} taker, {RT_FEE:.2%} round-trip)")
    print(f"  {'-'*65}")
    print(f"  Break-even: every trade must gross > {RT_FEE:.2%} just to cover fees")
    print(f"  2.5x fee hurdle (recommended): gross > {RT_FEE * 2.5:.2%} per trade")
    print()
    print(f"  {'Trade Size':>12} {'RT Fee':>10} {'Break-Even':>12} {'2.5x Hurdle':>12} {'Min Gross $':>12}")
    print(f"  {'-'*12} {'-'*10} {'-'*12} {'-'*12} {'-'*12}")
    for size in [30, 50, 100, 200, 500, 1000]:
        rt_fee = size * RT_FEE
        breakeven = rt_fee
        hurdle = rt_fee * 2.5
        min_move_pct = RT_FEE * 2.5 * 100
        print(f"  ${size:>10,} ${rt_fee:>9.2f} ${breakeven:>11.2f} ${hurdle:>11.2f} ${hurdle:>11.2f}")

    print()
    print(f"  At any size, you need a {RT_FEE * 2.5:.2%} gross move to hit 2.5x fee hurdle.")
    print(f"  At current fleet avg win of ${sum(r['pnl'] for r in records if r['pnl']>0)/max(sum(1 for r in records if r['pnl']>0),1):.2f}:")

    avg_win_size = []
    for r in records:
        if r["pnl"] > 0:
            avg_win_size.append(r["pnl"])
    avg_win = sum(avg_win_size) / len(avg_win_size) if avg_win_size else 0

    # What size makes avg_win > 2.5x fees?
    # avg_win_pct = avg_win / avg_size → need avg_win > size * RT * 2.5
    # → size < avg_win / (RT * 2.5)
    if avg_win > 0:
        max_viable_size = avg_win / (RT_FEE * 2.5)
        print(f"    Avg win ${avg_win:.2f} covers 2.5x fees on trades up to ${max_viable_size:.0f}")
        min_viable_size = 0.01 / RT_FEE  # $0.01 profit minimum
        print(f"    Minimum size for $0.01 net profit after fees: ${min_viable_size:.0f}")
    print()

    # From actual trade data: what % of trades cleared the fee hurdle?
    trades_above_hurdle = 0
    for r in records:
        if r["pnl"] > 0:
            # crude: if pnl > 0 they cleared fees (pnl is already net in most cases)
            trades_above_hurdle += 1
    print(f"  Trades with gross profit > 0: {trades_above_hurdle}/{total} ({trades_above_hurdle/total:.0%})")
    print()


if __name__ == "__main__":
    main()
