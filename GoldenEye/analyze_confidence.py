#!/usr/bin/env python3
"""
analyze_confidence.py — Confidence-vs-outcome curve for GoldenEye trades.

Reads goldeneye_factors.log (one JSON line per closed trade) and prints:
  1. Confidence bucket analysis (win rate and avg R per bucket)
  2. Per-signal breakdown (win rate and avg R per signal letter)
  3. Regime breakdown

By default, filters out the 5 ancient paper trades by skipping the first 5
entries in the file. Override with --all to include them, or --since-date
YYYY-MM-DD to cut at a specific date.

Usage:
    python analyze_confidence.py                # default: skip ancient 5
    python analyze_confidence.py --all          # include everything
    python analyze_confidence.py --since 2026-04-13  # live trades only
    python analyze_confidence.py --file path/to/factors.log
"""
import argparse
import datetime
import json
import os
import sys
from collections import defaultdict


DEFAULT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'goldeneye_factors.log')
ANCIENT_PAPER_COUNT = 5  # first 5 entries are pre-live paper trades from brain-wipe era


def load_trades(path, skip_ancient=True, since_ts=None):
    if not os.path.exists(path):
        print(f"ERROR: {path} not found", file=sys.stderr)
        sys.exit(1)
    trades = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                trades.append(t)
            except json.JSONDecodeError:
                continue
    if skip_ancient and since_ts is None:
        trades = trades[ANCIENT_PAPER_COUNT:]
    if since_ts is not None:
        trades = [t for t in trades if (t.get('t') or 0) >= since_ts]
    return trades


def confidence_buckets(trades):
    buckets = [
        ('<0.25', 0.00, 0.25),
        ('0.25-0.30', 0.25, 0.30),
        ('0.30-0.35', 0.30, 0.35),
        ('0.35-0.40', 0.35, 0.40),
        ('0.40-0.45', 0.40, 0.45),
        ('0.45-0.50', 0.45, 0.50),
        ('0.50-0.55', 0.50, 0.55),
        ('>=0.55', 0.55, 999),
    ]
    out = []
    for name, lo, hi in buckets:
        group = [t for t in trades
                 if t.get('confidence') is not None
                 and lo <= float(t['confidence']) < hi
                 and t.get('r') is not None]
        if not group:
            out.append((name, 0, 0, 0.0, 0.0))
            continue
        n = len(group)
        rs = [float(t['r']) for t in group]
        wins = sum(1 for r in rs if r > 0)
        wr = wins / n * 100
        avg_r = sum(rs) / n
        out.append((name, n, wins, wr, avg_r))
    return out


def per_signal(trades):
    # sigs is the post-confluence reduced list per _log_factor_trade output
    stats = defaultdict(lambda: {'n': 0, 'wins': 0, 'r_sum': 0.0})
    for t in trades:
        if t.get('r') is None:
            continue
        r = float(t['r'])
        for s in (t.get('sigs') or []):
            stats[s]['n'] += 1
            stats[s]['r_sum'] += r
            if r > 0:
                stats[s]['wins'] += 1
    out = []
    for sig, s in sorted(stats.items()):
        if s['n'] == 0:
            continue
        wr = s['wins'] / s['n'] * 100
        avg_r = s['r_sum'] / s['n']
        out.append((sig, s['n'], s['wins'], wr, avg_r))
    return out


def per_regime(trades):
    stats = defaultdict(lambda: {'n': 0, 'wins': 0, 'r_sum': 0.0})
    for t in trades:
        if t.get('r') is None:
            continue
        rgm = t.get('regime') or 'unknown'
        r = float(t['r'])
        stats[rgm]['n'] += 1
        stats[rgm]['r_sum'] += r
        if r > 0:
            stats[rgm]['wins'] += 1
    out = []
    for rgm, s in sorted(stats.items()):
        wr = s['wins'] / s['n'] * 100
        avg_r = s['r_sum'] / s['n']
        out.append((rgm, s['n'], s['wins'], wr, avg_r))
    return out


def overall(trades):
    rs = [float(t['r']) for t in trades if t.get('r') is not None]
    pnls = [float(t['pnl']) for t in trades if t.get('pnl') is not None]
    if not rs:
        return None
    wins = sum(1 for r in rs if r > 0)
    return {
        'n': len(rs),
        'wins': wins,
        'losses': len(rs) - wins,
        'wr': wins / len(rs) * 100,
        'avg_r': sum(rs) / len(rs),
        'total_pnl': sum(pnls),
    }


def print_table(title, rows, headers):
    print()
    print(f"=== {title} ===")
    col_widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    fmt = '  '.join('{:<' + str(w) + '}' for w in col_widths)
    print(fmt.format(*headers))
    print('  '.join('-' * w for w in col_widths))
    for r in rows:
        print(fmt.format(*r))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--file', default=DEFAULT_LOG, help=f'factor log path (default: {DEFAULT_LOG})')
    ap.add_argument('--all', action='store_true', help='include all entries (skip default filter of first 5 ancient paper trades)')
    ap.add_argument('--since', help='only trades on/after this date (YYYY-MM-DD)')
    args = ap.parse_args()

    since_ts = None
    if args.since:
        try:
            since_ts = datetime.datetime.strptime(args.since, '%Y-%m-%d').timestamp()
        except ValueError:
            print(f"ERROR: invalid --since date (expected YYYY-MM-DD), got: {args.since}", file=sys.stderr)
            sys.exit(1)

    trades = load_trades(args.file, skip_ancient=not args.all, since_ts=since_ts)

    if not trades:
        print(f"No trades in {args.file} after filtering.")
        sys.exit(0)

    print(f"GoldenEye confidence analysis")
    print(f"  source: {args.file}")
    if args.since:
        print(f"  filter: trades on/after {args.since}")
    elif args.all:
        print(f"  filter: none (all {len(trades)} entries)")
    else:
        print(f"  filter: skip first {ANCIENT_PAPER_COUNT} ancient paper trades")

    o = overall(trades)
    print()
    print(f"Overall: n={o['n']}  W/L={o['wins']}/{o['losses']}  WR={o['wr']:.1f}%  "
          f"avg R={o['avg_r']:+.3f}  total P/L=${o['total_pnl']:+.2f}")

    rows = [
        (name, f"{n}", f"{wins}", f"{wr:.0f}%" if n else '-', f"{avg_r:+.2f}" if n else '-')
        for name, n, wins, wr, avg_r in confidence_buckets(trades)
    ]
    print_table('Confidence buckets', rows, ('bucket', 'n', 'wins', 'WR', 'avg R'))

    rows = [
        (sig, f"{n}", f"{wins}", f"{wr:.0f}%", f"{avg_r:+.2f}")
        for sig, n, wins, wr, avg_r in per_signal(trades)
    ]
    if rows:
        print_table('Per-signal (post-confluence)', rows, ('sig', 'n', 'wins', 'WR', 'avg R'))

    rows = [
        (rgm, f"{n}", f"{wins}", f"{wr:.0f}%", f"{avg_r:+.2f}")
        for rgm, n, wins, wr, avg_r in per_regime(trades)
    ]
    if rows:
        print_table('Per-regime', rows, ('regime', 'n', 'wins', 'WR', 'avg R'))

    print()
    print(f"# Rerun: python {os.path.basename(__file__)} [--all | --since YYYY-MM-DD]")


if __name__ == '__main__':
    main()
