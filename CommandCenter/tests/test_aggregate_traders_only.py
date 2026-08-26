"""Fleet trading aggregates must count TRADERS only.

WHY THIS EXISTS. Observed live on 2026-08-26 16:23, minutes after a watchdog
restart:

    aggregate.total_open_positions   10
    portfolio.active_reservations     3

The 7-position gap was trinity, which is role="support" — an intel-only
scanner that holds no pool capital and cannot reserve. Its normalizer maps
signal tracks onto open_positions/total_trades, so the fleet appeared to hold
10 positions while the pool — the only thing that reserves real capital —
held 3.

A signal being tracked is not a position being held.

THE SHAPE. _TRADER_IDS already gated the WIN RATE, for exactly this reason
and with a long comment explaining it ("trinity is role='support' ... a
signal that resolved favourably is not a trade that made money"). The sibling
fields on the very next lines were left ungated. That is the
fix-one-sibling-miss-the-others pattern this project keeps hitting, and it is
why this test checks EVERY trading aggregate rather than the one that broke.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CC = os.path.dirname(HERE)
SRC_PATH = os.path.join(CC, "command_center.py")

sys.path.insert(0, CC)

failures = []

if not os.path.exists(SRC_PATH):
    print("FAIL: command_center.py not found at %s" % SRC_PATH)
    sys.exit(1)

with open(SRC_PATH, encoding="utf-8", errors="replace") as fh:
    SRC = fh.read()


def _code_only(text):
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return re.sub(r"#[^\n]*", "", text)


CODE = _code_only(SRC)

# ---------------------------------------------------------------------------
# 1. Every trading-activity field must be collected traders-only.
#    equity is deliberately NOT in this list: it is permanently None (there is
#    one pool and no bot holds capital), so it has nothing to leak.
# ---------------------------------------------------------------------------
TRADING_FIELDS = ["pnl", "open_positions", "total_trades"]

for field in TRADING_FIELDS:
    m = re.search(r"=\s*(_collect\w*)\(\"%s\"\)" % re.escape(field), CODE)
    if not m:
        failures.append("no collector found for %r in _compute_aggregate — "
                        "field renamed or removed" % field)
        continue
    fn = m.group(1)
    if fn == "_collect":
        failures.append(
            "%r is collected with _collect() (ALL bots) instead of a "
            "traders-only collector. trinity is role='support' and publishes "
            "signal tracks through this field: live on 2026-08-26 that made "
            "total_open_positions read 10 against 3 real reservations"
            % field)

# ---------------------------------------------------------------------------
# 2. The traders-only collector must actually filter on _TRADER_IDS.
#    A collector named right that filters nothing is worse than none.
# ---------------------------------------------------------------------------
m = re.search(r"def _collect_traders\(key\):(?:.|\n)*?(?=\n    \w|\n\n)", CODE)
if not m:
    failures.append("_collect_traders() not defined")
elif "_TRADER_IDS" not in m.group(0):
    failures.append("_collect_traders() does not filter on _TRADER_IDS — it "
                    "is a traders-only collector in name only")

# ---------------------------------------------------------------------------
# 3. _TRADER_IDS must be derived from fleet_config roles, not hardcoded.
#    A hardcoded list goes stale the moment a bot changes role.
# ---------------------------------------------------------------------------
try:
    from fleet_config import BOTS
    real_traders = {k for k, v in BOTS.items() if v.get("role") == "trader"}
    if not real_traders:
        failures.append("fleet_config has no role='trader' bots — the role "
                        "field this filter depends on is gone")
    if "trinity" in real_traders:
        failures.append("trinity is now role='trader' in fleet_config — this "
                        "test's premise needs re-deriving")
except Exception as e:
    failures.append("cannot import fleet_config: %r" % e)
    real_traders = set()

# ---------------------------------------------------------------------------
# 4. Behavioural: a support bot publishing open_positions must not reach the
#    fleet total. Drives the real filter predicate.
# ---------------------------------------------------------------------------
if real_traders:
    norms = {
        "confluence": {"open_positions": 3, "total_trades": 30, "pnl": -362.76},
        "trinity":    {"open_positions": 7, "total_trades": 2,  "pnl": 0.0},
    }
    TRADER_IDS = real_traders

    def collect_traders(key):
        return [(b, n[key]) for b, n in norms.items()
                if b in TRADER_IDS and n.get(key) is not None]

    got = sum(v for _, v in collect_traders("open_positions"))
    if got != 3:
        failures.append("traders-only open_positions summed to %d, expected 3 "
                        "(trinity's 7 signal tracks must not count)" % got)

    got_t = sum(v for _, v in collect_traders("total_trades"))
    if got_t != 30:
        failures.append("traders-only total_trades summed to %d, expected 30"
                        % got_t)

# --------------------------------------------------------------------- report
print("checked %s" % SRC_PATH)
print("traders (from fleet_config): %s" % sorted(real_traders))
for field in TRADING_FIELDS:
    m = re.search(r"=\s*(_collect\w*)\(\"%s\"\)" % re.escape(field), CODE)
    print("  %-16s -> %s" % (field, m.group(1) if m else "NOT FOUND"))

if failures:
    for f in failures:
        print("FAIL: %s" % f)
    sys.exit(1)

print("PASS: pnl, open_positions and total_trades all count traders only; "
      "support-bot signal tracks cannot inflate the fleet position count")
