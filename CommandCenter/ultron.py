#!/usr/bin/env python3
"""
ULTRON — Self-Evolution Engine
================================
Analyzes the fleet's OWN performance, finds systematic weaknesses,
simulates parameter changes, and generates evolution plans.

Usage:
    python ultron.py           # analyze last 7 days
    python ultron.py --days 3  # analyze last 3 days
"""

import argparse
import glob
import json
import os
import time
from datetime import datetime, timezone

import requests
from win_rate_scale import to_percent

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
CC_URL = "http://localhost:9000"
AI_URL = "http://localhost:9001"


class UltronAnalyzer:
    def __init__(self):
        self.findings = []
        self.recommendations = []

    def run(self, days=7):
        print("\n  ULTRON SELF-ANALYSIS v1.0")
        print(f"  Period: last {days} days\n")

        steps = [
            ("Regime stability",     self._regime_stability),
            ("Portfolio efficiency",  self._portfolio_efficiency),
            ("Bot utilization",      self._bot_utilization),
            ("Timing patterns",      self._timing_patterns),
        ]
        for i, (label, method) in enumerate(steps, 1):
            print(f"  [{i}/{len(steps)}] {label}...")
            method(days)

        self._print_findings()
        self._ai_synthesis()

        return {"findings": self.findings, "recommendations": self.recommendations}

    # ------------------------------------------------------------------
    # Analysis methods — each accepts `days` for uniformity
    # ------------------------------------------------------------------

    def _regime_stability(self, days):
        """How stable are regime classifications? Flags noisy bots."""
        events_dir = os.path.join(LOG_DIR, "events")
        rc = 0
        bot_counts = {}
        for f in sorted(glob.glob(os.path.join(events_dir, "*.jsonl")))[-days:]:
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if e.get("type") == "REGIME_CHANGE":
                            rc += 1
                            bot = e.get("bot", e.get("source", "?"))
                            bot_counts[bot] = bot_counts.get(bot, 0) + 1
            except OSError:
                continue
        if rc:
            self.findings.append({"cat": "regime", "msg": f"{rc} regime changes in {days}d", "sev": "info"})
        for bot, c in bot_counts.items():
            if c > days * 3:
                self.findings.append({"cat": "regime", "msg": f"{bot} changed regime {c}x in {days}d — noisy", "sev": "warning"})

    def _portfolio_efficiency(self, days):
        """Is capital used well?"""
        try:
            p = requests.get(f"{CC_URL}/api/portfolio", timeout=5).json()
            util = p.get("deployed", 0) / max(p.get("total", 1), 1)
            max_pct = p.get("limits", {}).get("max_deployed_pct", 80)
            if util < 0.1:
                self.findings.append({"cat": "portfolio", "msg": f"Only {util:.0%} deployed — capital idle", "sev": "warning"})
            self.findings.append({"cat": "portfolio", "msg": f"Deployed {util:.0%} of {max_pct}% limit", "sev": "info"})
        except Exception as e:
            print(f"    Could not reach Command Center: {e}")

    def _bot_utilization(self, days):
        """Which bots contribute vs dead weight?"""
        try:
            master = requests.get(f"{CC_URL}/api/master", timeout=5).json()
            for b in master.get("bots", []):
                n = b.get("normalized", {}) or {}
                name = b.get("name", "?")
                if not b.get("alive"):
                    self.findings.append({"cat": "bots", "msg": f"{name} OFFLINE", "sev": "warning"})
                    continue
                trades = n.get("total_trades", 0)
                wr = n.get("win_rate")
                if trades and trades > 20 and wr is not None:
                    # Normalize: some bots report 0-1, others 0-100
                    # win_rate_scale: the ONE scale rule (2026-08-26).
                    wr_pct = to_percent(wr)
                    if wr_pct is None:
                        continue
                    if wr_pct < 25:
                        self.findings.append({"cat": "bots", "msg": f"{name}: {trades} trades at {wr_pct:.0f}% WR — losing", "sev": "critical"})
        except Exception as e:
            print(f"    Could not reach Command Center: {e}")

    def _timing_patterns(self, days):
        """When does the fleet win/lose?"""
        hours = {h: {"w": 0, "l": 0} for h in range(24)}
        events_dir = os.path.join(LOG_DIR, "events")
        for f in sorted(glob.glob(os.path.join(events_dir, "*.jsonl")))[-days:]:
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if e.get("type") == "TRADE_CLOSE":
                            # Timestamp and PnL may be at top level or nested in data
                            data = e.get("data", {})
                            ts = e.get("ts", data.get("ts", 0))
                            pnl = data.get("pnl", e.get("pnl", 0))
                            if not ts:
                                continue
                            h = datetime.fromtimestamp(ts, tz=timezone.utc).hour
                            if pnl > 0:
                                hours[h]["w"] += 1
                            else:
                                hours[h]["l"] += 1
            except OSError:
                continue
        for h, s in hours.items():
            total = s["w"] + s["l"]
            if total >= 5:
                wr = s["w"] / total
                if wr > 0.6:
                    self.findings.append({"cat": "timing", "msg": f"Hour {h}:00 UTC: {wr:.0%} WR ({total} trades) — profitable", "sev": "positive"})
                elif wr < 0.2:
                    self.findings.append({"cat": "timing", "msg": f"Hour {h}:00 UTC: {wr:.0%} WR ({total} trades) — losing window", "sev": "warning"})

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _print_findings(self):
        """Print findings and recommendations to console."""
        print("\n  FINDINGS:")
        for f in self.findings:
            icon = {"positive": "+", "warning": "!", "critical": "X", "info": "-"}.get(f["sev"], "?")
            print(f"    [{icon}] {f['msg']}")

        if self.recommendations:
            print("\n  RECOMMENDATIONS:")
            for r in self.recommendations:
                print(f"    [{r['target']}] {r['action']} (conf={r.get('conf', '?')})")

    def _ai_synthesis(self):
        """Feed findings to AI for synthesis. Failure does not block output."""
        print("\n  Generating evolution plan...")
        try:
            resp = requests.post(f"{AI_URL}/api/ai/fleet-assessment",
                                 json={"fleet_state": {"findings": self.findings, "recommendations": self.recommendations}},
                                 timeout=120)
            if resp.status_code == 200:
                plan = resp.json()
                print("\n  AI EVOLUTION PLAN:")
                for k, v in plan.items():
                    if isinstance(v, dict):
                        print(f"    {k}:")
                        for k2, v2 in v.items():
                            print(f"      {k2}: {v2}")
                    elif isinstance(v, list):
                        print(f"    {k}:")
                        for item in v:
                            print(f"      - {item}")
                    else:
                        print(f"    {k}: {v}")

                # Save
                out_dir = os.path.join(LOG_DIR, "ultron")
                os.makedirs(out_dir, exist_ok=True)
                out_file = os.path.join(out_dir, f"{datetime.now(timezone.utc):%Y-%m-%d}.json")
                with open(out_file, "w") as fh:
                    json.dump({"findings": self.findings, "recommendations": self.recommendations,
                               "ai_plan": plan, "timestamp": time.time()}, fh, indent=2, default=str)
                print(f"\n  Saved to {out_file}")
            else:
                print(f"\n  AI returned status {resp.status_code}")
        except Exception as e:
            print(f"\n  AI synthesis unavailable: {e}")
        print()


def main():
    parser = argparse.ArgumentParser(description="ULTRON Self-Evolution Engine")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    UltronAnalyzer().run(args.days)


if __name__ == "__main__":
    main()
