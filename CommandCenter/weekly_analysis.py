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

import re as _re

# Probe pairs look like NF138587OK/USD — a marker prefix plus a timestamp.
_NF_PROBE = _re.compile(r"^NF\d+")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
# See the note in evolution.py: `logs/events` is legacy and contains no
# TRADE_CLOSE at all. Reading it made every weekly report say "0 closes" and
# "No trades to analyze" while the bus held 77 of them.
EVENT_DIR = os.path.join(LOG_DIR, "event_bus")
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


def _read_jsonl(directory, dates):
    entries = []
    for d in dates:
        path = os.path.join(directory, f"{d}.jsonl")
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


def read_events(dates):
    return _read_jsonl(EVENT_DIR, dates)


def read_journals(dates):
    return _read_jsonl(JOURNAL_DIR, dates)


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

    # Quick stats.
    # P/L lives at data.pnl on a bus event, NOT at the top level. Reading
    # `t.get("pnl", 0)` on the outer envelope returned None -> 0 for every
    # close, so 77 trades reported as 0W/77L totalling exactly $+0.00 --
    # incoherent on its face, and the giveaway that the field was never being
    # read. An unmeasured P/L is also NOT a loss: `losses = total - wins`
    # silently counted every unpriced close against the fleet.
    def _pnl(t):
        d = t.get("data") or {}
        v = d.get("pnl", t.get("pnl"))
        return v if isinstance(v, (int, float)) else None

    def _is_probe(t):
        """Synthetic closes created by test harnesses exercising the live path.

        Nothing in the payload marks these, so the pair name is the only
        signal — every probe uses a marker prefix no real market carries.
        They are NOT a rounding error: on 2026-08-07, 55 of 79 closes were
        probes contributing 55W/0L and +$678.70, which flipped the weekly
        headline from a real -$119.27 to a reported +$559.43. A report whose
        sign depends on test data is worse than no report.
        """
        pair = str(((t.get("data") or {}).get("pair")) or "").upper()
        return pair.startswith(("ZZPROBE", "ZZ", "NFNOK")) or _NF_PROBE.match(pair)

    _probes = [t for t in trade_closes if _is_probe(t)]
    trade_closes = [t for t in trade_closes if not _is_probe(t)]

    _pnls = [_pnl(t) for t in trade_closes]
    _decided = [p for p in _pnls if p is not None]
    _unpriced = len(_pnls) - len(_decided)

    total_pnl = sum(_decided)
    wins = sum(1 for p in _decided if p > 0)
    losses = sum(1 for p in _decided if p < 0)
    # A close with a P/L of exactly 0.0 is neither a win nor a loss — it is a
    # capital movement (a grid teardown, a cancelled entry, a re-reservation).
    # Counting it in the denominator deflates the win rate: 59W/1L over the
    # full 77 closes reads 77%, but the true decided rate over 60 is 98%.
    _flat = sum(1 for p in _decided if p == 0)
    _resolved = wins + losses
    wr = (wins / _resolved * 100) if _resolved else None

    print(f"\n  TRADE SUMMARY:")
    print(f"    Trades closed: {len(trade_closes)}")
    if _probes:
        _ppnl = sum(p for p in (_pnl(t) for t in _probes)
                    if isinstance(p, (int, float)))
        print(f"    Excluded:      {len(_probes)} synthetic probe close(s) "
              f"worth ${_ppnl:+,.2f} — test-harness rows, not fleet results")
    _wr_s = f"{wr:.0f}% WR" if wr is not None else "WR n/a"
    print(f"    Wins/Losses:   {wins}W / {losses}L ({_wr_s}, n={_resolved})")
    if _flat:
        print(f"    Flat:          {_flat} close(s) at exactly $0.00 "
              f"— capital movements, excluded from the win rate")
    if _unpriced:
        # Disclosed, not folded into losses. A quiet count is how an
        # unmeasured close becomes a fabricated loss.
        print(f"    Unpriced:      {_unpriced} close(s) carried no P/L "
              f"— excluded from W/L and from the total")
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
