# Expectancy store repair — 2026-08-07

`POST /api/expectancy/repair` was run once against the live store after the
Command Center restart that loaded `e2b488e`. This file records what changed,
because the corrected figures differ from every report published before it.

## Why the rows were wrong

Three writers used to persist `entry_price=data.get(..., 0)` into the durable
expectancy store, so a trade whose bot reported P/L but no prices was stored
claiming an entry of **$0.00** — a price nobody measured, indistinguishable
from a real one. All three are fixed (`8cac8f7`, `e1763b2`, `f7b0481`), but the
rows they had already written stayed.

Separately, the reservation-release path keyed its dedup on the **reservation
id** while the TRADE_CLOSE bus path keyed on the **event id**, so one close
arriving by both routes was stored twice under different keys.

The store could not be cleaned on disk: `ExpectancyTracker` loads once at
construction and `_save()` writes the in-memory copy, so a file edit under a
running Command Center is silently overwritten by the next trade.

## What the repair did

```
nulled:  5 fabricated price fields (entry/exit that were exactly 0)
dropped: 1 duplicate — gridzilla POL/USD gross=162.9797
```

Conservative by construction: it only nulls price fields that are exactly `0`,
and only drops a row duplicating another's `(pair, gross_pnl)` while carrying
strictly less price information. No `gross_pnl`, `size_usd`, `duration`,
`direction`, `won` or timestamp was modified.

## Before / after

| | before | after |
|---|---|---|
| total_trades | 7 | **6** |
| gridzilla expectancy | $86.17/trade | **$70.81/trade** |
| confluence expectancy | -$636.29 | -$636.29 (unchanged) |
| rows with a fabricated $0.00 price | 5 | **0** |
| total gross P/L | -$282.2462 | -$282.2462 (unchanged) |

**Gridzilla's per-trade expectancy was overstated by ~22%** — the double-count
was crediting one real +$162.98 cycle twice. Total P/L is unchanged because
nothing real was removed; only the trade *count* was wrong, and expectancy
divides by it.

Confluence's genuinely measured prices (`0.0968934 → 0.0954`) were left exactly
as recorded, which is the other half of the check: the repair must not touch a
real reading.

## Verification

- Dry-run first against a **copy** of the live store; the live run produced
  identical counts (5 nulled, 1 dropped).
- Re-running the endpoint immediately after returned `nulled=0, dropped=0` —
  idempotent.
- Backup taken before the run:
  `expectancy_pre_repair_1786138499.json` in the session tmp directory.
- `tests/test_store_repair.py` covers six properties, including that two
  different pairs sharing a P/L are a coincidence and must **not** be deduped.

## Reading older reports

Any figure quoting gridzilla at **$86.17/trade** or the fleet at **7 trades**
predates this repair and was computed over a double-counted row. The
`entry_price: None` values now in the store are honest absence — the bot
reported P/L without prices — not a defect, and `r_multiple` is `None` for
exactly that reason.
