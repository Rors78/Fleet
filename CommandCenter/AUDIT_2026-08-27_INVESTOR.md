# Fleet audit — 2026-08-27

Commissioned as an investor-readiness review. Every figure below was
re-derived from the durable event bus and `logs/expectancy.json`, not read
off an endpoint. Where a published number turned out to be wrong, the
correction and the mechanism are both stated.

---

## 1. The number you can defend

**Current era (post 2026-08-13 pool resize), all bots:**

| | |
|---|---|
| Closed trades | **16 decided** (0 flat) |
| Gross P/L | **+$5.42** |
| Win rate | **43.8%** (7W / 9L) |
| Expectancy | **+$0.34 / decided trade** |
| Return on pool | **+2.5%** on $215.95 |
| Contributing bots | **1 of 6 traders** (confluence) |

**This is PROVISIONAL and must be quoted with its n.** The fleet's own bar
for an expectancy read is ~30 closed trades; this is 16.

Stress-tested on the 18 raw store records (before dedupe):

- 95% CI on per-trade P/L: **[-$0.04, +$0.69]** — **includes zero**
- The **two best trades produce 76%** of all profit
- Remove them and the remainder is **+$1.42 over 16 trades**

The honest statement to an investor is: *the fleet is roughly break-even
with a slight positive tilt, on a sample too small to distinguish from
noise.* It is not yet evidence of an edge. The shape is interesting — a
sub-50% win rate that still profits because wins average $0.95 against
losses of $0.18 — but 16 trades cannot establish that.

## 2. What the dashboard said before this audit

**`WR: 53.8% (n=52)`** — wrong, and wrong in the flattering direction.

    gridzilla   16/16  = 100.0%    <- degenerate; loss column empty by construction
    confluence  11/32  =  34.4%
    turtlesue    1/4   =  25.0%    <- the 1 "win" is a 1.78e-14 rounding artefact
    -----------------------------
    published   28/52  =  53.8%

Gridzilla supplied **57% of the numerator**. Excluding it: **12/36 = 33.3%**,
a 20.5-point swing.

`expectancy.py` already computed `win_rate_degenerate: true` for gridzilla,
and the DASHBOARD honoured that flag in two display sites — but the fleet
aggregate in `command_center.py`, which produces the headline chip, never
read it. The flag stopped at the display layer while the number was
computed upstream of it. **Fixed.**

**`best_performer`** — gridzilla ranked first on **+$960.94**, every dollar
booked pre-resize on positions up to **$49,784**, against a pool that is now
**$215.95**. Its largest stored position is **236x the entire current pool**.
Real arithmetic, impossible economics, top of the leaderboard. **Fixed** — a
degenerate bot can no longer win the ranking. `worst_performer` is
deliberately NOT filtered: a degenerate rate can only flatter, so it can
only wrongly promote a bot, never wrongly condemn one.

## 3. The defect that matters most for risk

**A stop-loss can silently stop existing.**

Confluence held BLUR/USD. Oracle (its only price source, ~10 published
rows) stopped covering the pair. `_price_for` returned None, and
`if not price: continue` skipped stop, take-profit AND the 36h time exit
together. The position ran to 37.8h. When a fallback quote was added it
closed **immediately at `exit_reason=STOP`** — it had been through its stop
the whole time, invisibly.

A fleet-wide sweep found **the same shape in all six traders**:

| Bot | Finding | Status |
|---|---|---|
| Confluence | time exit unreachable when Oracle drops a held pair | **fixed** — flag + held-pairs-only fallback quote |
| Gridzilla | `check_drawdown_kill` needs NO price yet sat behind the price guard; **347 + 1273 Kraken failures in its own log**; no time exit as backstop | **fixed** — kill runs while blind |
| Arbitrageur | fell back to `pos.entry_price` — stop/TP both false, TIME_STOP still fires and books a real loss as **$0.00**; price cache had **no TTL** (scans dropped 4.2s to 0.0s: serving a startup price) | **fixed** — no fabricated price, 300s TTL |
| NexusBrain | 72h `check_exits` skipped by the price guard; dead `time_exit_hours = 48` that read as live policy | **fixed** — flag + dead config removed |
| Rubberband | `self.prices` never invalidated, rolling top-15 universe, **no max-age exit at all**, `MAX_POSITIONS = 1` so one stranded position wedges the bot | **fixed** — 600s staleness rejection |
| TurtleSue | counted **1.78e-14 as a win**; returned 0.0 for an empty book | **fixed** — epsilon + None-when-unmeasured |

Every one of these was **silent**. Not one logged a warning or emitted an
event when it could not price a position it held. Four of six rendered a
blind position as healthy or break-even. All six now report it.

## 4. Data integrity

**Two trades were double-booked** in the durable store (ENA/USD, BLUR/USD).
Confluence emits TRADE_CLOSE twice — once via the portfolio release (rich,
carrying `reservation_id`) and once from `_close()` (thin, without it).
`record_trade` dedups on `trade_id`; the two routes produced structurally
different keys, so dedup could never fire.

This is the **second** time this defect has been fixed. The first fix was
applied to the consumer (`command_center.py`) and was correct but
insufficient — it cannot work for an emitter that never sends the field.
**Now fixed at the emitter**, verified: 4 events produce 2 distinct keys.

Note the consumer-side hardening does NOT resolve the historical pair: the
two routes reported -0.2807 and -0.28 (one path rounds), so any
content-derived key still misses. The emitter fix is the real one.

**ACTION REQUIRED:** `logs/expectancy.json` still contains the 2 duplicate
rows. A deduped copy is prepared at
`.claude/jobs/e4a0593c/tmp/expectancy.deduped.json` (24 confluence records
instead of 26). It was **not** applied because Command Center holds the
store in memory and rewrites the whole file on every save — a write now
would be silently reverted. Apply it while CC is stopped.

CC's live `/api/current_era` already reports the correct n=16 / +$5.42, so
only the file on disk is affected.

## 5. What is measured well

Worth stating plainly, because an audit that only lists faults misleads:

- `/api/current_era` is the best figure the system produces — it excludes
  the 136 pre-resize trades and carries an explicit PROVISIONAL caveat
  below 30 trades.
- The pool reconciles exactly: $20.68 + $5.61 = $26.29 deployed against two
  open positions, $189.66 available. No stranded capital.
- `expectancy.py` already distinguishes absent / empty / unreadable, refuses
  to record a trade with neither prices nor P/L, and publishes
  `size_spread.dollar_mean_comparable: false` when stored trades are sized
  in a different pool era (currently **273x** the per-trade cap).
- The header win-rate chip carries its real denominator and names its
  window; the EXPECTANCY panel discloses when its dollar figure is
  era-stale.

The measurement layer is genuinely good. The failures were all at the
**seams** — a flag that reached the display but not the aggregate, a fix
applied to the consumer but not the producer, a guard written for
price-based exits that swallowed the price-free ones.

## 6. Standing risks not fixed here

- **Gridzilla's pre-fix ledger is permanently unmeasured.** 167 grids were
  torn down booking zero losses; the bus payload never carried the
  mark-to-market, so the true P/L was never written anywhere. Its code was
  fixed 2026-08-26, but **there have been zero GRID_KILLED events since**,
  so the fix is untested in production. Treat any gridzilla figure as
  unmeasured until it trades again.
- **Command Center binds 0.0.0.0:9000 with no authentication.** Must not be
  port-forwarded to the internet as-is.
- **~150 bare `except: pass` sites remain**, 45 on paths that record,
  persist or publish. Catalogued, not triaged. Mass-editing them would be
  change for its own sake; they should be fixed when touched.
- **One bot carries the entire current-era record.** Five of six traders
  have contributed nothing measurable since the resize.

## 7. Verification

- Suite: **86 tests, 86 passing, 0 failing** (grew from 84; this audit
  added `test_degenerate_rate_excluded.py` and
  `test_price_free_exits_reachable.py`, both confirmed running).
- Every new test was **sabotage-tested against the real pre-fix source**
  and confirmed to fail: the degenerate-rate test fired all 5 checks
  against the original `command_center.py`; the price-free-exit test fired
  7 checks against the four original bot files.
- All 7 edited files parse and every name referenced in an edit is defined
  (checked explicitly — one edit referenced `log` where the module defines
  `logger`, and one referenced `portfolio_balance`; both caught and
  verified in scope before shipping).

**The bot fixes are on disk but NOT in the running processes.** They load
on the next restart, which the watchdog owns.
