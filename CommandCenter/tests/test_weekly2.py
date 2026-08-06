"""Weekly scorecard: does it read the real files, and does it stay honest when empty?

The previous version of this test PASSED against a card that reported a flat
empty week while 5 real trades sat on disk. It only checked the card's text for
bad-looking strings; it never checked the card against the DATA. That is the
bug being fixed here as much as the code is.
"""
import sys, os, json, tempfile, shutil
sys.path.insert(0, r'D:\CommandCenter')
import os as _os
_OUT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'out')
_os.makedirs(_OUT, exist_ok=True)
from signal_broadcaster import WeeklyReportJob, CardFormatter, Transport
from datetime import datetime, timezone, timedelta


class Cap(Transport):
    name = "capture"
    def __init__(self): self.sent = []
    def send_free(self, m, event_id="", event_type=""): self.sent.append(("free", m)); return True
    def send_paid(self, m, event_id="", event_type=""): self.sent.append(("paid", m)); return True


def day_file(tmp, offset_days, pnl, trades, win_rate):
    """Write a file in fleet_logger.py's REAL schema."""
    d = (datetime.now(timezone.utc).date() - timedelta(days=offset_days)).strftime("%Y-%m-%d")
    payload = {
        "date": d,
        "fleet": {"starting_equity": 10000, "ending_equity": 10000 + pnl,
                  "daily_pnl": pnl, "total_trades": trades,
                  "wins": round(trades * win_rate / 100), "losses": 0,
                  "win_rate": win_rate,
                  "best_bot": {"id": "x", "pnl": pnl}, "worst_bot": {"id": "x", "pnl": pnl},
                  "regime_changes": 0, "whale_alerts": 0, "portfolio_denials": 0},
        "per_bot": {}, "uptime": {},
    }
    open(os.path.join(tmp, f"{d}.json"), "w", encoding="utf-8").write(json.dumps(payload))


def run(tmp):
    cap = Cap()
    job = WeeklyReportJob("http://localhost:9000", cap, CardFormatter())
    job._logs_dir = tmp
    job.run_now()
    return {t: m for t, m in cap.sent}


fails, out = [], []


def check(name, cond, detail=""):
    out.append(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# ── Case 1: real trades on disk MUST reach the card ──────────────────
tmp = tempfile.mkdtemp()
try:
    day_file(tmp, 1, -12.35, 3, 66.7)
    day_file(tmp, 2, -18.20, 2, 50.0)
    cards = run(tmp)
    paid = cards["paid"]
    out.append("=== CASE 1: 5 trades, -$30.55 on disk ===")
    out.append(paid)
    check("total P/L reaches card", "$-30.55" in paid, "expected $-30.55")
    check("trade count reaches card", "Trades    5" in paid)
    check("no phantom $+0.00", "Week P/L  $+0.00" not in paid)
    # 3 trades @66.7% + 2 @50% -> weighted 60% (not the 58% a flat mean gives)
    check("win rate weighted by trades", "Win Rate  60%" in paid,
          "flat mean would give 58%")
    # -30.55 / 5 = -6.11
    check("expectancy derived per-trade", "$-6.11" in paid)
    check("green days counts trading days only", "Green Days 0/2" in paid)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── Case 2: no files at all — nothing measured ───────────────────────
tmp = tempfile.mkdtemp()
try:
    cards = run(tmp)
    paid, free = cards["paid"], cards["free"]
    out.append("")
    out.append("=== CASE 2: no daily files ===")
    out.append(paid)
    check("empty: no fabricated P/L", "$+0.00" not in paid, "nothing was measured")
    check("empty: no literal None", "None" not in paid and "None" not in free)
    check("empty: no Green Days claim", "Green Days" not in paid)
    check("empty: no Win Rate row", "Win Rate" not in paid)
    check("empty: no Avg P/L row", "Avg P/L" not in paid)
    check("empty: no 'of 7 days' claim", "of 7" not in paid)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── Case 3: days recorded but zero trades — a REAL flat week ─────────
tmp = tempfile.mkdtemp()
try:
    day_file(tmp, 1, 0, 0, 0)
    day_file(tmp, 2, 0, 0, 0)
    cards = run(tmp)
    paid = cards["paid"]
    out.append("")
    out.append("=== CASE 3: 2 days recorded, 0 trades ===")
    out.append(paid)
    check("flat week: P/L is a real $+0.00", "Week P/L  $+0.00" in paid)
    check("flat week: says sidelines", "sideline" in paid.lower())
    check("flat week: no best/worst day", "Best Day" not in paid,
          "a no-trade day is not the best day of the week")
    check("flat week: no green-day ratio", "Green Days" not in paid)
    check("flat week: no win rate", "Win Rate" not in paid)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── Case 4: schema drift guard — flat shape must still work ──────────
tmp = tempfile.mkdtemp()
try:
    d = (datetime.now(timezone.utc).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    open(os.path.join(tmp, f"{d}.json"), "w").write(json.dumps(
        {"date": d, "daily_pnl": 44.0, "total_trades": 4, "win_rate": 75.0}))
    paid = run(tmp)["paid"]
    out.append("")
    out.append("=== CASE 4: flat (un-nested) shape ===")
    check("tolerates flat schema", "$+44.00" in paid and "Trades    4" in paid)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

out.append("")
out.append("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
dest = os.path.join(_OUT, 'weekly2.txt')
open(dest, 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(l for l in out if l.startswith('  ') or l.startswith('=') or l.startswith('ALL') or 'FAILED' in l))
raise SystemExit(1 if fails else 0)
