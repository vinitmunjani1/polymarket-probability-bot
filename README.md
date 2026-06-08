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

Latency improvements:

- evaluates all assets concurrently
- fetches spot + UP book + DOWN book concurrently per asset
- reuses HTTP connections with pooling
- caches recent Binance klines briefly to avoid repeated slow candle fetches

## Run

```bash
cd polymarket_probability_bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py --once
```

## Live mode

Set `EXECUTION_MODE=live` and wallet/CLOB env vars. The bot will still cap each order intent to `$1` and skip duplicate trades per asset/window.
