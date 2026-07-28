# TurtleSue -- TurtleBot ULTRA

## What This Is
Paper trading bot implementing the **original 1983 Turtle Trading rules** by Richard Dennis & William Eckhardt, adapted for crypto markets via Kraken public API.

NOT a real trading bot. No API keys. No real money. Paper mode only.

## Architecture
- `turtlebot.py` -- Everything in one file. Run with `python turtlebot.py`
- `dashboard.html` -- Self-contained HTML dashboard (no build step, no npm, no dependencies)
- Dashboard served by built-in HTTP server on port 8050 as a daemon thread inside turtlebot.py
- API endpoint: `GET /api/snapshot` returns full engine state as JSON
- TUI: ANSI escape codes (Pydroid3 compatible), no Rich dependency

## Turtle Rules Implemented
- System 1: 20-day Donchian breakout entry, 10-day contrary exit, winner filter + 55-day failsafe
- System 2: 55-day breakout entry, 20-day contrary exit, take all signals
- Position sizing: 1% equity / N (volatility-normalized units)
- Pyramiding: up to 4 units at 1/2 N intervals
- Stops: 2N from entry, raised to 2N from most recent unit add
- Risk limits: 4/market, 6/closely correlated, 10/loosely correlated, 12/direction
- Drawdown: -20% notional per -10% equity loss
- Strength ranking: buy strongest, sell weakest

## Data
- Kraken public OHLC API (no auth needed)
- 10 crypto pairs: BTC, ETH, SOL, LTC, LINK, XLM, XRP, DOT, AAVE, UNI
- Daily candles (1440 min), 120-day lookback
- Rate limited: 1.5s between API calls

## Port Convention
- TurtleSue: 8070
- Viper: 8071
- Trinity: 8072
- HiveMind: 8073
- Next project: 8074

## Working Rules
- NEVER overwrite working code based on assumptions
- ALWAYS read existing code before modifying
- No fake stats -- ever
- No dependencies beyond Python stdlib
