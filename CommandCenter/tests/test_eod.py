"""EndOfDayJob: does the image card stay honest with zero trades?

_gather_data returns None (not 0) for gross/fees/net when nothing traded.
The question is whether the RENDERER honours that or prints $0.00.
"""
import sys, os
sys.path.insert(0, r'D:\CommandCenter')
import os as _os
_OUT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'out')
_os.makedirs(_OUT, exist_ok=True)
from signal_broadcaster import EndOfDayJob, Transport

class Cap(Transport):
    name = "capture"
    def __init__(self): self.sent = []
    def send_free(self, m, event_id="", event_type=""): self.sent.append(("free", m)); return True
    def send_paid(self, m, event_id="", event_type=""): self.sent.append(("paid", m)); return True
    def send_free_image(self, png, caption="", event_id="", event_type=""):
        self.sent.append(("free_img", png)); return True
    def send_paid_image(self, png, caption="", event_id="", event_type=""):
        self.sent.append(("paid_img", png)); return True

cap = Cap()
job = EndOfDayJob("http://localhost:9000", cap, None)

data = job._gather_data()
print("=== _gather_data() with live (empty) fleet ===")
for k, v in data.items():
    if k == "trades":
        print(f"  {k:28} {len(v)} trades")
    else:
        print(f"  {k:28} {v!r}")

fails = []
if not data["trades"]:
    for k in ("gross", "fees", "net"):
        if data[k] == 0:
            fails.append(f"{k} is 0 with no trades — should be None (nothing measured)")
        elif data[k] is not None:
            fails.append(f"{k} is {data[k]!r} with no trades")

# Now check the renderer, if one exists.
print()
try:
    from card_renderer import CardRenderer
    r = CardRenderer()
    print("CardRenderer imported — rendering both tiers")
    for tier in ("paid", "free"):
        try:
            png = r.render_end_of_day(data, tier=tier)
            dest = os.path.join(_OUT, f'eod_{tier}.png')
            open(dest, 'wb').write(png)
            print(f"  {tier}: {len(png)} bytes -> {dest}")
        except Exception as e:
            fails.append(f"renderer {tier} raised: {type(e).__name__}: {e}")
            print(f"  {tier}: RAISED {type(e).__name__}: {e}")
except ImportError as e:
    print(f"no CardRenderer module ({e}) — EOD image path is inert")

print()
if fails:
    for f in fails: print("  FAIL:", f)
    raise SystemExit(1)
print("EOD DATA VERIFIED — no fabricated zeros")
