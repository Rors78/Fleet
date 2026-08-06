# TurtleSue -- TurtleBot ULTRA

## What This Is
Paper trading bot implementing the **original 1983 Turtle Trading rules** by Richard Dennis & William Eckhardt, adapted for crypto markets via Kraken public API.

NOT a real trading bot. No API keys. No real money. Paper mode only.

## Architecture
- `turtlebot.py` -- Everything in one file. Run with `python turtlebot.py`
- `dashboard.html` -- Self-contained HTML dashboard (no build step, no npm, no dependencies)
- Dashboard served by built-in HTTP server on **port 8070** as a daemon thread inside turtlebot.py
- API endpoint: `GET /api/snapshot` returns full engine state as JSON
- TUI: ANSI escape codes (Pydroid3 compatible), no Rich dependency

## Turtle Rules Implemented
Crypto-adapted values are marked; everything else is the 1983 original. The
rationale for each adaptation is in the CONFIG block in `turtlebot.py`.

- System 1: 20-day Donchian breakout entry, 10-day contrary exit, winner filter + 55-day failsafe
- System 2: 55-day breakout entry, 20-day contrary exit, take all signals
- System 3 (crypto addition): 40-day breakout entry, 15-day exit
- Position sizing: **0.5%** of the sizing basis / N (crypto: halved from 1%, since
  crypto daily ATR is 3-8% vs commodities' 1-3%)
- Pyramiding: up to 4 units at 1/2 N intervals
- Stops: **2.5N** from entry (crypto: raised from 2N — crypto wicks routinely spike
  2N intraday then reverse), raised 1/2 N per unit added
- Risk limits: 4/market, **4**/closely correlated, **8**/loosely correlated,
  **8**/direction (crypto: tightened from 6/10/12 — BTC/ETH/L1 correlation is 0.85+)
- Drawdown: -20% notional per -10% equity loss
- Strength ranking: buy strongest, sell weakest

## Sizing Basis (2026-08-05)
Position sizing is calculated from a **share of the Command Center capital pool**
(`equity_pool_share_pct`, currently 10% of $1,000,000 = $100,000), NOT from
`starting_equity`. Before this, the bot sized a $10k account while drawing on a
$1M pool — $50 risk per unit, 0.005% of the pool.

`self.equity` remains this bot's own realized-P/L ledger and still drives the
drawdown rule, the equity curve and the dashboard. The two are deliberately
separate: conflating them would apply the drawdown reduction twice.

If Command Center is unreachable, sizing falls back to local equity and says so
in `sizing_basis_source` and in `errors` — it never sizes off a guess.

## Data
- Kraken public OHLC API (no auth needed)
- 10 crypto pairs configured: BTC, ETH, SOL, LTC, LINK, XLM, XRP, DOT, AAVE, UNI
- **8 actually trade** — SOL/USD and DOT/USD are filtered by the fleet-wide
  blacklist in `fleet_config.BLACKLISTED_PAIRS` (0% WR across other bots).
  TurtleSue never traded either; the ban came from rubberband/trekbot/nexusbrain
  on 5 and 1 trades respectively.
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
