"""Exercise the REAL WeeklyReportJob._execute() against the live CC.

Sends nothing: a capture transport stands in for ChannelOps.
"""
import sys, os, json, glob
sys.path.insert(0, r'D:\CommandCenter')
import os as _os
_OUT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'out')
_os.makedirs(_OUT, exist_ok=True)
from signal_broadcaster import WeeklyReportJob, CardFormatter, Transport


class CaptureTransport(Transport):
    name = "capture"

    def __init__(self):
        self.sent = []

    def send_free(self, message, event_id="", event_type=""):
        self.sent.append(("free", event_type, message)); return True

    def send_paid(self, message, event_id="", event_type=""):
        self.sent.append(("paid", event_type, message)); return True


cap = CaptureTransport()
job = WeeklyReportJob("http://localhost:9000", cap, CardFormatter())

logs = os.path.join(r'D:\CommandCenter', 'logs', 'daily')
files = sorted(glob.glob(os.path.join(logs, '*.json')))
print(f"daily log files present: {len(files)}")
for f in files[-8:]:
    print("   ", os.path.basename(f))

job.run_now()

out = []
for tier, et, body in cap.sent:
    out.append("=" * 60)
    out.append(f"TIER={tier}  EVENT={et}")
    out.append("=" * 60)
    out.append(body)
    out.append("")

dest = os.path.join(_OUT, 'weekly.txt')
open(dest, 'w', encoding='utf-8').write('\n'.join(out))
print(f"cards produced: {len(cap.sent)}  -> {dest}")

failures = []
for tier, _, body in cap.sent:
    if 'None' in body:
        failures.append(f"{tier}: literal 'None' in card")
    # A figure printed from zero measurements is the defect we are hunting.
    for line in body.splitlines():
        s = line.strip()
        if s.startswith('Avg P/L') and '$+0.00' in s:
            failures.append(f"{tier}: 'Avg P/L $+0.00' rendered from no data -> {s}")
        if s.startswith('Week P/L') and '$+0.00' in s:
            failures.append(f"{tier}: 'Week P/L $+0.00' rendered from no data -> {s}")
        if s.startswith('Win Rate') and s.endswith('0%') and 'Win Rate  0%' in s:
            failures.append(f"{tier}: 'Win Rate 0%' from zero trades -> {s}")
        if 'Green Days' in s and '/7' in s and '0/7' in s:
            failures.append(f"{tier}: 'Green Days 0/7' with no days loaded -> {s}")

print()
if failures:
    for f in failures:
        print("  FAIL:", f)
    raise SystemExit(1)
print("WEEKLY VERIFIED")
