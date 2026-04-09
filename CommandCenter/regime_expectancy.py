"""
REGIME-CONDITIONAL EXPECTANCY

Primary data source: goldeneye_factors.log (TrekBot) — has regime, R-multiple,
signal list, PnL, and duration for every closed trade.

Secondary enrichment: logs/expectancy.json — has precise fee accounting.
Joined on (pair match, timestamp within 120s) when available.

For factor records that have no expectancy match (because the expectancy
tracker was not running for some TrekBot sessions — it was installed
2026-03-30 and has session gaps), fees are estimated from the Kraken taker
fee schedule (0.40% per side, 0.80% round-trip, tier 0) applied to the trade's
implied size.

Produces: per-regime expectancy net of fees.
This is the single most actionable table in the fleet.

Usage:
    python regime_expectancy.py
    python regime_expectancy.py --bot trekbot --days 7
    python regime_expectancy.py --since 2026-04-01
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

TREKBOT_DIR = os.environ.get("TREKBOT_DIR", r"D:\TrekBot")
FACTOR_LOG = os.path.join(TREKBOT_DIR, "goldeneye_factors.log")
EXPECTANCY_LOG = os.path.join(os.path.dirname(__file__), "logs", "expectancy.json")
MATCH_WINDOW_SEC = 120

# Kraken taker fee (per side, tier 0: $0-$10K/month, 2026-04).
KRAKEN_TAKER = 0.0040
KRAKEN_ROUNDTRIP = KRAKEN_TAKER * 2  # 0.80%


def _load_factors():
    records = []
    if not os.path.exists(FACTOR_LOG):
        return records
    with open(FACTOR_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                # only closed trades have pnl
                if r.get("pnl") is not None and r.get("regime") and r.get("sym"):
                    records.append(r)
            except Exception:
                pass
    # Deduplicate: factor log sometimes has near-duplicate records (same sym,
    # timestamp within 3s, same pnl). Keep the first occurrence.
    seen = set()
    unique = []
    for r in records:
        key = (r.get("sym"), round(r["t"], 0), round(r.get("pnl", 0), 2))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _load_expectancy(bot_filter="trekbot"):
    """Load expectancy records, filtered to a single bot.

    The factor log is TrekBot-only, so we must join only against TrekBot's
    expectancy records to avoid cross-bot pair/timestamp collisions as more
    bots accumulate data.
    """
    if not os.path.exists(EXPECTANCY_LOG):
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


def _to_ts(val):
    """Convert various timestamp formats to float epoch seconds."""
    if isinstance(val, (int, float)):
        # epoch ms vs epoch s heuristic
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
    """Compute mean trade size from available expectancy records for fee estimation.

    Returns the mean size_usd from matched TrekBot records, or a fallback
    based on observed P50=$12.51, mean=$28.60 distribution.
    """
    trek = [r for r in expectancy_records if r.get("_bot") == "trekbot" and r.get("size_usd")]
    if trek:
        sizes = [r["size_usd"] for r in trek if r["size_usd"] > 0]
        if sizes:
            return sum(sizes) / len(sizes)
    return 28.60  # Calibrated from 46 observed TrekBot trades (mean size)


# Module-level default; overwritten by _calibrate_default_size in join_records
_DEFAULT_SIZE = 28.60


def _estimate_fees_from_factor(f, default_size=None):
    """Estimate round-trip fees from factor log fields when expectancy record is missing.

    Uses PnL and R-multiple to back out approximate trade size, then applies
    Kraken taker round-trip rate. Falls back to calibrated mean size from
    matched expectancy records (observed mean ~$28.60, median ~$12.51).
    """
    if default_size is None:
        default_size = _DEFAULT_SIZE
    pnl = float(f.get("pnl", 0) or 0)
    r = float(f.get("r", 0) or 0)
    estimated_size = default_size
    if abs(r) > 0.01:
        # risk = |pnl / r|. TrekBot SL distance is typically 1-3% of entry.
        # size = risk / sl_pct. Use 2% as typical SL distance.
        risk = abs(pnl / r)
        size_from_r = risk / 0.02
        # Clamp to reasonable TrekBot range [$5, $500]
        if 5 <= size_from_r <= 500:
            estimated_size = size_from_r
    fees = estimated_size * KRAKEN_ROUNDTRIP
    return round(fees, 4), round(estimated_size, 2)


def join_records(factors, expectancy):
    """Join factor records with expectancy data. Factor log is primary source.

    For each factor record:
    - If a matching expectancy record exists (same pair, timestamp within 120s),
      use its precise fee/net_pnl data.
    - Otherwise, estimate fees from Kraken fee schedule and factor log fields.

    This ensures ALL factor records produce output, not just those with
    expectancy matches.

    Returns: (joined, matched_count, estimated_count, overlap_stats)
        overlap_stats = {"overlap_total": N, "overlap_matched": M} — match rate
        computed only over the period where expectancy data exists, which is
        the meaningful denominator (factor records before the expectancy tracker
        was installed cannot possibly match).
    """
    # Build expectancy index: {norm_pair: [(ts, record), ...]}
    exp_index = defaultdict(list)
    all_exp_ts = []
    for r in expectancy:
        ts = _to_ts(r.get("timestamp") or r.get("ts") or r.get("entry_time"))
        pair = _normalize_pair(r.get("pair") or r.get("symbol"))
        if ts and pair:
            exp_index[pair].append((ts, r))
            all_exp_ts.append(ts)

    # Sort each pair's expectancy records by timestamp for efficient matching
    for pair in exp_index:
        exp_index[pair].sort(key=lambda x: x[0])

    # Determine overlap period: earliest expectancy timestamp onward
    overlap_start = min(all_exp_ts) if all_exp_ts else float("inf")

    # Calibrate default size from available expectancy data
    cal_size = _calibrate_default_size(expectancy)

    # Track which expectancy records get consumed (prevent double-matching)
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

        best = None
        best_delta = MATCH_WINDOW_SEC + 1
        for idx, (ts_e, rec) in enumerate(exp_index.get(pair_f, [])):
            if id(rec) in used_exp:
                continue
            delta = abs(ts_e - ts_f)
            if delta < best_delta:
                best_delta = delta
                best = rec

        if best and best_delta <= MATCH_WINDOW_SEC:
            # Matched — use precise fee data from expectancy tracker
            used_exp.add(id(best))
            gross_pnl = float(best.get("gross_pnl", 0) or 0)
            fees = float(best.get("fees", 0) or 0)
            net_pnl = float(best.get("net_pnl", 0) or 0)
            matched_count += 1
            source = "matched"
            if in_overlap:
                overlap_matched += 1
        else:
            # Unmatched — estimate fees from factor log + Kraken schedule
            gross_pnl = float(f.get("pnl", 0) or 0)
            fees, _ = _estimate_fees_from_factor(f, default_size=cal_size)
            net_pnl = gross_pnl - fees
            estimated_count += 1
            source = "estimated"

        if in_overlap:
            overlap_total += 1

        joined.append({
            "regime": str(f.get("regime", "unknown")).lower(),
            "pair": pair_f,
            "sigs": f.get("sigs", []),
            "r_gross": float(f.get("r", 0) or 0),
            "gross_pnl": gross_pnl,
            "fees": fees,
            "net_pnl": net_pnl,
            "won": net_pnl > 0,
            "_source": source,
        })

    overlap_stats = {
        "overlap_start": overlap_start,
        "overlap_total": overlap_total,
        "overlap_matched": overlap_matched,
    }
    return joined, matched_count, estimated_count, overlap_stats


def compute_regime_table(joined):
    """Per-regime stats."""
    buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "gross": 0.0, "fees": 0.0, "net": 0.0})
    for r in joined:
        b = buckets[r["regime"]]
        b["trades"] += 1
        b["wins"] += int(r["won"])
        b["gross"] += r["gross_pnl"]
        b["fees"] += r["fees"]
        b["net"] += r["net_pnl"]

    rows = []
    for regime, b in sorted(buckets.items(), key=lambda x: x[1]["trades"], reverse=True):
        n = b["trades"]
        wr = b["wins"] / n * 100 if n else 0
        exp_net = b["net"] / n if n else 0
        fee_ratio = (b["fees"] / abs(b["gross"]) * 100) if b["gross"] != 0 else float("inf")
        rows.append({
            "regime": regime, "trades": n, "win_rate": wr,
            "gross_pnl": b["gross"], "fees": b["fees"], "net_pnl": b["net"],
            "expectancy_net": exp_net, "fee_ratio_pct": fee_ratio,
        })
    return rows


def compute_signal_table(joined):
    """Per-signal net expectancy (only signals with 5+ appearances)."""
    sig_buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0})
    for r in joined:
        for sig in r.get("sigs", []):
            b = sig_buckets[sig]
            b["trades"] += 1
            b["wins"] += int(r["won"])
            b["net"] += r["net_pnl"]

    rows = []
    for sig, b in sorted(sig_buckets.items(), key=lambda x: x[1]["net"] / max(x[1]["trades"], 1)):
        if b["trades"] < 5:
            continue
        rows.append({
            "signal": sig, "trades": b["trades"],
            "win_rate": b["wins"] / b["trades"] * 100,
            "expectancy_net": b["net"] / b["trades"],
            "total_net": b["net"],
        })
    return rows


def print_table(headers, rows, fmt):
    col_w = [len(h) for h in headers]
    formatted = []
    for row in rows:
        fr = [f % v if isinstance(v, float) else str(v) for f, v in zip(fmt, row)]
        formatted.append(fr)
        for i, cell in enumerate(fr):
            col_w[i] = max(col_w[i], len(cell))

    sep = "+-" + "-+-".join("-" * w for w in col_w) + "-+"
    header_row = "| " + " | ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)) + " |"
    print(sep)
    print(header_row)
    print(sep)
    for fr in formatted:
        print("| " + " | ".join(c.ljust(col_w[i]) for i, c in enumerate(fr)) + " |")
    print(sep)


def _parse_args():
    p = argparse.ArgumentParser(description="Regime-conditional expectancy analysis")
    p.add_argument("--bot", default="trekbot", help="Bot to analyze (default: trekbot)")
    p.add_argument("--days", type=int, default=0, help="Restrict to last N days (0=all)")
    p.add_argument("--since", default=None,
                   help="Only analyze trades after this date (YYYY-MM-DD). "
                        "Use to restrict to the period where expectancy tracker was running.")
    return p.parse_args()


def main():
    args = _parse_args()
    factors = _load_factors()
    expectancy = _load_expectancy(bot_filter=args.bot)

    # Apply --since filter
    since_ts = None
    if args.since:
        try:
            since_ts = datetime.strptime(args.since, "%Y-%m-%d").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            print(f"Invalid --since date: {args.since} (expected YYYY-MM-DD)")
            sys.exit(1)
        factors = [f for f in factors if (f.get("t") or 0) >= since_ts]
    elif args.days > 0:
        import time as _time
        cutoff = _time.time() - args.days * 86400
        factors = [f for f in factors if (f.get("t") or 0) >= cutoff]

    if not factors:
        print(f"No factor log found at {FACTOR_LOG}")
        sys.exit(1)

    print(f"Loaded {len(factors)} factor records (deduplicated), {len(expectancy)} {args.bot} expectancy records")

    joined, matched, estimated, overlap = join_records(factors, expectancy)
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

    # Regime table
    regime_rows = compute_regime_table(joined)
    print("=== REGIME-CONDITIONAL EXPECTANCY (net of fees) ===\n")
    headers = ["Regime", "Trades", "WR%", "Gross PnL", "Fees", "Net PnL", "Exp/Trade", "Fee Ratio%"]
    fmt = ["%s", "%d", "%.1f", "$%.2f", "$%.2f", "$%.2f", "$%.2f", "%.0f%%"]
    rows = [[r["regime"], r["trades"], r["win_rate"], r["gross_pnl"],
             r["fees"], r["net_pnl"], r["expectancy_net"], r["fee_ratio_pct"]]
            for r in regime_rows]
    print_table(headers, rows, fmt)

    # Signal table
    sig_rows = compute_signal_table(joined)
    if sig_rows:
        print("\n=== SIGNAL NET EXPECTANCY (5+ appearances, worst to best) ===\n")
        headers2 = ["Signal", "Trades", "WR%", "Exp/Trade(net)", "Total Net"]
        fmt2 = ["%s", "%d", "%.1f", "$%.3f", "$%.2f"]
        rows2 = [[r["signal"], r["trades"], r["win_rate"], r["expectancy_net"], r["total_net"]]
                 for r in sig_rows]
        print_table(headers2, rows2, fmt2)

    # Headline verdict
    print("\n=== VERDICT ===")
    positive = [r for r in regime_rows if r["expectancy_net"] > 0]
    negative = [r for r in regime_rows if r["expectancy_net"] <= 0]
    if positive:
        print(f"Positive expectancy regimes: {', '.join(r['regime'] for r in positive)}")
    if negative:
        print(f"Negative expectancy regimes: {', '.join(r['regime'] for r in negative)}")
    if not positive:
        print("No regime shows positive net expectancy. Edge may be gone or sample too small.")


if __name__ == "__main__":
    main()
