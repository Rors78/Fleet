# Gridzilla v11.0 -- Volatility Grid Engine

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
