#!/usr/bin/env python3
"""
WEEKLY FLEET ANALYSIS
======================
Reads the last 7 days of fleet logs, sends them to the AI inference
server for post-mortem and assessment, prints results.

Usage:
    python weekly_analysis.py           # last 7 days
    python weekly_analysis.py --days 3  # last 3 days
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
EVENT_DIR = os.path.join(LOG_DIR, "events")
JOURNAL_DIR = os.path.join(LOG_DIR, "journals")
WEEKLY_DIR = os.path.join(LOG_DIR, "weekly")
INFERENCE_URL = "http://localhost:9001"
CC_URL = "http://localhost:9000"


def date_range(days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    dates = []
    current = start.date()
    while current <= end.date():
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def read_events(dates):
    events = []
    for d in dates:
        path = os.path.join(EVENT_DIR, f"{d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return events


def read_journals(dates):
    entries = []
    for d in dates:
        path = os.path.join(JOURNAL_DIR, f"{d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return entries


def fetch_fleet_state():
    try:
        resp = urlopen(Request(f"{CC_URL}/api/master"), timeout=5)
        master = json.loads(resp.read())
    except Exception:
        master = {"bots": []}

    try:
        resp = urlopen(Request(f"{CC_URL}/api/portfolio"), timeout=5)
        portfolio = json.loads(resp.read())
    except Exception:
        portfolio = {}

    # Get AEGIS and PHITEX scores
    aegis_score = None
    aegis_regime = None
    phitex_score = None
    for b in master.get("bots", []):
        if b.get("id") == "aegis" and b.get("alive"):
            n = b.get("normalized", {}) or {}
            aegis_score = n.get("aegis_score")
            aegis_regime = n.get("regime")
        if b.get("id") == "phitex" and b.get("alive"):
            n = b.get("normalized", {}) or {}
            phitex_score = n.get("fleet_score")

    return {
        "bots": master.get("bots", []),
        "portfolio": portfolio,
        "aegis_score": aegis_score,
        "aegis_regime": aegis_regime,
        "phitex_score": phitex_score,
        "whale_count": next((
            (b.get("normalized") or {}).get("whale_count", 0)
            for b in master.get("bots", [])
            if b.get("id") == "deepblue" and b.get("alive")
        ), 0),
    }


def query_inference(endpoint, payload, timeout=180):
    try:
        req = Request(f"{INFERENCE_URL}{endpoint}",
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Weekly Fleet Analysis")
    parser.add_argument("--days", type=int, default=7, help="Days to analyze")
    args = parser.parse_args()

    dates = date_range(args.days)
    print(f"\n  === WEEKLY FLEET ANALYSIS === ")
    print(f"  Period: {dates[0]} to {dates[-1]} ({args.days} days)")
    print()

    # Load events
    events = read_events(dates)
    trades = [e for e in events if e.get("type") in ("TRADE_OPEN", "TRADE_CLOSE")]
    trade_closes = [e for e in events if e.get("type") == "TRADE_CLOSE"]
    print(f"  Events loaded: {len(events)} total, {len(trades)} trade events, {len(trade_closes)} closes")

    # Load journals
    journals = read_journals(dates)
    print(f"  AI journals: {len(journals)} entries")

    # Quick stats
    total_pnl = sum(t.get("pnl", 0) for t in trade_closes)
    wins = sum(1 for t in trade_closes if t.get("pnl", 0) > 0)
    losses = len(trade_closes) - wins
    wr = (wins / len(trade_closes) * 100) if trade_closes else 0

    print(f"\n  TRADE SUMMARY:")
    print(f"    Trades closed: {len(trade_closes)}")
    print(f"    Wins/Losses:   {wins}W / {losses}L ({wr:.0f}% WR)")
    print(f"    Total PnL:     ${total_pnl:+,.2f}")

    # Fetch current state
    # Brainiac market context
    print(f"\n  Fetching Brainiac market data...")
    brainiac = {}
    for endpoint in ["correlations", "funding", "metrics"]:
        try:
            resp = urlopen(Request(f"http://localhost:9000/api/brainiac/{endpoint}"), timeout=5)
            data = json.loads(resp.read())
            if data and data.get("data"):
                brainiac[endpoint] = data["data"]
                if endpoint == "correlations":
                    print(f"    Correlations: avg_abs={data['data'].get('avg_abs_correlation', '?')}")
                elif endpoint == "funding":
                    print(f"    Funding: {data['data'].get('pairs', 0)} pairs, avg={data['data'].get('avg_rate', '?')}")
                elif endpoint == "metrics":
                    print(f"    BTC dominance: {data['data'].get('btc_dominance', '?')}%")
        except Exception:
            pass

    print(f"\n  Fetching current fleet state...")
    fleet_state = fetch_fleet_state()
    fleet_state["brainiac"] = brainiac

    # AI Post-Mortem
    if trade_closes:
        print(f"\n  Running AI post-mortem on {min(len(trade_closes), 30)} trades...")
        # Format trades for AI
        formatted = []
        for t in trade_closes[-30:]:
            formatted.append({
                "symbol": t.get("pair", "?"),
                "direction": t.get("direction", "LONG"),
                "pnl": t.get("pnl", 0),
                "r": t.get("r", 0),
                "exit_reason": t.get("exit_reason", "unknown"),
                "regime": t.get("regime", "?"),
                "factors": t.get("factors", {}),
            })
        postmortem = query_inference("/api/ai/post-mortem", {"trades": formatted})

        if "error" not in postmortem:
            print(f"\n  AI POST-MORTEM:")
            for k in ("winning_patterns", "losing_patterns", "suggestions"):
                items = postmortem.get(k, [])
                if items:
                    print(f"    {k.replace('_', ' ').title()}:")
                    for item in items:
                        print(f"      - {item}")
            summary = postmortem.get("summary", "")
            if summary:
                print(f"    Summary: {summary}")
        else:
            print(f"  AI Post-mortem error: {postmortem['error']}")
    else:
        print(f"\n  No trades to analyze.")
        postmortem = {}

    # AI Fleet Assessment
    print(f"\n  Running AI fleet assessment...")
    assessment = query_inference("/api/ai/fleet-assessment", {"fleet_state": fleet_state})

    if "error" not in assessment:
        print(f"\n  AI FLEET ASSESSMENT:")
        if assessment.get("assessment"):
            print(f"    Assessment: {assessment['assessment']}")
        for k in ("risk_factors", "opportunities"):
            items = assessment.get(k, [])
            if items:
                print(f"    {k.replace('_', ' ').title()}:")
                for item in items:
                    print(f"      - {item}")
        if assessment.get("recommended_action"):
            print(f"    Recommendation: {assessment['recommended_action']}")
        if assessment.get("aegis_appropriate") is not None:
            print(f"    AEGIS appropriate: {'Yes' if assessment['aegis_appropriate'] else 'No'}")
    else:
        print(f"  AI Assessment error: {assessment['error']}")

    # Run Ultron self-analysis
    print(f"\n  Running Ultron self-analysis...")
    ultron_results = {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from ultron import UltronAnalyzer
        ultron = UltronAnalyzer()
        ultron_results = ultron.run(args.days)
    except Exception as e:
        print(f"  Ultron error: {e}")

    # Save to weekly log
    os.makedirs(WEEKLY_DIR, exist_ok=True)
    report = {
        "date": dates[-1],
        "period_days": args.days,
        "trade_summary": {
            "total_closes": len(trade_closes),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wr, 1),
            "total_pnl": round(total_pnl, 2),
        },
        "ai_postmortem": postmortem,
        "ai_assessment": assessment,
        "ultron": ultron_results,
        "brainiac": brainiac,
        "fleet_state_snapshot": {
            "aegis_score": fleet_state.get("aegis_score"),
            "aegis_regime": fleet_state.get("aegis_regime"),
            "phitex_score": fleet_state.get("phitex_score"),
        },
        "journals_count": len(journals),
    }
    report_path = os.path.join(WEEKLY_DIR, f"{dates[-1]}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Saved to: {report_path}")
    print()


if __name__ == "__main__":
    main()
