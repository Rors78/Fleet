# Fleet Audit — 2026-08-26 (operator handoff)

**Scope:** full read of the live fleet, then edits. Every figure below is
re-derived from the durable event bus (`logs/event_bus/*.jsonl`, 25 files),
not read off a dashboard.

**What this fleet is:** a paper-trading **signal** product. It has never
traded real money and cannot — Confluence's live-execution path is
deliberately unimplemented and releases its reservation rather than fill.
The asset at risk is the *signal*, not capital.

---

## The headline

The fleet's record **on the pool it actually has**:

| | |
|---|---|
| Closed trades | **14** |
| Total P/L | **+$5.01** |
| Win rate | **42.9%** (n=14 decided) |
| Avg win / avg loss | +$1.04 / −$0.15 |
| Expectancy | **+$0.36 / trade** |
| Return on pool | **+2.32%** ($215.54) |
| Window | 2026-08-13 21:00 UTC → now (12.8 days) |

**Verdict: PROVISIONAL.** n=14 is below this fleet's own 30-trade bar,
and 109% of the P/L traces to three positions a broken exit guard forgot
(see below).
Positive and honest, but not yet a rate. Do not drive parameter changes from
it, and quote it only with the n attached.

---

## Three published numbers that fail audit

**1. The −$362.76 headline is an era artifact.**
Bot P/L ledgers are lifetime and cross the 2026-08-13 pool resize
($1,000,000 → $210.53). The total adds two pools' worth of dollars under one
label. Confluence's −362.76 is dominated by pre-resize trades on notionals
of $38k–$49k (FLOW −1216.75, STORJ −567.06, XMR −581.30, ETHFI +3111.19)
against a pool now worth $215. A single one of those exceeds the whole
current pool by 14×.
*Fixed:* aggregate now discloses the crossing; `/api/current_era` reports the
current-pool record separately. The lifetime number is unchanged, not hidden.

**2. Gridzilla's 100% win rate (16W/0L) is stale, not earned.**
The structural cause — only winning cycles recorded — was fixed earlier and
is loaded in the running process. But it has recorded no closes since, so the
100% is **history, not new evidence**. I drove the shipped teardown logic
through five cases including a legacy state file: a losing teardown now books
a negative P/L and reaches the expectancy tracker. **Armed and proven capable
— but still unproven in live trading.** Treat the 100% as untrustworthy until
a real losing cycle books.

**3. TurtleSue's +$370.20 is entirely test fixtures.**
30 ZZPROBE closes, every one a win. Its real-pair P/L is **$0.00** across 3
trades. The probe predicate now excludes these fleet-wide.

---

## Defects found and fixed

**Confluence's time exit was unreachable** — and it produced 88% of the
"profit". `manage_positions()` opened with `if not price: continue`, and the
`MAX_POSITION_AGE_H = 36` check sat *below* that guard. Any pair Oracle
stopped quoting could never age out. Measured: AAVE/USD held **215.9h
(6.0×)**, LINK/USD 212.1h, PENDLE/USD 182.9h. Those three booked **+$5.47
against an era total of +$5.01 — 109% of all profit.** Strip them out and the
fleet is **−$0.46 over the remaining 11 trades**: net negative. The fleet is
in the black *only* because of positions the exit logic lost track of, which
drifted up over 8–9 days in a rising tape. A falling tape takes it back the
same way, and the same broken guard would have held them all the way down.
*Fixed:* the age check is now reachable without a quote. It **flags rather
than force-closes** — closing needs a price, and inventing one books a
fictional P/L.

**Confluence never reached the event bus.** `_emit()` called
`self._events.publish({...})`; `EventPublisher`'s API is `emit(type, data)`.
Every call raised AttributeError into a bare `except: pass`. **0 TRADE_OPEN
events in the bot's entire history** vs 146 from gridzilla. The 22 TRADE_CLOSE
rows on the bus were synthesized by Command Center's `portfolio_release` path
(`"via": "portfolio_release"`), not the bot. The only live trader was
invisible on entry.
*Fixed:* correct API, failures logged once per event type. Swept all 17
siblings — every other bot was already correct.

**Rubberband's entry gate was mathematically unreachable.** 600 scans, 0
signals, 0 trades ever — reading as "selective" the whole time. It measured
reward on the 15m Bollinger band but risk as 2.0 × the **60m** ATR. Clearing
2:1 required ATR_60m ≤ 0.93× ATR_15m; measured live against Kraken OHLC it is
**2.29×** (XBT 2.31, ETH 2.14, LINK 2.26, AVAX 2.53, XRP 2.24).
*Fixed:* the stop now uses the 15m ATR — the same clock as the target — in
both LONG and SHORT branches. `MIN_RR_RATIO` stays at 2.0; that gate ended a
21-consecutive-loss streak and the defect was never the ratio.
**Necessary but not sufficient:** this converts an *impossibility* into a
*rare setup*. The gate now reduces to `BB_stdev ≥ 2 × ATR_15m`, and 0 of 25
live pairs cleared it at fix time (best: AAVE 1.86; CSPR improved 0.87 →
1.99). Expect signals to stay infrequent.

**TurtleSue printed its own floor as "$0".** The min-trade rejection
formatted a $0.25 floor with `:.0f`, so a real rejection read
`too small: $0.13 < $0 minimum` — unfalsifiable to an operator.

---

## Corrections to my own work

**I got the era boundary wrong twice, and neither error raised anything.**
`1786924800.0` was 2026-08-17 (4 days late — dropped 7 real trades).
`1786579200.0` was 2026-08-13 00:00 UTC (21h early — admitted 12 old-pool
trades and reported **+$939 on a $215 pool** with a $300 average win, a 436%
return with clean arithmetic). The correct value is derived from the notional
break in the data itself (ETHFI $38,557 → CRV $6.80) and verified with
`time.gmtime()`.

**My first test failed against correct code** because it matched the old
broken call in the fix's own docstring. Tests now strip comments and
docstrings before scanning.

---

## What I did NOT do

- **No restarts.** The watchdog owns restarts; racing it creates duplicate
  processes that can trade invisibly. **Every fix here is committed but NOT
  running** — the processes started 03:49 today and still carry the old code.
- **No collateral change.** Raising the pool would mask the stale constants
  and reset the only measurable record.
- **No deployment.** All work is on branch `audit-exit-emit`; the deployed
  tree is untouched and the live page still serves HTTP 200.

---

## Fleet shape, for the record

18/18 alive, none stale. But **only 6 are traders** (arbitrageur, confluence,
gridzilla, nexusbrain, rubberband, turtlesue) — the other 12 are intel and
support. "18 bots" is not 18 strategies.

Of the 6 traders, **1 has traded in the current era** (confluence). Gridzilla
last closed pre-resize; turtlesue's real P/L is $0.00; rubberband and
arbitrageur have never traded. Arbitrageur's silence looks like genuine
selectivity — it finds 7 correlated pairs but no ≥2% gap.

---

## What an investor should be told

The honest pitch is **infrastructure, not track record**. The measurement
layer here is unusually disciplined — absent stays absent, rates ship with
their n, era boundaries are enforced, and guards are proven red-capable. That
is real and rare.

The track record is **14 trades and +$5.01**, from one bot — and **109% of
that profit came from three positions a broken exit guard forgot about.**
Excluding them the record is −$0.46 over 11 trades. The strategy has not yet
demonstrated an edge; what it has demonstrated is that the instrumentation is
good enough to catch that. Anyone shown a win rate without its n, or a P/L
spanning the pool resize, is being shown something the data does not support.
