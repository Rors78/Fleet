"""
SIGNAL ATTRIBUTION — Gross Signal Value (LEGACY)

LEGACY MODULE: depends on goldeneye_factors.log from TrekBot, which left
the fleet — the log no longer updates. Kept for historical analysis only.

GROSS SEMANTICS (since 2026-07-30): the fleet is a signal product — all
P/L here is GROSS price movement. No fees are calculated, estimated, or
deducted; subscribers pay whatever their own exchange charges.
logs/expectancy.json records written before 2026-07-30 carry net-of-fees
`net_pnl` values; this module reads only their `gross_pnl` field.

Primary data source: goldeneye_factors.log (R-multiples + signal lists).
Secondary enrichment: logs/expectancy.json (precise recorded PnL).

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


def _parse_args():
    p = argparse.ArgumentParser(description="Gross signal attribution (legacy)")
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

    For matched records: use the expectancy tracker's recorded gross_pnl.
    For unmatched records: use the factor log's own pnl.
    All values are GROSS price movement — no fees (signal product, 2026-07-30).
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

        # Gross semantics (2026-07-30): matched records contribute their
        # recorded gross_pnl (NOT the legacy net_pnl, which had fees
        # subtracted); unmatched records use the factor log's pnl as-is.
        if best and best_delta <= MATCH_WINDOW_SEC:
            used_exp.add(id(best))
            gross_pnl = float(best.get("gross_pnl", 0) or 0)
            matched_count += 1
            source = "matched"
            if in_overlap:
                overlap_matched += 1
        else:
            gross_pnl = float(f.get("pnl", 0) or 0)
            estimated_count += 1
            source = "factor_log"

        if in_overlap:
            overlap_total += 1

        joined.append({
            "sigs": f.get("sigs", []),
            "regime": str(f.get("regime", "unknown")).lower(),
            "pair": pair_f,
            "r_gross": float(f.get("r", 0) or 0),
            "gross_pnl": gross_pnl,
            "won": gross_pnl > 0,
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
    For each signal source, compute gross win rate, avg R, and gross
    expectancy, plus a KEEP / MONITOR / CUT verdict.
    (Gross semantics since 2026-07-30 — no fee columns.)
    """
    buckets = defaultdict(lambda: {
        "trades": 0, "wins": 0,
        "sum_r": 0.0, "sum_gross": 0.0,
    })

    for r in joined:
        for sig in r.get("sigs", []):
            b = buckets[sig]
            b["trades"] += 1
            b["wins"] += int(r["won"])
            b["sum_r"] += r["r_gross"]
            b["sum_gross"] += r["gross_pnl"]

    rows = []
    for sig, b in buckets.items():
        n = b["trades"]
        if n < min_trades:
            continue
        wr = b["wins"] / n * 100
        exp_gross = b["sum_gross"] / n
        avg_r = b["sum_r"] / n

        if exp_gross > 0.05:
            verdict = "KEEP"
        elif exp_gross > -0.10:
            verdict = "MONITOR"
        else:
            verdict = "CUT"

        rows.append({
            "sig": sig, "trades": n,
            "wr": wr, "exp_gross": exp_gross,
            "avg_r": avg_r, "verdict": verdict,
            "total_gross": b["sum_gross"],
        })

    # Sort: worst gross expectancy first
    rows.sort(key=lambda x: x["exp_gross"])
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

    print(f"Joined: {total} total ({matched} matched with expectancy records, {estimated} from factor log alone)")
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

    print(f"=== SIGNAL ATTRIBUTION — GROSS (min {args.min_trades} trades) ===\n")
    print(f"{'Sig':<6} {'N':>5} {'WR%':>6} "
          f"{'Exp/Trd':>9} {'AvgR':>7} {'Verdict':<9}")
    print("-" * 48)
    for r in rows:
        print(
            f"{r['sig']:<6} {r['trades']:>5} {r['wr']:>5.1f}% "
            f"{r['exp_gross']:>+9.3f} {r['avg_r']:>+7.3f} {r['verdict']:<9}"
        )

    cuts = [r for r in rows if r["verdict"] == "CUT"]
    keeps = [r for r in rows if r["verdict"] == "KEEP"]

    print(f"\n=== SUMMARY ===")
    print(f"KEEP:    {len(keeps)} signals   ({', '.join(r['sig'] for r in keeps) or 'none'})")
    print(f"MONITOR: {len([r for r in rows if r['verdict']=='MONITOR'])} signals")
    print(f"CUT:     {len(cuts)} signals   ({', '.join(r['sig'] for r in cuts) or 'none'})")

    # Total fleet cost of CUT signals
    if cuts:
        cut_cost = sum(r["total_gross"] for r in cuts)
        print(f"\nCumulative gross P/L from CUT signals: ${cut_cost:+.2f}")
        print(f"Removing them would have saved/cost that amount over the measurement period.")


if __name__ == "__main__":
    main()
