# Polymarket Probability Bot v0

Trades Polymarket 5-minute crypto Up/Down markets using a fair-probability edge gate.

Default mode is `dry-run`. Live mode requires explicit env config and review.

Core v0 guardrail: **at most $1 notional per asset per 5-minute market window**.

## Fast-path design

The bot is split into focused modules so the 5-minute market loop stays fast:

- `bot.py` — tiny CLI entrypoint
- `pmpbot/engine.py` — orchestration/event loop
- `pmpbot/market_discovery.py` — Gamma market lookup
- `pmpbot/clob.py` — CLOB order book + live execution
- `pmpbot/spot.py` — Binance spot features and fair probability
- `pmpbot/strategy.py` — candidate selection and trade gates
- `pmpbot/state.py` — one-order-per-asset-window guardrail
- `pmpbot/config.py` — env-driven settings

Trading rule v0:

- wait until chosen side ask is at least `MIN_SIGNAL_PRICE=0.80`
- buy immediately at the current ask using market-style execution
- skip any candidate above `MAX_ENTRY_PRICE=0.90`
- never trade above `MAX_ENTRY_PRICE=0.90`
- dry capital starts at `$10` per asset and overall equity updates with PnL

Price-to-beat:

- uses Vatic/Chainlink target API as the Polymarket price-to-beat
- does not use Binance candle open as the resolution boundary
- Binance remains a fast current spot / volatility input

Latency improvements:

- evaluates all assets concurrently
- fetches spot + UP book + DOWN book concurrently per asset
- reuses HTTP connections with pooling
- caches recent Binance klines briefly to avoid repeated slow candle fetches

## Run CLI bot

```bash
cd polymarket_probability_bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py --once
```

Continuous dry-run:

```bash
python bot.py
```

## Run dashboard

The dashboard starts the bot engine in the background and streams read-only events to the browser.

```bash
uvicorn pmpbot.dashboard:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

Dashboard v0 includes:

- top health/status bar
- BTC/ETH/SOL/XRP market cards
- bid/ask, fair probability, edge, spread, spot/price-to-beat distance
- trade/skip decision and reason
- latency per asset cycle
- event log
- current config view

## Live mode

Set `EXECUTION_MODE=live` and wallet/CLOB env vars. The bot will still cap each order intent to `$1` and skip duplicate trades per asset/window.
