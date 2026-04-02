#!/usr/bin/env python3
"""
FLEET ANALYZER — CLI Tool for Querying Fleet Logs
===================================================
Reads JSONL log files written by fleet_logger.py and prints
clean tables for diagnostics.

Usage:
    python analyze.py bot nexusbrain --date 2026-03-28
    python analyze.py trades --exit-reason stop_loss --last 7d
    python analyze.py equity --last 7d
    python analyze.py regimes --last 7d
    python analyze.py pairs --sort pnl --last 7d
    python analyze.py correlation whale_alerts trades --last 30d
    python analyze.py leaderboard --last 30d
    python analyze.py uptime trinity --last 7d
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
SNAPSHOT_DIR = os.path.join(LOG_DIR, "snapshots")
EVENT_DIR = os.path.join(LOG_DIR, "events")
DAILY_DIR = os.path.join(LOG_DIR, "daily")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_last(last_str):
    """Parse '7d', '30d', '24h' into a start timestamp."""
    last_str = last_str.strip().lower()
    try:
        if last_str.endswith("d"):
            days = int(last_str[:-1])
            return time.time() - days * 86400
        elif last_str.endswith("h"):
            hours = int(last_str[:-1])
            return time.time() - hours * 3600
        else:
            days = int(last_str)
            return time.time() - days * 86400
    except ValueError:
        print(f"Error: invalid --last value '{last_str}'. Use e.g. '7d', '24h', or '30'.")
        sys.exit(1)


def _date_range(start_ts, end_ts=None):
    """Generate date strings between two timestamps."""
    end_ts = end_ts or time.time()
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    dates = []
    current = start_dt.date()
    while current <= end_dt.date():
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def _read_jsonl(directory, dates):
    """Read all JSONL lines from the given dates."""
    lines = []
    for date_str in dates:
        path = os.path.join(directory, f"{date_str}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return lines


def _read_daily(dates):
    """Read daily summary JSONs for the given dates."""
    summaries = []
    for date_str in dates:
        path = os.path.join(DAILY_DIR, f"{date_str}.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    summaries.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
    return summaries


def _ts_str(ts):
    """Format timestamp as readable string."""
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _duration_str(seconds):
    """Format duration in seconds to human readable."""
    if not seconds or seconds < 0:
        return "N/A"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def _pnl_str(pnl):
    """Format PnL with color indicator."""
    if pnl > 0:
        return f"+${pnl:.2f}"
    elif pnl < 0:
        return f"-${abs(pnl):.2f}"
    return "$0.00"


def _print_table(headers, rows, col_widths=None):
    """Print a formatted table."""
    if not col_widths:
        col_widths = []
        for i, h in enumerate(headers):
            max_w = len(h)
            for row in rows:
                if i < len(row):
                    max_w = max(max_w, len(str(row[i])))
            col_widths.append(min(max_w + 2, 40))

    # Header
    header_line = ""
    for i, h in enumerate(headers):
        header_line += str(h).ljust(col_widths[i])
    print(header_line)
    print("-" * len(header_line))

    # Rows
    for row in rows:
        line = ""
        for i, val in enumerate(row):
            if i < len(col_widths):
                line += str(val).ljust(col_widths[i])
        print(line)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_bot(args):
    """Show bot activity for a date range."""
    bot_id = args.bot_id
    if args.date:
        dates = [args.date]
    else:
        start = _parse_last(args.last)
        dates = _date_range(start)

    events = _read_jsonl(EVENT_DIR, dates)
    bot_events = [e for e in events if e.get("bot") == bot_id]

    if not bot_events:
        print(f"No events found for {bot_id} in {dates[0]} to {dates[-1]}")
        return

    # Summary
    trades_open = [e for e in bot_events if e.get("type") == "TRADE_OPEN"]
    trades_close = [e for e in bot_events if e.get("type") == "TRADE_CLOSE"]
    regime_changes = [e for e in bot_events if e.get("type") == "REGIME_CHANGE"]
    status_changes = [e for e in bot_events if e.get("type") == "STATUS_CHANGE"]

    total_pnl = sum(t.get("pnl", 0) for t in trades_close)
    wins = sum(1 for t in trades_close if t.get("pnl", 0) > 0)
    losses = len(trades_close) - wins

    print(f"\n  {bot_id.upper()} — {dates[0]} to {dates[-1]}")
    print(f"  {'='*50}")
    print(f"  Trades opened:   {len(trades_open)}")
    print(f"  Trades closed:   {len(trades_close)}")
    print(f"  Wins / Losses:   {wins}W / {losses}L")
    print(f"  Total PnL:       {_pnl_str(total_pnl)}")
    print(f"  Regime changes:  {len(regime_changes)}")
    print(f"  Status changes:  {len(status_changes)}")
    print()

    if args.trades and trades_close:
        print("  CLOSED TRADES:")
        headers = ["Time", "Pair", "PnL", "Duration", "Exit Reason"]
        rows = []
        for t in sorted(trades_close, key=lambda x: x.get("ts", 0)):
            rows.append([
                _ts_str(t.get("ts")),
                t.get("pair", ""),
                _pnl_str(t.get("pnl", 0)),
                _duration_str(t.get("duration_s", 0)),
                t.get("exit_reason", "unknown"),
            ])
        _print_table(headers, rows)
        print()

    if args.trades and trades_open:
        print("  OPENED TRADES:")
        headers = ["Time", "Pair", "Direction", "Entry", "Size"]
        rows = []
        for t in sorted(trades_open, key=lambda x: x.get("ts", 0)):
            rows.append([
                _ts_str(t.get("ts")),
                t.get("pair", ""),
                t.get("direction", ""),
                f"${float(str(t.get('entry', 0)).replace(',', '')):.2f}" if t.get("entry") else "N/A",
                f"${float(str(t.get('size', 0)).replace(',', '')):.2f}" if t.get("size") else "N/A",
            ])
        _print_table(headers, rows)
        print()


def cmd_trades(args):
    """Show all trades, optionally filtered."""
    start = _parse_last(args.last)
    dates = _date_range(start)
    events = _read_jsonl(EVENT_DIR, dates)

    trades = [e for e in events if e.get("type") in ("TRADE_OPEN", "TRADE_CLOSE")]

    if args.exit_reason:
        trades = [t for t in trades if t.get("exit_reason", "").lower() == args.exit_reason.lower()]

    if args.pair:
        trades = [t for t in trades if t.get("pair", "").upper() == args.pair.upper()]

    if args.bot:
        trades = [t for t in trades if t.get("bot") == args.bot]

    if not trades:
        print("No matching trades found.")
        return

    trades.sort(key=lambda x: x.get("ts", 0), reverse=True)

    headers = ["Time", "Bot", "Type", "Pair", "Direction", "PnL", "Duration", "Exit"]
    rows = []
    for t in trades[:100]:  # cap output
        rows.append([
            _ts_str(t.get("ts")),
            t.get("bot", ""),
            t.get("type", ""),
            t.get("pair", ""),
            t.get("direction", ""),
            _pnl_str(t.get("pnl", 0)) if t.get("type") == "TRADE_CLOSE" else "",
            _duration_str(t.get("duration_s", 0)) if t.get("duration_s") else "",
            t.get("exit_reason", "") if t.get("type") == "TRADE_CLOSE" else "",
        ])

    print(f"\n  TRADES — last {args.last}")
    print(f"  Total: {len(trades)}")
    print()
    _print_table(headers, rows)
    print()


def cmd_equity(args):
    """Show equity curve from snapshots."""
    start = _parse_last(args.last)
    dates = _date_range(start)
    snapshots = _read_jsonl(SNAPSHOT_DIR, dates)

    if not snapshots:
        print("No snapshots found.")
        return

    # Sample: one per hour for readability
    hourly = {}
    for snap in snapshots:
        ts = snap.get("ts", 0)
        hour_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
        hourly[hour_key] = snap  # last snapshot of each hour wins

    headers = ["Time", "Equity", "Deployed", "Available", "Positions", "Bots Alive"]
    rows = []
    for hour_key in sorted(hourly.keys()):
        snap = hourly[hour_key]
        agg = snap.get("aggregate", {})
        port = snap.get("portfolio") or {}
        rows.append([
            hour_key,
            f"${port.get('total', 0):,.2f}" if port.get("total") else "N/A",
            f"${port.get('deployed', 0):,.2f}" if port else "N/A",
            f"${port.get('available', 0):,.2f}" if port else "N/A",
            agg.get("total_open_positions", "N/A"),
            f"{agg.get('bots_alive', 0)}/{agg.get('bots_total', 0)}",
        ])

    print(f"\n  EQUITY CURVE — last {args.last} (hourly samples)")
    print()
    _print_table(headers, rows)
    print()


def cmd_regimes(args):
    """Show regime changes and what followed."""
    start = _parse_last(args.last)
    dates = _date_range(start)
    events = _read_jsonl(EVENT_DIR, dates)

    regimes = [e for e in events if e.get("type") == "REGIME_CHANGE"]

    if not regimes:
        print("No regime changes found.")
        return

    # For each regime change, find trades in the next hour
    all_trades = [e for e in events if e.get("type") == "TRADE_CLOSE"]

    headers = ["Time", "Bot", "From", "To", "Trades (1h after)", "PnL (1h after)"]
    rows = []
    for r in sorted(regimes, key=lambda x: x.get("ts", 0)):
        ts = r.get("ts", 0)
        # Find trades within 1 hour after this regime change
        window_trades = [t for t in all_trades if ts <= t.get("ts", 0) <= ts + 3600]
        window_pnl = sum(t.get("pnl", 0) for t in window_trades)

        rows.append([
            _ts_str(ts),
            r.get("bot", ""),
            r.get("from", ""),
            r.get("to", ""),
            len(window_trades),
            _pnl_str(window_pnl),
        ])

    print(f"\n  REGIME CHANGES — last {args.last}")
    print()
    _print_table(headers, rows)
    print()


def cmd_pairs(args):
    """Show per-pair performance across all bots."""
    start = _parse_last(args.last)
    dates = _date_range(start)
    events = _read_jsonl(EVENT_DIR, dates)

    trades = [e for e in events if e.get("type") == "TRADE_CLOSE"]

    if not trades:
        print("No closed trades found.")
        return

    # Aggregate by pair
    pair_stats = {}
    for t in trades:
        pair = t.get("pair", "UNKNOWN")
        if pair not in pair_stats:
            pair_stats[pair] = {"trades": 0, "wins": 0, "pnl": 0, "bots": set()}
        p = pair_stats[pair]
        p["trades"] += 1
        p["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            p["wins"] += 1
        p["bots"].add(t.get("bot", ""))

    # Sort
    sort_key = args.sort if args.sort else "pnl"
    if sort_key == "pnl":
        sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["pnl"])
    elif sort_key == "trades":
        sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["trades"], reverse=True)
    elif sort_key == "winrate":
        sorted_pairs = sorted(pair_stats.items(),
                              key=lambda x: x[1]["wins"] / max(x[1]["trades"], 1), reverse=True)
    else:
        sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]["pnl"])

    headers = ["Pair", "Trades", "Wins", "Losses", "Win Rate", "PnL", "Bots"]
    rows = []
    for pair, s in sorted_pairs:
        wr = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
        rows.append([
            pair,
            s["trades"],
            s["wins"],
            s["trades"] - s["wins"],
            f"{wr:.0f}%",
            _pnl_str(s["pnl"]),
            ", ".join(sorted(s["bots"])),
        ])

    print(f"\n  PAIR PERFORMANCE — last {args.last} (sorted by {sort_key})")
    print()
    _print_table(headers, rows)
    print()


def cmd_correlation(args):
    """Check correlation between event types (e.g. whale_alerts vs trades)."""
    start = _parse_last(args.last)
    dates = _date_range(start)
    events = _read_jsonl(EVENT_DIR, dates)

    type_a = args.type_a.upper()
    type_b = args.type_b.upper()

    # Map common aliases
    type_map = {
        "WHALE_ALERTS": "WHALE_ALERT",
        "TRADES": "TRADE_CLOSE",
        "REGIME_CHANGES": "REGIME_CHANGE",
        "TRADE_OPENS": "TRADE_OPEN",
        "TRADE_CLOSES": "TRADE_CLOSE",
    }
    type_a = type_map.get(type_a, type_a)
    type_b = type_map.get(type_b, type_b)

    events_a = sorted([e for e in events if e.get("type") == type_a], key=lambda x: x.get("ts", 0))
    events_b = sorted([e for e in events if e.get("type") == type_b], key=lambda x: x.get("ts", 0))

    if not events_a:
        print(f"No {type_a} events found.")
        return
    if not events_b:
        print(f"No {type_b} events found.")
        return

    # For each type_a event, count type_b events within 1 hour window
    windows = [300, 1800, 3600]  # 5min, 30min, 1hr
    window_labels = ["5min", "30min", "1hr"]

    print(f"\n  CORRELATION: {type_a} -> {type_b}")
    print(f"  {type_a} events: {len(events_a)}")
    print(f"  {type_b} events: {len(events_b)}")
    print()

    for window, label in zip(windows, window_labels):
        total_following = 0
        total_pnl = 0
        events_with_following = 0

        for ea in events_a:
            ts = ea.get("ts", 0)
            pair = ea.get("pair", "")
            following = [eb for eb in events_b
                         if ts <= eb.get("ts", 0) <= ts + window
                         and (not pair or eb.get("pair", "") == pair or not eb.get("pair"))]
            if following:
                events_with_following += 1
                total_following += len(following)
                total_pnl += sum(f.get("pnl", 0) for f in following)

        hit_rate = (events_with_following / len(events_a) * 100) if events_a else 0
        avg_following = (total_following / len(events_a)) if events_a else 0

        print(f"  Window {label}:")
        print(f"    {type_a} followed by {type_b}: {events_with_following}/{len(events_a)} ({hit_rate:.0f}%)")
        print(f"    Avg {type_b} per {type_a}: {avg_following:.1f}")
        if type_b == "TRADE_CLOSE":
            print(f"    Total PnL in window: {_pnl_str(total_pnl)}")
        print()


def cmd_leaderboard(args):
    """Bot comparison leaderboard."""
    start = _parse_last(args.last)
    dates = _date_range(start)

    # Try daily summaries first
    summaries = _read_daily(dates)

    if summaries:
        # Aggregate from daily summaries
        bot_totals = {}
        for s in summaries:
            for bid, stats in s.get("per_bot", {}).items():
                if bid not in bot_totals:
                    bot_totals[bid] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0, "days_active": 0}
                b = bot_totals[bid]
                b["trades"] += stats.get("trades", 0)
                b["wins"] += stats.get("wins", 0)
                b["losses"] += stats.get("losses", 0)
                b["pnl"] += stats.get("pnl", 0)
                if stats.get("trades", 0) > 0:
                    b["days_active"] += 1
    else:
        # Fall back to events
        events = _read_jsonl(EVENT_DIR, dates)
        trades = [e for e in events if e.get("type") == "TRADE_CLOSE"]
        bot_totals = {}
        for t in trades:
            bid = t.get("bot", "unknown")
            if bid not in bot_totals:
                bot_totals[bid] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0, "days_active": 0}
            b = bot_totals[bid]
            b["trades"] += 1
            pnl = t.get("pnl", 0)
            b["pnl"] += pnl
            if pnl > 0:
                b["wins"] += 1
            else:
                b["losses"] += 1

    if not bot_totals:
        print("No trade data found.")
        return

    # Sort by PnL descending
    sorted_bots = sorted(bot_totals.items(), key=lambda x: x[1]["pnl"], reverse=True)

    headers = ["#", "Bot", "Trades", "Wins", "Losses", "Win Rate", "PnL"]
    rows = []
    for i, (bid, b) in enumerate(sorted_bots, 1):
        wr = (b["wins"] / b["trades"] * 100) if b["trades"] > 0 else 0
        rows.append([
            i,
            bid,
            b["trades"],
            b["wins"],
            b["losses"],
            f"{wr:.0f}%",
            _pnl_str(b["pnl"]),
        ])

    print(f"\n  LEADERBOARD — last {args.last}")
    print()
    _print_table(headers, rows)
    print()


def cmd_uptime(args):
    """Show bot uptime/downtime transitions."""
    bot_id = args.bot_id
    start = _parse_last(args.last)
    dates = _date_range(start)
    events = _read_jsonl(EVENT_DIR, dates)

    status_events = [e for e in events if e.get("bot") == bot_id and e.get("type") == "STATUS_CHANGE"]

    if not status_events:
        print(f"No status changes found for {bot_id}.")
        # Check snapshots for alive status
        snapshots = _read_jsonl(SNAPSHOT_DIR, dates[-3:] if len(dates) > 3 else dates)
        alive_count = 0
        total = 0
        for snap in snapshots:
            bot_data = snap.get("bots", {}).get(bot_id, {})
            total += 1
            if bot_data.get("alive"):
                alive_count += 1
        if total > 0:
            pct = alive_count / total * 100
            print(f"  From snapshots: {bot_id} was alive in {alive_count}/{total} checks ({pct:.0f}%)")
        return

    headers = ["Time", "Transition", "Duration Since Last"]
    rows = []
    prev_ts = None
    for e in sorted(status_events, key=lambda x: x.get("ts", 0)):
        dur = ""
        if prev_ts:
            dur = _duration_str(e.get("ts", 0) - prev_ts)
        rows.append([
            _ts_str(e.get("ts")),
            f"{e.get('from', '')} -> {e.get('to', '')}",
            dur,
        ])
        prev_ts = e.get("ts", 0)

    offline_count = sum(1 for e in status_events if e.get("to") == "OFFLINE")
    online_count = sum(1 for e in status_events if e.get("to") == "ONLINE")

    print(f"\n  UPTIME: {bot_id.upper()} — last {args.last}")
    print(f"  Went offline: {offline_count} times")
    print(f"  Came online:  {online_count} times")
    print()
    _print_table(headers, rows)
    print()


# ---------------------------------------------------------------------------
# Main / Argparse
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fleet Analyzer — Query fleet logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python analyze.py bot nexusbrain --date 2026-03-28 --trades
  python analyze.py trades --exit-reason stop_loss --last 7d
  python analyze.py equity --last 7d
  python analyze.py regimes --last 7d
  python analyze.py pairs --sort pnl --last 7d
  python analyze.py correlation whale_alerts trades --last 30d
  python analyze.py leaderboard --last 30d
  python analyze.py uptime trinity --last 7d
        """
    )

    sub = parser.add_subparsers(dest="command", help="Analysis command")

    # bot
    p_bot = sub.add_parser("bot", help="Show bot activity")
    p_bot.add_argument("bot_id", help="Bot ID (e.g. nexusbrain)")
    p_bot.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    p_bot.add_argument("--last", default="1d", help="Time range (e.g. 7d, 24h)")
    p_bot.add_argument("--trades", action="store_true", help="Show individual trades")

    # trades
    p_trades = sub.add_parser("trades", help="Show all trades")
    p_trades.add_argument("--exit-reason", help="Filter by exit reason")
    p_trades.add_argument("--pair", help="Filter by pair")
    p_trades.add_argument("--bot", help="Filter by bot")
    p_trades.add_argument("--last", default="7d", help="Time range")

    # equity
    p_equity = sub.add_parser("equity", help="Show equity curve")
    p_equity.add_argument("--last", default="7d", help="Time range")

    # regimes
    p_regimes = sub.add_parser("regimes", help="Show regime changes")
    p_regimes.add_argument("--last", default="7d", help="Time range")

    # pairs
    p_pairs = sub.add_parser("pairs", help="Per-pair performance")
    p_pairs.add_argument("--sort", choices=["pnl", "trades", "winrate"], default="pnl", help="Sort field")
    p_pairs.add_argument("--last", default="7d", help="Time range")

    # correlation
    p_corr = sub.add_parser("correlation", help="Correlate event types")
    p_corr.add_argument("type_a", help="First event type (e.g. whale_alerts)")
    p_corr.add_argument("type_b", help="Second event type (e.g. trades)")
    p_corr.add_argument("--last", default="30d", help="Time range")

    # leaderboard
    p_lb = sub.add_parser("leaderboard", help="Bot comparison leaderboard")
    p_lb.add_argument("--last", default="30d", help="Time range")

    # uptime
    p_up = sub.add_parser("uptime", help="Bot uptime history")
    p_up.add_argument("bot_id", help="Bot ID")
    p_up.add_argument("--last", default="7d", help="Time range")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "bot": cmd_bot,
        "trades": cmd_trades,
        "equity": cmd_equity,
        "regimes": cmd_regimes,
        "pairs": cmd_pairs,
        "correlation": cmd_correlation,
        "leaderboard": cmd_leaderboard,
        "uptime": cmd_uptime,
    }

    fn = commands.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
