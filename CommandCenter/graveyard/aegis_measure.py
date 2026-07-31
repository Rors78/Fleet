import json, math, time
from collections import Counter, defaultdict

with open('D:/CommandCenter/aegis_master.json', encoding='utf-8') as f:
    master = json.load(f)
with open('D:/CommandCenter/aegis_all.json', encoding='utf-8') as f:
    all_events = json.load(f)

print("=== EVENT BUS CONTENTS (n=500 unfiltered) ===")
types = Counter(e.get('type') for e in all_events)
print("Total events:", len(all_events))
print("Type counts:")
for t, c in types.most_common():
    print(f"  {t}: {c}")
ts_vals = [e.get('ts', 0) for e in all_events if e.get('ts')]
now = time.time()
if ts_vals:
    print(f"Event window: {now - max(ts_vals):.0f}s to {now - min(ts_vals):.0f}s ago")
print()

# === Q1 ===
print("=== Q1: REGIME / ENTROPY ===")
_VALID = {
    "bull","bear","range","chop","trending","ranging","defensive","cautious",
    "BULL","BEAR","RANGE","CHOP","TRENDING","RANGING","DEFENSIVE","CAUTIOUS",
    "TREND_UP","TREND_DOWN","VOLATILE","MEAN_REVERTING",
    "mean_reverting","NEUTRAL","neutral","NORMAL","normal",
}
_NORM = {
    "bull":"BULL","BULL":"BULL","TRENDING":"BULL","TREND_UP":"BULL",
    "bear":"BEAR","BEAR":"BEAR","TREND_DOWN":"BEAR",
    "range":"RANGING","RANGE":"RANGING","RANGING":"RANGING","ranging":"RANGING",
    "EQUILIBRIUM":"RANGING","NEUTRAL":"RANGING","neutral":"RANGING",
    "MEAN_REVERTING":"RANGING","mean_reverting":"RANGING",
    "chop":"CHOP","CHOP":"CHOP",
    "transitioning":"TRANSITIONING","VOLATILE":"TRANSITIONING",
    "NORMAL":"RANGING","normal":"RANGING",
    "PRE_CRITICAL":"TRANSITIONING","CRITICAL":"TRANSITIONING",
    "DEFENSIVE":"RANGING","defensive":"RANGING",
    "EXTREME_FEAR":"BEAR","extreme_fear":"BEAR",
    "EXTREME_GREED":"BULL","extreme_greed":"BULL",
    "MIXED":"RANGING","mixed":"RANGING",
    "high_activity":"RANGING","HIGH_ACTIVITY":"RANGING",
}
_EXCL = {"aegis","phitex"}

raw_all = {}
passing = {}
for b in master.get('bots', []):
    if not b.get('alive'): continue
    bid = b.get('id')
    if bid in _EXCL: continue
    n = b.get('normalized') or {}
    r = n.get('regime')
    if r:
        raw_all[bid] = r
        if r in _VALID:
            passing[bid] = r

print(f"Alive non-excluded bots with regime: {len(raw_all)}")
for bid, r in sorted(raw_all.items()):
    status = "PASS" if r in _VALID else "BLOCK"
    norm = _NORM.get(r, r.upper())
    print(f"  {bid:15} raw={r!r:25} {status:5} norm={norm}")
print()

regime_events = [e for e in all_events if e.get('type') == 'REGIME_CHANGE']
print(f"REGIME_CHANGE events in window: {len(regime_events)}")
rc_by_source_raw = defaultdict(list)
for e in regime_events:
    src = e.get('source','')
    to = (e.get('data') or {}).get('to','')
    rc_by_source_raw[src].append(to)
for src, vals in sorted(rc_by_source_raw.items()):
    c = Counter(vals)
    print(f"  {src}: {dict(c)}")
print()

def entropy(labels):
    if not labels: return 1.0
    total = len(labels)
    c = Counter(labels)
    e = 0.0
    for v in c.values():
        p = v/total
        if p>0: e -= p*math.log2(p)
    return e / math.log2(5)

raw_labels = list(raw_all.values())
norm_labels_all = [_NORM.get(r, r.upper()) for r in raw_labels]
aegis_labels = [_NORM.get(r, r.upper()) for r in passing.values()]

print(f"Raw unique (all alive): {sorted(set(raw_labels))} count={len(set(raw_labels))}")
print(f"Fully normalized unique: {sorted(set(norm_labels_all))} count={len(set(norm_labels_all))}")
print(f"AEGIS-actual (filter+norm): {sorted(set(aegis_labels))} count={len(set(aegis_labels))}")
print(f"Entropy raw (all): {entropy(raw_labels):.4f}")
print(f"Entropy fully-normalized (all): {entropy(norm_labels_all):.4f}")
print(f"Entropy AEGIS-actual: {entropy(aegis_labels):.4f}")
blocked = [(b,r) for b,r in raw_all.items() if r not in _VALID]
print(f"Blocked by _VALID filter: {blocked}")
print()

# === Q2: Signal Coherence ===
print("=== Q2: SIGNAL COHERENCE ===")
sig_events = [e for e in all_events if e.get('type') in ('TRADE_OPEN','SIGNAL')]
print(f"TRADE_OPEN + SIGNAL events: {len(sig_events)}")
ttypes = Counter(e.get('type') for e in sig_events)
print(f"  Breakdown: {dict(ttypes)}")

# Sort by ts, group by bot
sig_events_sorted = sorted(sig_events, key=lambda e: e.get('ts', 0))
by_bot = defaultdict(list)
for e in sig_events_sorted:
    src = e.get('source','')
    d = e.get('data') or {}
    if not src or not d.get('pair'): continue
    by_bot[src].append(d)

print()
print(f"{'bot':15} {'evts':5} {'uniq_pair':10} {'max_streak':11} {'sp_ratio':10} {'spd_ratio':10}")
for bot, sigs in sorted(by_bot.items()):
    total = len(sigs)
    uniq = len(set(s.get('pair') for s in sigs))
    # max consecutive same-pair streak
    max_streak = 1 if sigs else 0
    cur = 1
    for i in range(1, len(sigs)):
        if sigs[i].get('pair') == sigs[i-1].get('pair'):
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 1
    # same_pair ratio: fraction of consecutive (i-1,i) pairs with same pair
    if len(sigs) >= 2:
        sp = sum(1 for i in range(1,len(sigs)) if sigs[i].get('pair')==sigs[i-1].get('pair'))
        spd = sum(1 for i in range(1,len(sigs)) if sigs[i].get('pair')==sigs[i-1].get('pair') and sigs[i].get('direction')==sigs[i-1].get('direction'))
        sp_ratio = sp/(len(sigs)-1)
        spd_ratio = spd/(len(sigs)-1)
    else:
        sp_ratio = spd_ratio = 0.0
    print(f"  {bot:13} {total:5} {uniq:10} {max_streak:11} {sp_ratio:10.3f} {spd_ratio:10.3f}")

# exact signal_coherence() calc
coherence_scores = []
for bot, sigs in by_bot.items():
    if len(sigs) < 2: continue
    stable = 0
    total = 0
    for i in range(1, len(sigs)):
        if sigs[i].get('pair') == sigs[i-1].get('pair'):
            total += 1
            if sigs[i].get('direction') == sigs[i-1].get('direction'):
                stable += 1
    coherence_scores.append(stable/total if total > 0 else 0.5)
C = sum(coherence_scores)/len(coherence_scores) if coherence_scores else None
print(f"Exact signal_coherence() = {C}  (None => bootstrap path)")
print()

# === Q3: Whale flow ===
print("=== Q3: WHALE FLOW ===")
whale_events = [e for e in all_events if e.get('type') == 'WHALE_ALERT']
print(f"WHALE_ALERT events in window: {len(whale_events)}")
tier_counts = Counter()
whale_pair_tier = defaultdict(list)
for e in whale_events:
    d = e.get('data') or {}
    tier = d.get('tier','')
    pair = d.get('pair','')
    tier_counts[tier] += 1
    if pair:
        whale_pair_tier[pair].append(tier)

print(f"Tiers: {dict(tier_counts)}")
print(f"Whale pairs: {list(whale_pair_tier.keys())}")
high_ext_pairs = set()
for pair, tiers in whale_pair_tier.items():
    if any(t in ('HIGH','EXTREME') for t in tiers):
        high_ext_pairs.add(pair)
print(f"HIGH/EXTREME whale pairs: {sorted(high_ext_pairs)}")

# trade+signal pairs
trade_pairs_500 = set()
for e in sig_events:
    p = (e.get('data') or {}).get('pair')
    if p: trade_pairs_500.add(p)
print(f"Bot trade+signal pairs (500-evt): {sorted(trade_pairs_500)}")
inter_500 = high_ext_pairs & trade_pairs_500
print(f"Intersection (500-evt): {sorted(inter_500)}")

# 300s window
cutoff = time.time() - 300
whales_300 = [e for e in whale_events if e.get('ts',0) > cutoff]
trades_300 = [e for e in sig_events if e.get('ts',0) > cutoff]
w300_pairs = set()
for e in whales_300:
    d = e.get('data') or {}
    if d.get('tier') in ('HIGH','EXTREME') and d.get('pair'):
        w300_pairs.add(d.get('pair'))
t300_pairs = set((e.get('data') or {}).get('pair') for e in trades_300 if (e.get('data') or {}).get('pair'))
print(f"300s window: whales={len(whales_300)} (HIGH/EXT pairs={sorted(w300_pairs)}), trades/signals={len(trades_300)} (pairs={sorted(t300_pairs)})")
print(f"Intersection (300s): {sorted(w300_pairs & t300_pairs)}")
