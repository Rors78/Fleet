"""
DENIAL COST — Portfolio Denial Opportunity Cost

Reads PORTFOLIO_RESERVE_DENIED events from logs/events/*.jsonl.
For each denied trade, looks up the price N minutes later
(using fleet's average hold time) and computes what the trade
would have netted after fees.

Answers: "Is the risk system protecting us or strangling us?"

Usage:
    python denial_cost.py
    python denial_cost.py --days 3
    python denial_cost.py --hold-minutes 240
"""

import json
import os
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

CC_URL = "http://localhost:9000"
EVENTS_DIR = os.path.join(os.path.dirname(__file__), "logs", "events")
FEE_RATE = 0.0026  # Kraken taker 0.26% each side = 0.52% round trip
DEFAULT_HOLD_MINUTES = 240  # fleet avg ~3-4 hours
DEFAULT_DAYS = 3


def _parse_args():
    p = argparse.ArgumentParser(description="Denial cost analysis")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--hold-minutes", type=int, default=DEFAULT_HOLD_MINUTES,
                   help="Simulated hold time in minutes (default: fleet avg 240)")
    return p.parse_args()


def _load_denial_events(days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    denials = []
    if not os.path.exists(EVENTS_DIR):
        return denials

    for fname in sorted(os.listdir(EVENTS_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(EVENTS_DIR, fname)
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                # Match denial events — type or event field
                ev_type = ev.get("type", "") or ev.get("event", "")
                if "DENIED" not in ev_type and "denial" not in str(ev_type).lower():
                    continue
                ts_raw = ev.get("ts") or ev.get("timestamp")
                try:
                    ts = datetime.fromtimestamp(
                        ts_raw / 1000 if ts_raw > 1e12 else ts_raw,
                        tz=timezone.utc
                    )
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                data = ev.get("data", ev)
                pair = data.get("pair") or data.get("symbol")
                direction = data.get("direction", "long")
                amount = data.get("amount", 0)
                reason = data.get("reason", "unknown")
                bot = data.get("bot_id") or data.get("bot") or ev.get("source", "unknown")
                # Skip config errors (arbitrageur not a trading bot)
                if "not a trading bot" in str(reason).lower():
                    continue
                if pair and amount:
                    denials.append({
                        "ts": ts, "pair": pair, "direction": direction,
                        "amount": float(amount), "reason": reason, "bot": bot,
                    })
    return denials


def _fetch_ohlc(pair, ts_start, hold_minutes):
    """Fetch OHLC around the denial time and return entry + exit prices."""
    import urllib.request
    import urllib.parse

    # Use 60-min candles; fetch enough to cover the hold period
    limit = max(hold_minutes // 60 + 2, 5)
    since = int(ts_start.timestamp())
    params = urllib.parse.urlencode({
        "pair": pair,
        "interval": 60,
        "limit": limit,
        "since": since,
    })
    try:
        url = f"{CC_URL}/api/market/ohlc?{params}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        candles = data.get("candles") or data.get("ohlc") or data.get("data") or []
        if len(candles) < 2:
            return None, None
        entry_price = float(candles[0][4])  # close of first candle
        exit_idx = min(hold_minutes // 60, len(candles) - 1)
        exit_price = float(candles[exit_idx][4])
        return entry_price, exit_price
    except Exception:
        return None, None


def _simulate_pnl(direction, entry, exit_price, amount_usd):
    """Compute net PnL after round-trip fees."""
    if not entry or entry == 0:
        return None
    size = amount_usd / entry
    if direction.lower() in ("long", "buy"):
        gross = (exit_price - entry) * size
    else:
        gross = (entry - exit_price) * size
    fees = amount_usd * FEE_RATE * 2  # entry + exit
    return gross - fees


def main():
    args = _parse_args()
    print(f"Loading denial events from last {args.days} days...")
    denials = _load_denial_events(args.days)

    if not denials:
        print("No denial events found. Either no denials occurred or event logs are empty.")
        print(f"Expected events with type containing 'DENIED' in {EVENTS_DIR}")
        sys.exit(0)

    print(f"Found {len(denials)} denials (excluding config errors). Simulating outcomes...\n")

    results = []
    fetch_errors = 0
    for d in denials:
        entry, exit_p = _fetch_ohlc(d["pair"], d["ts"], args.hold_minutes)
        if entry is None:
            fetch_errors += 1
            continue
        pnl = _simulate_pnl(d["direction"], entry, exit_p, d["amount"])
        if pnl is None:
            continue
        results.append({**d, "entry": entry, "exit": exit_p, "sim_pnl": pnl, "won": pnl > 0})

    if fetch_errors:
        print(f"  ({fetch_errors} denials skipped — price data unavailable)\n")

    if not results:
        print("No simulated outcomes. CC may be offline or pairs not in universe.")
        sys.exit(0)

    # Aggregate by reason
    by_reason = defaultdict(lambda: {"count": 0, "sim_pnl": 0.0, "wins": 0, "capital": 0.0})
    for r in results:
        b = by_reason[r["reason"]]
        b["count"] += 1
        b["sim_pnl"] += r["sim_pnl"]
        b["wins"] += int(r["won"])
        b["capital"] += r["amount"]

    # Aggregate by bot
    by_bot = defaultdict(lambda: {"count": 0, "sim_pnl": 0.0, "wins": 0})
    for r in results:
        b = by_bot[r["bot"]]
        b["count"] += 1
        b["sim_pnl"] += r["sim_pnl"]
        b["wins"] += int(r["won"])

    total_pnl = sum(r["sim_pnl"] for r in results)
    total_wins = sum(int(r["won"]) for r in results)
    total_n = len(results)

    print(f"=== DENIAL OPPORTUNITY COST ({args.days}d, {args.hold_minutes}min hold simulation) ===\n")
    print(f"Total denials simulated: {total_n}")
    print(f"Simulated win rate:      {total_wins/total_n*100:.1f}%")
    print(f"Simulated total PnL:     ${total_pnl:+.2f}")
    print(f"Simulated PnL/denial:    ${total_pnl/total_n:+.2f}")

    if total_pnl > 0:
        print(f"\n>>> RISK SYSTEM IS COSTING MONEY: denied trades would have earned ${total_pnl:+.2f}")
        print("    Consider loosening constraints or investigating which reasons block the most edge.")
    elif total_pnl < 0:
        print(f"\n>>> RISK SYSTEM IS SAVING MONEY: denied trades would have lost ${total_pnl:.2f}")
        print("    Current limits are doing their job.")
    else:
        print("\n    Neutral — denied trades approximately break even.")

    print("\n=== BY DENIAL REASON ===\n")
    sorted_reasons = sorted(by_reason.items(), key=lambda x: x[1]["sim_pnl"])
    print(f"{'Reason':<40} {'N':>5} {'WR%':>6} {'SimPnL':>10} {'Avg':>8}")
    print("-" * 72)
    for reason, b in sorted_reasons:
        wr = b["wins"] / b["count"] * 100 if b["count"] else 0
        avg = b["sim_pnl"] / b["count"] if b["count"] else 0
        print(f"{reason[:40]:<40} {b['count']:>5} {wr:>5.1f}% {b['sim_pnl']:>+10.2f} {avg:>+8.2f}")

    print("\n=== BY BOT ===\n")
    sorted_bots = sorted(by_bot.items(), key=lambda x: x[1]["sim_pnl"])
    print(f"{'Bot':<20} {'N':>5} {'WR%':>6} {'SimPnL':>10} {'Avg':>8}")
    print("-" * 50)
    for bot, b in sorted_bots:
        wr = b["wins"] / b["count"] * 100 if b["count"] else 0
        avg = b["sim_pnl"] / b["count"] if b["count"] else 0
        print(f"{bot:<20} {b['count']:>5} {wr:>5.1f}% {b['sim_pnl']:>+10.2f} {avg:>+8.2f}")


if __name__ == "__main__":
    main()
