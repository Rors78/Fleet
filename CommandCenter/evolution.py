#!/usr/bin/env python3
"""
EVOLUTION ENGINE — The fleet improves itself.
==============================================
Analyzes all bot performance, identifies what works and what doesn't,
and generates specific parameter change recommendations.

The evolution cycle:
  1. MEASURE  — performance metrics for every component
  2. ANALYZE  — statistical significance of patterns
  3. PROPOSE  — specific parameter changes
  4. ESTIMATE — heuristic impact estimate for proposals
  5. RECOMMEND — ranked list with confidence scores
  6. REPORT   — human-readable + machine-readable output

Usage:
    python evolution.py              # default 7 days
    python evolution.py --days 3     # last 3 days
"""

import argparse
import glob
import json
import os
import time
import urllib.request as urlreq
from collections import defaultdict
from datetime import datetime, timedelta, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
EVENT_DIR = os.path.join(LOG_DIR, "events")
DAILY_DIR = os.path.join(LOG_DIR, "daily")
ULTRON_DIR = os.path.join(LOG_DIR, "ultron")
EVOLUTION_DIR = os.path.join(LOG_DIR, "evolution")
CC_URL = "http://localhost:9000"

# Bot ports — mirrors BOTS in fleet_config.py.
# Keep in sync manually when adding/removing bots.
BOT_PORTS = {
    "turtlesue": 8070, "sentinel": 8071, "trinity": 8072, "hivemind": 8073,
    "nexusbrain": 8074, "oracle": 8075, "deepblue": 8076, "gridzilla": 8077,
    "phitex": 8078, "aegis": 8079, "nexus": 8082, "rubberband": 8083,
    "contrarian": 8084, "arbitrageur": 8085, "chronos": 8086, "confluence": 8088,
}


class EvolutionEngine:
    """Self-evolution engine for the trading fleet."""

    def __init__(self):
        self.measurements = {}
        self.proposals = []
        self.applied_history = []
        self._load_history()

    def _load_history(self):
        """Load history of previously applied changes."""
        hist_file = os.path.join(EVOLUTION_DIR, "applied_history.json")
        if os.path.exists(hist_file):
            try:
                with open(hist_file, "r") as f:
                    self.applied_history = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def _save_history(self):
        os.makedirs(EVOLUTION_DIR, exist_ok=True)
        with open(os.path.join(EVOLUTION_DIR, "applied_history.json"), "w") as f:
            json.dump(self.applied_history, f, indent=2, default=str)

    # --- STEP 1: MEASURE -----------------------------------------

    def measure(self, days=7):
        """Measure every measurable aspect of the fleet."""
        print(f"\n  [1/5] MEASURING (last {days} days)...")

        # 1a. Trade events from logs
        self._measure_trades(days)

        # 1b. Live bot snapshots
        self._measure_live_bots()

        # 1c. Portfolio utilization
        self._measure_portfolio()

        # 1d. Event bus health
        self._measure_bus_activity(days)

        # 1e. Timing patterns
        self._measure_timing(days)

        return self.measurements

    def _measure_trades(self, days):
        """Aggregate trade events from event logs."""
        bot_trades = defaultdict(lambda: {
            "opens": 0, "closes": 0, "wins": 0, "losses": 0,
            "total_pnl": 0, "pnls": [], "r_sum": 0, "r_count": 0,
            "exit_reasons": defaultdict(int), "pairs": defaultdict(int),
        })

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for f in sorted(glob.glob(os.path.join(EVENT_DIR, "*.jsonl"))):
            date_str = os.path.basename(f).replace(".jsonl", "")
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date.date() < cutoff.date():
                    continue
            except ValueError:
                continue

            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        bot = e.get("bot", e.get("source", "unknown"))
                        t = e.get("type", "")
                        data = e.get("data", {})

                        if t == "TRADE_OPEN":
                            bot_trades[bot]["opens"] += 1
                            pair = data.get("pair", e.get("pair", "?"))
                            bot_trades[bot]["pairs"][pair] += 1
                        elif t == "TRADE_CLOSE":
                            pnl = data.get("pnl", e.get("pnl", 0))
                            r = data.get("r", e.get("r", 0))
                            reason = data.get("exit_reason", data.get("reason", e.get("exit_reason", e.get("reason", "unknown"))))
                            bot_trades[bot]["closes"] += 1
                            bot_trades[bot]["total_pnl"] += pnl
                            bot_trades[bot]["pnls"].append(pnl)
                            if r:
                                bot_trades[bot]["r_sum"] += r
                                bot_trades[bot]["r_count"] += 1
                            bot_trades[bot]["exit_reasons"][reason] += 1
                            if pnl > 0:
                                bot_trades[bot]["wins"] += 1
                            else:
                                bot_trades[bot]["losses"] += 1
            except OSError:
                continue

        # Compute derived metrics
        for bot, stats in bot_trades.items():
            total = stats["wins"] + stats["losses"]
            stats["win_rate"] = (stats["wins"] / total * 100) if total > 0 else 0
            stats["avg_pnl"] = (sum(stats["pnls"]) / len(stats["pnls"])) if stats["pnls"] else 0
            stats["avg_r"] = (stats["r_sum"] / stats["r_count"]) if stats["r_count"] > 0 else 0

            # Profit factor (capped to avoid inf in JSON output)
            gross_wins = sum(p for p in stats["pnls"] if p > 0)
            gross_losses = abs(sum(p for p in stats["pnls"] if p < 0))
            if gross_losses > 0:
                stats["profit_factor"] = min(gross_wins / gross_losses, 99.0)
            else:
                stats["profit_factor"] = 99.0 if gross_wins > 0 else 0

            # Expectancy: avg_win × WR - avg_loss × (1-WR)
            wins_list = [p for p in stats["pnls"] if p > 0]
            losses_list = [p for p in stats["pnls"] if p <= 0]
            avg_win = sum(wins_list) / len(wins_list) if wins_list else 0
            avg_loss = abs(sum(losses_list) / len(losses_list)) if losses_list else 0
            wr = stats["win_rate"] / 100
            stats["expectancy"] = avg_win * wr - avg_loss * (1 - wr)

            # Serialize defaultdicts and drop intermediate data
            stats["exit_reasons"] = dict(stats["exit_reasons"])
            stats["pairs"] = dict(stats["pairs"])

        self.measurements["trades"] = dict(bot_trades)
        print(f"    Trades: {sum(s['closes'] for s in bot_trades.values())} closes across {len(bot_trades)} bots")

    def _measure_live_bots(self):
        """Query live bot snapshots."""
        live = {}
        for bot_id, port in BOT_PORTS.items():
            try:
                resp = urlreq.urlopen(f"http://localhost:{port}/api/snapshot", timeout=1)
                data = json.loads(resp.read())
                live[bot_id] = {
                    "status": data.get("status", "online"),
                    "equity": data.get("equity", data.get("paper_balance")),
                    "pnl": data.get("pnl", 0),
                    "total_trades": data.get("total_trades", 0),
                    "win_rate": data.get("win_rate", 0),
                    "open_positions": data.get("open_positions", data.get("open_spreads", 0)),
                }
            except Exception:
                live[bot_id] = {"status": "offline"}

        self.measurements["live_bots"] = live
        alive = sum(1 for v in live.values() if v["status"] != "offline")
        print(f"    Live bots: {alive}/{len(BOT_PORTS)} responding")

    def _measure_portfolio(self):
        """Check portfolio state."""
        try:
            resp = urlreq.urlopen(f"{CC_URL}/api/portfolio", timeout=2)
            data = json.loads(resp.read())
            # /api/portfolio returns PortfolioManager.state() which has
            # top-level "deployed", "available", "total" — not raw reservations
            deployed = data.get("deployed", 0)
            total = data.get("total", 10000)
            self.measurements["portfolio"] = {
                "total": total,
                "deployed": deployed,
                "utilization_pct": data.get("deployed_pct", deployed / max(total, 1) * 100),
                "active_reservations": data.get("active_reservations", 0),
            }
            print(f"    Portfolio: ${deployed:,.2f} deployed ({deployed/max(total,1)*100:.1f}%)")
        except Exception as e:
            self.measurements["portfolio"] = {"status": "offline"}
            print(f"    Portfolio: Command Center offline ({e})")

    def _measure_bus_activity(self, days):
        """Analyze event bus traffic patterns."""
        event_types = defaultdict(int)
        sources = defaultdict(int)
        total = 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for f in sorted(glob.glob(os.path.join(EVENT_DIR, "*.jsonl"))):
            date_str = os.path.basename(f).replace(".jsonl", "")
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date.date() < cutoff.date():
                    continue
            except ValueError:
                continue

            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            e = json.loads(line)
                            event_types[e.get("type", "?")] += 1
                            sources[e.get("source", e.get("bot", "?"))] += 1
                            total += 1
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

        self.measurements["bus"] = {
            "total_events": total,
            "event_types": dict(event_types),
            "active_sources": dict(sources),
            "events_per_day": total / max(days, 1),
        }
        print(f"    Bus: {total} events from {len(sources)} sources ({total/max(days,1):.0f}/day)")

    def _measure_timing(self, days):
        """Analyze which hours/days are profitable."""
        hours = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
        weekdays = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for f in sorted(glob.glob(os.path.join(EVENT_DIR, "*.jsonl"))):
            date_str = os.path.basename(f).replace(".jsonl", "")
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date.date() < cutoff.date():
                    continue
            except ValueError:
                continue

            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            e = json.loads(line)
                            if e.get("type") != "TRADE_CLOSE":
                                continue
                            data = e.get("data", {})
                            ts = e.get("ts", data.get("ts", 0))
                            pnl = data.get("pnl", e.get("pnl", 0))
                            if not ts:
                                continue
                            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                            h = dt.hour
                            wd = dt.weekday()
                            hours[h]["pnl"] += pnl
                            weekdays[wd]["pnl"] += pnl
                            if pnl > 0:
                                hours[h]["wins"] += 1
                                weekdays[wd]["wins"] += 1
                            else:
                                hours[h]["losses"] += 1
                                weekdays[wd]["losses"] += 1
                        except (json.JSONDecodeError, ValueError, OSError):
                            continue
            except OSError:
                continue

        self.measurements["timing"] = {
            "hours": {h: dict(s) for h, s in hours.items()},
            "weekdays": {d: dict(s) for d, s in weekdays.items()},
        }

        # Find best/worst hours
        best_h = max(hours.items(), key=lambda x: x[1]["pnl"], default=(None, {}))
        worst_h = min(hours.items(), key=lambda x: x[1]["pnl"], default=(None, {}))
        if best_h[0] is not None:
            print(f"    Timing: best hour={best_h[0]}:00 UTC (${best_h[1]['pnl']:+.2f}), "
                  f"worst hour={worst_h[0]}:00 UTC (${worst_h[1]['pnl']:+.2f})")

    # --- STEP 2: ANALYZE -----------------------------------------

    def analyze(self):
        """Find statistically significant patterns."""
        print(f"\n  [2/5] ANALYZING patterns...")

        patterns = []

        # Pattern: profitable vs losing bots
        trades = self.measurements.get("trades", {})
        for bot, stats in trades.items():
            total = stats.get("closes", 0)
            if total < 5:
                continue

            wr = stats.get("win_rate", 0)
            pf = stats.get("profit_factor", 0)
            exp = stats.get("expectancy", 0)
            pnl = stats.get("total_pnl", 0)

            if wr > 50 and pf > 1.2 and pnl > 0:
                patterns.append({
                    "type": "PROFITABLE_BOT",
                    "bot": bot,
                    "confidence": min(0.9, total / 50),
                    "data": {"wr": wr, "pf": pf, "expectancy": exp, "trades": total, "pnl": pnl},
                    "suggestion": f"{bot}: {wr:.0f}% WR, PF={pf:.2f}, +${pnl:.2f} — increase allocation",
                })
            elif total >= 10 and wr < 30 and pnl < 0:
                patterns.append({
                    "type": "LOSING_BOT",
                    "bot": bot,
                    "confidence": min(0.9, total / 30),
                    "data": {"wr": wr, "pf": pf, "expectancy": exp, "trades": total, "pnl": pnl},
                    "suggestion": f"{bot}: {wr:.0f}% WR, PF={pf:.2f}, ${pnl:.2f} — review strategy",
                })

        # Pattern: exit reason effectiveness
        for bot, stats in trades.items():
            reasons = stats.get("exit_reasons", {})
            total_closes = stats.get("closes", 0)
            if total_closes < 10:
                continue
            for reason, count in reasons.items():
                pct = count / total_closes * 100
                if pct > 50:
                    patterns.append({
                        "type": "DOMINANT_EXIT",
                        "bot": bot,
                        "confidence": 0.6,
                        "data": {"reason": reason, "pct": pct, "count": count},
                        "suggestion": f"{bot}: {pct:.0f}% exits by {reason} — may need adjustment",
                    })

        # Pattern: underutilized capital
        portfolio = self.measurements.get("portfolio", {})
        util = portfolio.get("utilization_pct", 0)
        if util < 10 and portfolio.get("status") != "offline":
            patterns.append({
                "type": "IDLE_CAPITAL",
                "confidence": 0.7,
                "data": {"utilization_pct": util},
                "suggestion": f"Only {util:.1f}% capital deployed — bots may be too conservative",
            })

        # Pattern: timing edges
        timing = self.measurements.get("timing", {})
        for h, stats in timing.get("hours", {}).items():
            total = stats.get("wins", 0) + stats.get("losses", 0)
            if total < 5:
                continue
            wr = stats["wins"] / total * 100
            if wr > 65:
                patterns.append({
                    "type": "PROFITABLE_HOUR",
                    "confidence": min(0.7, total / 20),
                    "data": {"hour": h, "wr": wr, "pnl": stats["pnl"], "trades": total},
                    "suggestion": f"Hour {h}:00 UTC: {wr:.0f}% WR over {total} trades — increase sizing",
                })
            elif wr < 25 and total >= 8:
                patterns.append({
                    "type": "LOSING_HOUR",
                    "confidence": min(0.7, total / 20),
                    "data": {"hour": h, "wr": wr, "pnl": stats["pnl"], "trades": total},
                    "suggestion": f"Hour {h}:00 UTC: {wr:.0f}% WR over {total} trades — reduce or avoid",
                })

        print(f"    Found {len(patterns)} patterns")
        return patterns

    # --- STEP 3: PROPOSE ------------------------------------------

    def propose(self, patterns):
        """Generate specific parameter change proposals."""
        print(f"\n  [3/5] GENERATING proposals...")

        proposals = []

        for p in patterns:
            ptype = p["type"]

            if ptype == "PROFITABLE_BOT":
                proposals.append({
                    "target": p["bot"],
                    "parameter": "max_per_bot_pct",
                    "direction": "INCREASE",
                    "current": "30%",
                    "proposed": "35%",
                    "rationale": p["suggestion"],
                    "confidence": p["confidence"],
                    "risk": "LOW",
                    "category": "allocation",
                })

            elif ptype == "LOSING_BOT":
                proposals.append({
                    "target": p["bot"],
                    "parameter": "max_per_bot_pct",
                    "direction": "DECREASE",
                    "current": "30%",
                    "proposed": "20%",
                    "rationale": p["suggestion"],
                    "confidence": p["confidence"],
                    "risk": "LOW",
                    "category": "allocation",
                })

            elif ptype == "IDLE_CAPITAL":
                proposals.append({
                    "target": "fleet",
                    "parameter": "trade_size_pct",
                    "direction": "INCREASE",
                    "current": "3%",
                    "proposed": "4%",
                    "rationale": p["suggestion"],
                    "confidence": p["confidence"],
                    "risk": "MEDIUM",
                    "category": "sizing",
                })

            elif ptype == "LOSING_HOUR":
                proposals.append({
                    "target": "fleet",
                    "parameter": f"avoid_hour_{p['data']['hour']}",
                    "direction": "ADD_FILTER",
                    "current": "none",
                    "proposed": f"reduce size 50% at hour {p['data']['hour']} UTC",
                    "rationale": p["suggestion"],
                    "confidence": p["confidence"],
                    "risk": "LOW",
                    "category": "timing",
                })

        print(f"    Generated {len(proposals)} proposals")
        return proposals

    # --- STEP 4: ESTIMATE -----------------------------------------

    def estimate_impact(self, proposals):
        """Heuristic impact estimate for proposed changes.

        This is NOT a backtest — it assigns qualitative impact labels based
        on the proposal category and direction. A proper simulation would
        require replay infrastructure that doesn't exist yet.
        """
        print(f"\n  [4/5] ESTIMATING impact...")

        for proposal in proposals:
            cat = proposal.get("category", "")
            direction = proposal.get("direction", "")

            if cat == "allocation" and direction == "INCREASE":
                proposal["estimated_impact"] = "POSITIVE"
                proposal["impact_confidence"] = proposal["confidence"] * 0.8
                proposal["estimated_pnl_change"] = "+5-15%"
            elif cat == "allocation" and direction == "DECREASE":
                proposal["estimated_impact"] = "PROTECTIVE"
                proposal["impact_confidence"] = proposal["confidence"] * 0.9
                proposal["estimated_pnl_change"] = "Reduced drawdown"
            elif cat == "signal":
                proposal["estimated_impact"] = "POSITIVE" if direction == "INCREASE" else "PROTECTIVE"
                proposal["impact_confidence"] = proposal["confidence"] * 0.7
                proposal["estimated_pnl_change"] = "Improved signal quality"
            else:
                proposal["estimated_impact"] = "UNKNOWN"
                proposal["impact_confidence"] = 0.3
                proposal["estimated_pnl_change"] = "Uncertain"

        print(f"    Estimated {len(proposals)} proposals")
        return proposals

    # --- STEP 5: RECOMMEND ----------------------------------------

    def recommend(self, proposals):
        """Rank by confidence × impact."""
        print(f"\n  [5/5] RANKING recommendations...\n")

        ranked = sorted(proposals,
                        key=lambda x: x.get("impact_confidence", 0) * (1 if x.get("risk") == "LOW" else 0.7),
                        reverse=True)
        return ranked

    # --- FULL CYCLE -----------------------------------------------

    def run(self, days=7):
        """Execute complete evolution cycle."""
        print(f"\n  {'=' * 50}")
        print(f"  EVOLUTION ENGINE v1.0 — {days}-day analysis")
        print(f"  {'=' * 50}")

        self.measure(days)
        patterns = self.analyze()
        proposals = self.propose(patterns)
        estimated = self.estimate_impact(proposals)
        recommendations = self.recommend(estimated)

        # Report
        self._print_report(recommendations, patterns)
        self._save_report(recommendations, patterns, days)

        return recommendations

    def _print_report(self, recommendations, patterns):
        """Human-readable report."""
        print(f"\n  {'-' * 50}")
        print(f"  EVOLUTION REPORT")
        print(f"  {'-' * 50}")

        if not recommendations:
            print("\n    No recommendations at this time.")
            print("    (Need more trade data for statistical significance)")
            return

        for i, rec in enumerate(recommendations, 1):
            risk_icon = {"LOW": "+", "MEDIUM": "!", "HIGH": "X"}.get(rec.get("risk", "?"), "?")
            print(f"\n  {i}. [{risk_icon}] {rec['target']}: {rec['parameter']}")
            print(f"     {rec.get('current', '?')} -> {rec.get('proposed', '?')}")
            print(f"     Rationale: {rec.get('rationale', '')}")
            print(f"     Confidence: {rec.get('confidence', 0):.0%} | "
                  f"Impact: {rec.get('estimated_impact', '?')} | "
                  f"Risk: {rec.get('risk', '?')}")

        # Trade summary by bot
        trades = self.measurements.get("trades", {})
        if trades:
            print(f"\n  {'-' * 50}")
            print(f"  TRADE SUMMARY")
            print(f"  {'-' * 50}")
            print(f"\n  {'Bot':15s}  {'Trades':>6s}  {'WR':>6s}  {'PF':>6s}  {'Exp':>7s}  {'PnL':>10s}")
            print(f"  {'-' * 55}")
            for bot in sorted(trades.keys()):
                s = trades[bot]
                if s["closes"] == 0:
                    continue
                print(f"  {bot:15s}  {s['closes']:6d}  {s['win_rate']:5.1f}%  "
                      f"{s['profit_factor']:5.2f}  ${s['expectancy']:6.2f}  "
                      f"${s['total_pnl']:9.2f}")

        print(f"\n  {'=' * 50}\n")

    def _save_report(self, recommendations, patterns, days):
        """Save machine-readable report."""
        os.makedirs(EVOLUTION_DIR, exist_ok=True)
        report = {
            "timestamp": time.time(),
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "period_days": days,
            "measurements_summary": {
                "bots_measured": len(self.measurements.get("trades", {})),
                "total_trades": sum(s.get("closes", 0)
                                    for s in self.measurements.get("trades", {}).values()),
                "portfolio": self.measurements.get("portfolio", {}),
                "bus_events": self.measurements.get("bus", {}).get("total_events", 0),
            },
            "patterns": patterns,
            "recommendations": recommendations,
        }

        out_file = os.path.join(EVOLUTION_DIR,
                                f"{datetime.now(timezone.utc):%Y-%m-%d}.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"  Saved to: {out_file}")


def main():
    parser = argparse.ArgumentParser(description="Fleet Evolution Engine")
    parser.add_argument("--days", type=int, default=7, help="Days to analyze")
    args = parser.parse_args()

    engine = EvolutionEngine()
    engine.run(args.days)


if __name__ == "__main__":
    main()
