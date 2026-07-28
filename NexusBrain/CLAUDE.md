# NexusBrain v1.0 -- Multi-TF Confluence Scanner

## What This Is
Combined crypto signal bot merging best ideas from Aegis-5 + Sonnet-Brain v4.
Multi-timeframe confluence scoring, cross-exchange price validation, regime detection,
paper trading with realistic slippage + fees, SQLite persistence.
Zero external dependencies -- pure Python stdlib.

## Architecture
- `nexus_brain.py` -- Everything in one file. CLI with multiple commands.
- `dashboard.html` -- Self-contained HTML dashboard
- Dashboard served on port 8074 via `python nexus_brain.py dashboard`
- SQLite database: `nexus_brain.db` (signals, trades, state)

## Run
```
cd D:\NexusBrain
python nexus_brain.py dashboard --auto          # dashboard + backtest
python nexus_brain.py scan --max-pairs 10       # live signal scan
python nexus_brain.py backtest --days 90        # walk-forward backtest
python nexus_brain.py run-sim --days 7          # forward simulation
python nexus_brain.py report                    # DB performance report
python nexus_brain.py health --port 8074        # HTTP health endpoint
```

## Signal System (6 components, multi-TF weighted)
- EMA Alignment (0.25): price > EMA9 > EMA21 > EMA50
- RSI Momentum (0.15): sweet spot 40-65, penalize overbought
- MACD Momentum (0.20): histogram direction + signal cross
- Bollinger Position (0.10): %B positioning
- Volume Confirmation (0.10): relative to 20-period SMA
- Regime Alignment (0.20): TREND_UP preferred for longs

## Multi-Timeframe Weights
- 5m (0.10), 15m (0.15), 1h (0.25), 4h (0.30), 1d (0.20)

## Exchanges
- Kraken (primary): OHLC + ticker via canonical fleet helper
- CoinGecko (secondary): price cross-validation only — informational, not load-bearing
  (kept after the 2026-04-10 third-party-data audit because removing it would
  disable the dual-source sanity check; see project_coingecko_keep_decision.md)

## Risk Management
- Max 5 concurrent positions
- 10% equity per position (confidence-weighted 0.5x-1.0x)
- 2x ATR stop loss, 3x ATR take profit
- 3% daily loss limit
- 1h cooldown after stop loss hit
- 0.1% taker fee + 5bps slippage simulation

## Port Convention
- NexusBrain: 8074 (this bot)
- Full fleet port map: see `D:\CommandCenter\fleet_config.py` — canonical source of truth.
  Don't duplicate the table here; it drifts.

## Working Rules
- NEVER overwrite working code based on assumptions
- ALWAYS read existing code before modifying
- No fake stats -- ever
