# Confluence — Intel-Driven Fleet Trader

## What This Is

Port 8088. Kraken spot, **LONG-only**, paper-gated. Fills the trader slot left
by TrekBot when it was renamed GoldenEye and moved to standalone operation.

The other five traders (Rubberband, Arbitrageur, Gridzilla, TurtleSue,
NexusBrain) all read **charts**. Confluence reads **the fleet**. It holds no
indicators of its own. Its entire edge is aggregating intel the fleet already
produces but that nothing previously traded on.

## Intel Sources

| Source | Port | Contributes | Weight |
|--------|------|-------------|--------|
| Oracle | 8075 | Scored candidates w/ entry, stop, target, rr, hurst | 0.45 |
| Deep Blue | 8076 | `whaleScore` × `reliability` | 0.25 |
| NEXUS | 8082 | `market_character` (fleet-wide posture) | 0.18 |
| Sentinel | 8071 | 1h expected move, damped by downside tail risk | 0.12 |

Oracle anchors — it's the only source giving a full trade thesis. The rest
confirm or veto. **A candidate backed only by Oracle is rejected** by the
`MIN_SOURCES=2` gate. That's deliberate: acting on Oracle alone would just be
Oracle with extra steps.

## Confluence Scoring

Score is normalized against the weight that actually **contributed**, not the
theoretical all-four maximum and not source *presence*:

```
confluence = weighted_sum / contributed_weight
```

`contributed_w` is accumulated inline next to each `weighted +=` so the two
cannot drift apart. This matters twice over:

- Deep Blue tracks only a handful of pairs fleet-wide, so scoring against the
  full denominator makes the gate mathematically unreachable.
- **Presence is not contribution.** A whale below `MIN_WHALE_SCORE`, or an
  unmapped NEXUS character, is present but adds nothing. Counting it in the
  denominator made a weak signal score strictly *worse* than a missing one
  (0.399 vs 0.557) — backwards. Risk-off NEXUS was penalised twice: numerator
  −0.09 *and* denominator +0.18.

Risk-off NEXUS remains a pure numerator veto by design — it subtracts without
enlarging the denominator.

## Entry Gates (in order)

1. `direction` — LONG only; NEUTRAL/SHORT rejected
2. `oracle_stale` — intel older than 300s
3. `blacklist` — `fleet_config.BLACKLISTED_PAIRS`
4. `oracle_score` — ≥ 55.0
5. `risk_reward` — rr ≥ 1.8
6. `min_sources` — ≥ 2 distinct sources agreeing
7. `confluence` — ≥ 0.55
8. `fee_floor` — target must clear 2× round-trip fees (0.80%)
9. `position_mgmt` — max 3 open, pair/loss cooldowns
10. `portfolio` — CC reservation must succeed

## Calibration Notes

`MIN_CONFLUENCE = 0.55` — basis, 2026-07-28 live intel: with Oracle scores in
the 55-60 band and NEXUS at MIXED, candidates land 0.45-0.56. This admits only
the strongest of a mediocre field. A stronger tape (Oracle 70+, NEXUS TRENDING,
whale present) clears 0.70+ comfortably. **Raise after the first 20-30 closed
trades** give a real win rate to calibrate against.

**Position sizing (rewritten 2026-08-05).** Was a flat `POSITION_SIZE_USD =
550.0`, justified by CC's size floor of 5% of a $9,970 pool. Both halves of
that justification are gone — the pool is $1,000,000 and the floor became an
absolute $100.

A flat size also made *risk* a function of stop distance rather than
conviction. Measured live: risk per position ranged $3.37–$12.65 (nearly 4×)
purely because stops ranged 0.61%–2.30%. Backwards — the tight-stop setup is
usually the higher-confidence one and was taking the smallest risk.

Sizing is now risk-first:

```
basis = pool_total × RISK_POOL_SHARE_PCT      (10% → $100,000)
risk  = basis × RISK_PER_TRADE_PCT            (0.5% → $500)
size  = risk / max(stop_pct, MIN_STOP_PCT_FOR_SIZING)
size  = clamp(size, MIN_POSITION_USD, MAX_POSITION_USD)
```

Every position risks the same $500; the stop decides the notional. Clamps stop
a near-zero stop dividing its way to an enormous position and a huge stop
producing dust. If CC is unreachable, sizing falls back to
`FALLBACK_EQUITY_USD` and says so in `sizing_basis_source` — it never sizes
off a guess.

**P/L is gross.** Trades closed before 2026-07-30 recorded `net = gross − fees`,
and `realized_pnl` accumulated the net, so that fee drag rode forward forever
— the bot reported −$23.69 when its gross P/L was −$14.90. `_load_state` now
restates legacy rows to gross once and marks them `restated_gross`.

## Safety Invariants

- **Live execution is intentionally not implemented.** If `fleet_config.is_live()`
  ever returns True, `try_enter` releases the reservation and skips. This bot has
  never traded real capital and must not silently start.
- Every position reserves from CC before opening and releases on close.
- State written atomically via `.tmp` + `os.replace`.
- All intel fetches are best-effort — a down source degrades scoring, never crashes.
- Scan loop catches and logs full tracebacks; one bad cycle can't kill the bot.

## Gotchas Found During Build

- **Sentinel's `forecasts` is a dict keyed by pair**, not a list. Iterating it
  naively yields key strings and throws `'str' object has no attribute 'get'`.
- **Sentinel records carry no conviction scalar** — only `current`, `1h.expected`,
  `ci_68`, `tail_risk_up/down`, `skew`. Conviction is derived from expected drift
  damped by downside tail risk.
- **Sentinel drift is ~100x smaller than intuition suggests.** The original
  scale treated 1% hourly drift as full conviction; the live distribution
  across 53 pairs is max 0.149%, p90 0.111%, median 0.037%. Nothing ever
  approached the threshold, so Sentinel could never confirm anything. See
  `SENTINEL_FULL_CONVICTION_DRIFT` (0.0012, just above p90).
- **Sentinel only forecasts pairs it knows about.** It ranks by volume while
  Oracle signals on mid-cap alts, so Sentinel was changed to union Oracle's
  live signal pairs into its universe and persist that rotation history
  (`D:\Sentinel\oracle_seen.json`). Without it, Oracle candidates get no
  forecast and Confluence loses its per-pair confirming source.
- **Oracle emits bare symbols** (`CRV`), the fleet speaks `CRV/USD`. Normalized on
  ingest.
- **Unmapped NEXUS characters log a WARNING** rather than silently scoring zero.

## Endpoints

- `/api/snapshot` — full state (CC polls this)
- `/positions`, `/candidates`, `/trades`
