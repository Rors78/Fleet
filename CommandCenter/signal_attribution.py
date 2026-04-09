"""
SIGNAL ATTRIBUTION — Fee-Adjusted Signal Value

Primary data source: goldeneye_factors.log (pre-fee R-multiples + signal lists).
Secondary enrichment: logs/expectancy.json (precise post-fee net PnL).

For factor records that have no expectancy match (because the expectancy
tracker was not running for some TrekBot sessions), fees are estimated from
the Kraken taker fee schedule (0.40% per side, 0.80% round-trip, tier 0).

Problem this solves: Ultron ranks signals by pre-fee R-multiple.
A signal with R=+0.10 looks marginal but may be negative after fees.
A signal Ultron wants to cut may actually be the only fee-positive one.

Usage:
    python signal_attribution.py
    python signal_attribution.py --min-trades 3
    python signal_attribution.py --bot trekbot
"""

import json
import os
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timezone

TREKBOT_DIR = os.environ.get("TREKBOT_DIR", r"D:\TrekBot")
FACTOR_LOG = os.path.join(TREKBOT_DIR, "goldeneye_factors.log")
EXPECTANCY_LOG = os.path.join(os.path.dirname(__file__), "logs", "expectancy.json")
MATCH_WINDOW_SEC = 120

# Kraken taker fee (per side, tier 0: $0-$10K/month, 2026-04).
KRAKEN_TAKER = 0.0040
KRAKEN_ROUNDTRIP = KRAKEN_TAKER * 2  # 0.80%


def _parse_args():
    p = argparse.ArgumentParser(description="Fee-adjusted signal attribution")
    p.add_argument("--min-trades", type=int, default=5,
                   help="Minimum appearances to include signal in report (default: 5)")
    p.add_argument("--bot", default="trekbot", help="Bot to analyze (default: trekbot)")
    p.add_argument("--since", default=None,
                   help="Only analyze trades after this date (YYYY-MM-DD). "
                        "Use to restrict to the period where expectancy tracker was running.")
    return p.parse_args()


def _to_ts(val):
    if isinstance(val, (int, float)):
        return val / 1000.0 if val > 1e12 else float(val)
    if isinstance(val, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
    return None


def _normalize_pair(p):
    if p is None:
        return ""
    return p.replace("/", "").replace("-", "").upper()


def _calibrate_default_size(expectancy_records):
    """Compute mean trade size from available expectancy records for fee estimation."""
    trek = [r for r in expectancy_records if r.get("_bot") == "trekbot" and r.get("size_usd")]
    if trek:
        sizes = [r["size_usd"] for r in trek if r["size_usd"] > 0]
        if sizes:
            return sum(sizes) / len(sizes)
    return 28.60  # Calibrated from 46 observed TrekBot trades (mean size)


_DEFAULT_SIZE = 28.60


def _estimate_fees_from_factor(f, default_size=None):
    """Estimate round-trip fees from factor log fields when expectancy record is missing.

    Uses R-multiple and PnL to back out approximate trade size, then applies
    Kraken taker round-trip rate. Falls back to calibrated mean size from
    matched expectancy records (observed mean ~$28.60, median ~$12.51).
    """
    if default_size is None:
        default_size = _DEFAULT_SIZE
    pnl = float(f.get("pnl", 0) or 0)
    r = float(f.get("r", 0) or 0)
    estimated_size = default_size
    if abs(r) > 0.01:
        risk = abs(pnl / r)
        size_from_r = risk / 0.02
        if 5 <= size_from_r <= 500:
            estimated_size = size_from_r
    fees = estimated_size * KRAKEN_ROUNDTRIP
    return round(fees, 4)


def load_factors():
    records = []
    if not os.path.exists(FACTOR_LOG):
        print(f"Factor log not found: {FACTOR_LOG}")
        return records
    with open(FACTOR_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("pnl") is not None and r.get("sigs") is not None:
                    records.append(r)
            except Exception:
                pass
    # Deduplicate near-identical records (same sym, timestamp within 3s, same pnl)
    seen = set()
    unique = []
    for r in records:
        key = (r.get("sym"), round(r.get("t", 0), 0), round(r.get("pnl", 0), 2))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def load_expectancy(bot_filter="trekbot"):
    """Load expectancy records, filtered to a single bot.

    The factor log is TrekBot-only, so we must join only against TrekBot's
    expectancy records to avoid cross-bot pair/timestamp collisions as more
    bots accumulate data.
    """
    if not os.path.exists(EXPECTANCY_LOG):
        print(f"Expectancy log not found: {EXPECTANCY_LOG}")
        return []
    with open(EXPECTANCY_LOG) as f:
        try:
            data = json.load(f)
        except Exception:
            return []
    records = []
    if isinstance(data, dict):
        for bot, trades in data.items():
            if bot_filter and bot != bot_filter:
                continue
            if isinstance(trades, list):
                for t in trades:
                    t["_bot"] = bot
                    records.append(t)
    elif isinstance(data, list):
        records = [r for r in data if not bot_filter or r.get("_bot") == bot_filter]
    return records


def join(factors, expectancy):
    """Join factor records with expectancy data. Factor log is primary source.

    For matched records: use precise fee data from expectancy tracker.
    For unmatched records: estimate fees from Kraken fee schedule.
    Returns all factor records, not just those with expectancy matches.

    Returns: (joined, matched_count, estimated_count, overlap_stats)
        overlap_stats = {"overlap_total": N, "overlap_matched": M} — match rate
        computed only over the period where expectancy data exists, which is
        the meaningful denominator (factor records before the expectancy tracker
        was installed cannot possibly match).
    """
    exp_index = defaultdict(list)
    all_exp_ts = []
    for r in expectancy:
        ts = _to_ts(r.get("timestamp") or r.get("ts") or r.get("entry_time"))
        pair = _normalize_pair(r.get("pair") or r.get("symbol"))
        if ts and pair:
            exp_index[pair].append((ts, r))
            all_exp_ts.append(ts)

    for pair in exp_index:
        exp_index[pair].sort(key=lambda x: x[0])

    # Determine overlap period: earliest expectancy timestamp onward
    overlap_start = min(all_exp_ts) if all_exp_ts else float("inf")

    # Calibrate default size from available expectancy data
    cal_size = _calibrate_default_size(expectancy)

    used_exp = set()
    joined = []
    matched_count = 0
    estimated_count = 0
    overlap_total = 0
    overlap_matched = 0

    for f in factors:
        ts_f = _to_ts(f.get("t") or f.get("timestamp"))
        pair_f = _normalize_pair(f.get("sym") or f.get("pair"))
        if not ts_f or not pair_f:
            continue

        in_overlap = ts_f >= overlap_start

        best, best_delta = None, MATCH_WINDOW_SEC + 1
        for idx, (ts_e, rec) in enumerate(exp_index.get(pair_f, [])):
            if id(rec) in used_exp:
                continue
            d = abs(ts_e - ts_f)
            if d < best_delta:
                best_delta, best = d, rec

        if best and best_delta <= MATCH_WINDOW_SEC:
            used_exp.add(id(best))
            gross_pnl = float(best.get("gross_pnl", 0) or 0)
            fees = float(best.get("fees", 0) or 0)
            net_pnl = float(best.get("net_pnl", 0) or 0)
            matched_count += 1
            source = "matched"
            if in_overlap:
                overlap_matched += 1
        else:
            gross_pnl = float(f.get("pnl", 0) or 0)
            fees = _estimate_fees_from_factor(f, default_size=cal_size)
            net_pnl = gross_pnl - fees
            estimated_count += 1
            source = "estimated"

        if in_overlap:
            overlap_total += 1

        joined.append({
            "sigs": f.get("sigs", []),
            "regime": str(f.get("regime", "unknown")).lower(),
            "pair": pair_f,
            "r_gross": float(f.get("r", 0) or 0),
            "gross_pnl": gross_pnl,
            "fees": fees,
            "net_pnl": net_pnl,
            "won_gross": float(f.get("pnl", 0) or 0) > 0,
            "won_net": net_pnl > 0,
            "_source": source,
        })

    overlap_stats = {
        "overlap_start": overlap_start,
        "overlap_total": overlap_total,
        "overlap_matched": overlap_matched,
    }
    return joined, matched_count, estimated_count, overlap_stats


def compute_signal_stats(joined, min_trades):
    """
    For each signal source, compute:
    - Pre-fee: win rate, avg R, expectancy from factor log
    - Post-fee: win rate, expectancy from expectancy log
    - Fee drag: difference between gross and net expectancy
    - Verdict: KEEP / MONITOR / CUT
    """
    buckets = defaultdict(lambda: {
        "trades": 0, "wins_gross": 0, "wins_net": 0,
        "sum_r": 0.0, "sum_gross": 0.0, "sum_net": 0.0, "sum_fees": 0.0,
    })

    for r in joined:
        for sig in r.get("sigs", []):
            b = buckets[sig]
            b["trades"] += 1
            b["wins_gross"] += int(r["won_gross"])
            b["wins_net"] += int(r["won_net"])
            b["sum_r"] += r["r_gross"]
            b["sum_gross"] += r["gross_pnl"]
            b["sum_net"] += r["net_pnl"]
            b["sum_fees"] += r["fees"]

    rows = []
    for sig, b in buckets.items():
        n = b["trades"]
        if n < min_trades:
            continue
        wr_gross = b["wins_gross"] / n * 100
        wr_net = b["wins_net"] / n * 100
        exp_gross = b["sum_gross"] / n
        exp_net = b["sum_net"] / n
        avg_r = b["sum_r"] / n
        fee_drag = b["sum_fees"] / n
        fee_ratio = (b["sum_fees"] / abs(b["sum_gross"]) * 100) if b["sum_gross"] != 0 else float("inf")

        if exp_net > 0.05:
            verdict = "KEEP"
        elif exp_net > -0.10:
            verdict = "MONITOR"
        else:
            verdict = "CUT"

        # Flag signals where pre-fee looked good but post-fee is bad
        misleading = exp_gross > 0 and exp_net < 0

        rows.append({
            "sig": sig, "trades": n,
            "wr_gross": wr_gross, "wr_net": wr_net,
            "exp_gross": exp_gross, "exp_net": exp_net,
            "avg_r": avg_r, "fee_drag": fee_drag,
            "fee_ratio": fee_ratio, "verdict": verdict,
            "misleading": misleading,
            "total_net": b["sum_net"],
        })

    # Sort: worst net expectancy first
    rows.sort(key=lambda x: x["exp_net"])
    return rows


def main():
    args = _parse_args()

    factors = load_factors()
    expectancy = load_expectancy(bot_filter=args.bot)

    # Apply --since filter
    if args.since:
        try:
            since_ts = datetime.strptime(args.since, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            print(f"Invalid --since date: {args.since} (expected YYYY-MM-DD)")
            sys.exit(1)
        factors = [f for f in factors if (f.get("t") or 0) >= since_ts]

    if not factors:
        print("Missing data. Ensure TrekBot has logged trades with signal attribution.")
        sys.exit(1)

    print(f"Loaded {len(factors)} factor records (deduplicated), {len(expectancy)} {args.bot} expectancy records")
    joined, matched, estimated, overlap = join(factors, expectancy)
    total = matched + estimated
    naive_pct = matched / total * 100 if total else 0

    # Overlap-period match rate: the meaningful metric
    ov_total = overlap["overlap_total"]
    ov_matched = overlap["overlap_matched"]
    ov_pct = ov_matched / ov_total * 100 if ov_total else 0
    pre_overlap = total - ov_total

    print(f"Joined: {total} total ({matched} matched with fee data, {estimated} with estimated fees)")
    if pre_overlap > 0:
        ov_start_str = datetime.fromtimestamp(
            overlap["overlap_start"], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  {pre_overlap} factor records predate expectancy tracker (before {ov_start_str})")
        print(f"  Overlap period: {ov_matched}/{ov_total} matched = {ov_pct:.1f}% match rate")
    else:
        print(f"Match rate: {naive_pct:.1f}%")
    print()

    if not joined:
        print("No records to analyze. Check that goldeneye_factors.log has closed trades.")
        sys.exit(1)

    rows = compute_signal_stats(joined, args.min_trades)
    if not rows:
        print(f"No signals with {args.min_trades}+ appearances found.")
        sys.exit(0)

    print(f"=== SIGNAL ATTRIBUTION — FEE-ADJUSTED (min {args.min_trades} trades) ===\n")
    print(f"{'Sig':<6} {'N':>5} {'WR%(pre)':>9} {'WR%(net)':>9} "
          f"{'Exp(pre)':>9} {'Exp(net)':>9} {'Fee/Trd':>8} {'FeeRatio%':>10} {'Verdict':<9} {'!'}")
    print("-" * 90)
    for r in rows:
        flag = "MISLEADING" if r["misleading"] else ""
        fr = "inf" if r["fee_ratio"] == float("inf") else f"{r['fee_ratio']:.0f}%"
        print(
            f"{r['sig']:<6} {r['trades']:>5} {r['wr_gross']:>8.1f}% {r['wr_net']:>8.1f}% "
            f"{r['exp_gross']:>+9.3f} {r['exp_net']:>+9.3f} "
            f"${r['fee_drag']:>7.3f} {fr:>10} {r['verdict']:<9} {flag}"
        )

    cuts = [r for r in rows if r["verdict"] == "CUT"]
    keeps = [r for r in rows if r["verdict"] == "KEEP"]
    misleading = [r for r in rows if r["misleading"]]

    print(f"\n=== SUMMARY ===")
    print(f"KEEP:    {len(keeps)} signals   ({', '.join(r['sig'] for r in keeps) or 'none'})")
    print(f"MONITOR: {len([r for r in rows if r['verdict']=='MONITOR'])} signals")
    print(f"CUT:     {len(cuts)} signals   ({', '.join(r['sig'] for r in cuts) or 'none'})")
    if misleading:
        print(f"\nMISLEADING (positive pre-fee, negative post-fee): "
              f"{', '.join(r['sig'] for r in misleading)}")
        print("  These signals look profitable in the factor log but cost money after fees.")
        print("  Ultron's pre-fee analysis would tell you to keep them. Don't.")

    # Total fleet cost of CUT signals
    if cuts:
        cut_cost = sum(r["total_net"] for r in cuts)
        print(f"\nCumulative P/L from CUT signals: ${cut_cost:+.2f}")
        print(f"Removing them would have saved/cost that amount over the measurement period.")


if __name__ == "__main__":
    main()
