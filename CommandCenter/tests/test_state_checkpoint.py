"""Safety state must actually reach disk, not merely be writable.

The gap this closes: tests/test_unreadable_state.py calls _save_positions()
directly and passed, while the live turtle_positions.json carried only
['positions','saved_at'] — no equity, no trades. The writer worked; nothing
called it. All four call sites were entry/pyramid/exit, so a bot holding
steady positions never rewrote state, and the drawdown brake re-anchored to
starting_equity on every restart.

A test that constructs its own state cannot catch that. This one asserts on
the FILE the running bot writes.
"""
import json
import os
import sys
import time

sys.path.insert(0, r'D:\CommandCenter')

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# (bot, state file, keys that must be present, max age in seconds)
# Age budgets are the bot's scan cadence plus slack — a file older than that
# means the checkpoint is not firing.
#
# Coverage audit 2026-08-13: this list held only two bots while four more
# persist safety state. Extending it surfaced the reason age budgets are
# hard here — see EVENT_DRIVEN below.
TARGETS = [
    ("turtlesue", r"D:\TurtleSue\turtle_positions.json",
     ["positions", "equity", "peak_equity", "trades"], 900),
    ("confluence", r"D:\Confluence\confluence_state.json",
     ["positions", "realized_pnl", "wins", "losses"], 900),
    ("rubberband", r"D:\Rubberband\rubberband_state.json",
     ["positions", "equity", "peak_equity", "sl_cooldowns"], 900),
    # Gridzilla now checkpoints every 5 min from its scan loop; before that
    # its file went 55 minutes stale while the bot was perfectly healthy,
    # because all four save sites were event-driven.
    ("gridzilla", r"D:\Gridzilla\gridzilla_state.json",
     ["active_grids", "total_pnl"], 900),
]

# Bots whose state file is written ONLY on a position change. For these an
# old file is not evidence of a stalled writer: arbitrageur's was 12.9 days
# old on 2026-08-13 and entirely correct — it holds `positions: []` and the
# bot has genuinely had no position in that time. Asserting a freshness
# budget on these would fail on a healthy idle bot, which is a false RED
# and just as damaging as a false green.
#
# They are still checked for SHAPE and parseability; only the age assertion
# is skipped, and the age is printed so a human can eyeball it.
EVENT_DRIVEN = [
    ("arbitrageur", r"D:\Arbitrageur\arbitrageur_state.json", ["positions"]),
    ("nexusbrain", r"D:\NexusBrain\nexus_positions.json", ["positions"]),
]

print("=== state files carry their safety fields ===")
for bot, path, keys, _budget in TARGETS:
    if not os.path.exists(path):
        check(f"{bot}: state file exists", False, path)
        continue
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        check(f"{bot}: state file parses", False, f"{type(e).__name__}: {e}")
        continue
    missing = [k for k in keys if k not in d]
    check(f"{bot}: carries {', '.join(keys)}", not missing,
          f"missing {missing}" if missing else "")

print()
print("=== event-driven state files carry their fields (age not asserted) ===")
now = time.time()
for bot, path, keys in EVENT_DRIVEN:
    if not os.path.exists(path):
        check(f"{bot}: state file exists", False, path)
        continue
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        check(f"{bot}: state file parses", False, f"{type(e).__name__}: {e}")
        continue
    missing = [k for k in keys if k not in d]
    _age = now - os.path.getmtime(path)
    check(f"{bot}: carries {', '.join(keys)}", not missing,
          (f"missing {missing}" if missing
           else f"{_age / 3600:.1f}h old — event-driven, age not asserted"))

print()
print("=== state files are FRESH — the checkpoint actually fires ===")
for bot, path, _keys, budget in TARGETS:
    if not os.path.exists(path):
        continue
    age = now - os.path.getmtime(path)
    check(f"{bot}: written within {budget}s", age <= budget,
          f"{age / 60:.1f} min old — checkpoint not firing"
          if age > budget else f"{age:.0f}s old")

print()
print("=== the equity ledger is not silently re-baselined ===")
p = r"D:\TurtleSue\turtle_positions.json"
if os.path.exists(p):
    d = json.load(open(p, encoding="utf-8"))
    eq, peak = d.get("equity"), d.get("peak_equity")
    check("equity is a number", isinstance(eq, (int, float)), repr(eq))
    check("peak_equity is a number", isinstance(peak, (int, float)), repr(peak))
    # peak can never be below equity; if it is, the ledger was reset.
    if isinstance(eq, (int, float)) and isinstance(peak, (int, float)):
        check("peak >= equity", peak >= eq - 0.01,
              f"peak {peak} below equity {eq} — ledger was reset"
              if peak < eq - 0.01 else f"peak {peak}, equity {eq}")

print()
print("=== confluence's fee restatement is durable, not recomputed ===")
p = r"D:\Confluence\confluence_state.json"
if os.path.exists(p):
    d = json.load(open(p, encoding="utf-8"))
    trades = d.get("closed_trades") or []
    unrestated = [t for t in trades
                  if (t.get("fees") or 0) and not t.get("restated_gross")]
    check("no unrestated fee rows left on disk", not unrestated,
          f"{len(unrestated)} rows still carry fees")
    # realized_pnl should track the sum of its rows, but not to the cent:
    # the restatement writes round(gross, 2) into each row while
    # realized_pnl accumulates unrounded figures, so a sub-cent-per-trade
    # gap is arithmetic, not corruption. Verified on the live file:
    # rows sum -14.9000, stored -14.8897, difference 0.0103 over 5 trades.
    # Tolerance is one cent per trade plus a cent of slack; anything larger
    # means the ledger and its rows have genuinely diverged.
    if trades:
        s = sum(t.get("net_pnl") or 0 for t in trades)
        r = d.get("realized_pnl")
        tol = 0.01 * len(trades) + 0.01
        check("realized_pnl tracks its rows",
              isinstance(r, (int, float)) and abs(r - s) <= tol,
              f"stored {r:.4f}, rows sum {s:.4f}, diff {abs(r - s):.4f} "
              f"(tolerance {tol:.2f})" if isinstance(r, (int, float)) else repr(r))

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    raise SystemExit(1)
print("ALL PASS — safety state reaches disk and stays coherent")
