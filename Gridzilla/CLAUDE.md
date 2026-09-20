# Gridzilla -- Adaptive Grid Engine

> **Doc drift:** the sections below describe a "v11.0" Bollinger/5-level
> design. The code on disk is **v2 adaptive** (`GridArchitect` detects range
> from S/R with a Bollinger fallback, ATR-floored width; 5-25 lines). Trust
> the code, not this heading, until someone reconciles them.

## Live arming (read this before anything touching orders)

Gridzilla CAN place real Kraken spot orders. `GridExecutor.check_fills()`
calls `KrakenSpotClient.buy()/.sell()` (marketable limits, never market) via
`D:\CommandCenter\kraken_client.py`.

**Three predicates must ALL be true for a real order** (`fleet_config.is_bot_live`):

1. `FLEET_MODE == "live"`   -- shared fleet switch, POST /api/fleet/mode
2. `FLEET_ENGAGE_STATE == "live_engaged"`  -- second stage of the same API
3. `"gridzilla" in LIVE_ARMED_BOTS`  -- set `GRIDZILLA_LIVE_ARM=1` in this
   bot's own launcher environment

Any one false = simulated fill. As of 2026-09-19 all three are false.

Predicate 3 exists because 1 and 2 are FLEET-WIDE: seven bots read the same
switch, so flipping it used to arm every credentialed bot at once. Arming is
deliberately an env var on the launcher, not an API call -- no single HTTP
request can take money live.

The gate is checked at startup AND at both trade sites, because the executor
binds its Kraken client once at construction and a mode change under a running
process must not arm it. Run `python test_arming.py` after touching any of it.

A rejected live order is NOT booked as a fill (fixed 2026-09-19). It used to
log "using paper fill" and mark the level filled, which would report realised
P/L on trades the exchange refused.

## What This Is
Grid trading bot with volatility-scaled position sizing, multi-timeframe Bollinger Band entries,
and proper risk management. Paper trading on Kraken.
Rebuilt from scratch -- stripped the gambler's-fallacy streak multiplier, fixed the Bollinger math,
added multi-TF confirmation.

## Architecture
- `gridzilla.py` -- Everything in one file. Run with `python gridzilla.py --auto`
- `dashboard.html` -- Self-contained HTML dashboard
- Port 8077, serves /api/snapshot and dashboard
- State persistence to gridzilla_state.json
- Decimal precision for all money math

## Trading Logic
- Volatility-scaled sizing: 1% equity risk per trade, stop at 2x ATR
- Real Bollinger Bands (proper std dev, not mean absolute deviation)
- 5 grid levels per pair (lower BB to upper BB)
- Multi-TF: 5m BB for signals, 1h SMA trend filter, 4h ADX regime detection
- Grid best in RANGING regime; skips entries in strong TRENDING

## Risk Management
- 6% max portfolio risk, 2% max per position
- 2x ATR stop loss
- Trailing stop: activates at 1.5x ATR profit, trails at 1x ATR
- Max 8 concurrent positions

## Pairs
Fetches from fleet universe (http://127.0.0.1:9000/api/universe), falls back to 12 hardcoded Kraken pairs.

## Port Convention
- TurtleSue: 8070
- Viper: 8071
- Trinity: 8072
- HiveMind: 8073
- NexusBrain: 8074
- Oracle: 8075
- Deep Blue: 8076
- Gridzilla: 8077
- Command Center: 9000

## Working Rules
- NEVER overwrite working code based on assumptions
- ALWAYS read existing code before modifying
- No fake stats -- ever
