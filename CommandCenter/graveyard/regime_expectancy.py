"""
REGIME-CONDITIONAL EXPECTANCY (LEGACY)

LEGACY MODULE: depends on goldeneye_factors.log from TrekBot, which left
the fleet — the log no longer updates. Kept for historical analysis only.

GROSS SEMANTICS (since 2026-07-30): the fleet is a signal product — all
P/L here is GROSS price movement. No fees are calculated, estimated, or
deducted; subscribers pay whatever their own exchange charges.
logs/expectancy.json records written before 2026-07-30 carry net-of-fees
`net_pnl` values; this module reads only their `gross_pnl` field.

Primary data source: goldeneye_factors.log (TrekBot) — has regime,
R-multiple, signal list, PnL, and duration for every closed trade.

Secondary enrichment: logs/expectancy.json — precise recorded PnL.
Joined on (pair match, timestamp within 120s) when available.

Produces: per-regime gross expectancy.

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


def join_records(factors, expectancy):
    """Join factor records with expectancy data. Factor log is primary source.

    For each factor record:
    - If a matching expectancy record exists (same pair, timestamp within 120s),
      use its recorded gross_pnl.
    - Otherwise, use the factor log's own pnl.
    All values are GROSS price movement — no fees (signal product, 2026-07-30).

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
            "regime": str(f.get("regime", "unknown")).lower(),
            "pair": pair_f,
            "sigs": f.get("sigs", []),
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


def compute_regime_table(joined):
    """Per-regime stats (gross price movement)."""
    buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "gross": 0.0})
    for r in joined:
        b = buckets[r["regime"]]
        b["trades"] += 1
        b["wins"] += int(r["won"])
        b["gross"] += r["gross_pnl"]

    rows = []
    for regime, b in sorted(buckets.items(), key=lambda x: x[1]["trades"], reverse=True):
        n = b["trades"]
        wr = b["wins"] / n * 100 if n else 0
        exp_gross = b["gross"] / n if n else 0
        rows.append({
            "regime": regime, "trades": n, "win_rate": wr,
            "gross_pnl": b["gross"],
            "expectancy_gross": exp_gross,
        })
    return rows


def compute_signal_table(joined):
    """Per-signal gross expectancy (only signals with 5+ appearances)."""
    sig_buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "gross": 0.0})
    for r in joined:
        for sig in r.get("sigs", []):
            b = sig_buckets[sig]
            b["trades"] += 1
            b["wins"] += int(r["won"])
            b["gross"] += r["gross_pnl"]

    rows = []
    for sig, b in sorted(sig_buckets.items(), key=lambda x: x[1]["gross"] / max(x[1]["trades"], 1)):
        if b["trades"] < 5:
            continue
        rows.append({
            "signal": sig, "trades": b["trades"],
            "win_rate": b["wins"] / b["trades"] * 100,
            "expectancy_gross": b["gross"] / b["trades"],
            "total_gross": b["gross"],
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

    # Regime table
    regime_rows = compute_regime_table(joined)
    print("=== REGIME-CONDITIONAL EXPECTANCY (gross price movement) ===\n")
    headers = ["Regime", "Trades", "WR%", "Gross PnL", "Exp/Trade"]
    fmt = ["%s", "%d", "%.1f", "$%.2f", "$%.2f"]
    rows = [[r["regime"], r["trades"], r["win_rate"], r["gross_pnl"],
             r["expectancy_gross"]]
            for r in regime_rows]
    print_table(headers, rows, fmt)

    # Signal table
    sig_rows = compute_signal_table(joined)
    if sig_rows:
        print("\n=== SIGNAL GROSS EXPECTANCY (5+ appearances, worst to best) ===\n")
        headers2 = ["Signal", "Trades", "WR%", "Exp/Trade", "Total Gross"]
        fmt2 = ["%s", "%d", "%.1f", "$%.3f", "$%.2f"]
        rows2 = [[r["signal"], r["trades"], r["win_rate"], r["expectancy_gross"], r["total_gross"]]
                 for r in sig_rows]
        print_table(headers2, rows2, fmt2)

    # Headline verdict
    print("\n=== VERDICT ===")
    positive = [r for r in regime_rows if r["expectancy_gross"] > 0]
    negative = [r for r in regime_rows if r["expectancy_gross"] <= 0]
    if positive:
        print(f"Positive expectancy regimes: {', '.join(r['regime'] for r in positive)}")
    if negative:
        print(f"Negative expectancy regimes: {', '.join(r['regime'] for r in negative)}")
    if not positive:
        print("No regime shows positive gross expectancy. Edge may be gone or sample too small.")


if __name__ == "__main__":
    main()
