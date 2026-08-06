"""Exercise the REAL DailySummaryJob._execute() path against the live CC.

Sends nothing: a capture transport stands in for ChannelOps, so this proves
what would be published at 08:00 UTC without publishing it.
"""
import sys, os, json
sys.path.insert(0, r'D:\CommandCenter')
from signal_broadcaster import DailySummaryJob, CardFormatter, Transport


class CaptureTransport(Transport):
    name = "capture"

    def __init__(self):
        self.sent = []

    def send_free(self, message, event_id="", event_type=""):
        self.sent.append(("free", event_type, message)); return True

    def send_paid(self, message, event_id="", event_type=""):
        self.sent.append(("paid", event_type, message)); return True


cap = CaptureTransport()
job = DailySummaryJob("http://localhost:9000", cap, CardFormatter())

# Run the actual scheduled method — same code path 08:00 UTC will take.
job.run_now()

assert cap.sent, "DailySummaryJob produced NO output — it would fire silently"
print(f"cards produced: {len(cap.sent)}  tiers: {[t for t, _, _ in cap.sent]}")

live = json.loads(__import__('urllib.request', fromlist=['x'])
                  .urlopen('http://localhost:9000/api/expectancy',
                           timeout=12).read())
pb, fm = live.get('participating_bots'), live.get('fleet_members')
ev = live.get('fleet_expectancy')
print(f"live API: expectancy={ev} participating={pb} members={fm}")

paid = [m for t, _, m in cap.sent if t == "paid"]
free = [m for t, _, m in cap.sent if t == "free"]

failures = []

# The paid card must carry the figure AND its denominator.
for body in paid:
    ev_lines = [l for l in body.splitlines() if "Avg P/L" in l]
    if not ev_lines:
        # Correct when there are no closed trades: the row is omitted rather
        # than printing "$+0.00" from zero measurements.
        if isinstance(ev, (int, float)):
            failures.append("paid card has no Avg P/L line despite ev=%r" % ev)
        else:
            print("PAID  expectancy line: (omitted — 0 trades, nothing measured)")
        continue
    line = ev_lines[0]
    print(f"PAID  expectancy line: {line.strip()}")
    if isinstance(ev, (int, float)) and f"{ev:+.2f}" not in line:
        failures.append(f"paid card missing live figure {ev:+.2f}")
    if pb is not None and fm is not None and pb < fm:
        if f"{pb}/{fm}" not in line:
            failures.append(f"paid card missing denominator {pb}/{fm}")

# The free card must not publish a bare fleet-labelled expectancy.
for body in free:
    ev_lines = [l for l in body.splitlines() if "Avg P/L" in l or "E[V]" in l]
    print(f"FREE  expectancy line: {ev_lines or '(none — free card omits it)'}")
    for line in ev_lines:
        if pb is not None and fm is not None and pb < fm and f"{pb}/{fm}" not in line:
            failures.append("free card shows expectancy WITHOUT denominator")

# No card may render a placeholder where a real number was expected.
for tier, _, body in cap.sent:
    if "None" in body:
        failures.append(f"{tier} card contains literal 'None'")

print()
if failures:
    for f in failures:
        print("  FAIL:", f)
    raise SystemExit(1)
print("DAILY SUMMARY VERIFIED — fires with live figures and the denominator")
