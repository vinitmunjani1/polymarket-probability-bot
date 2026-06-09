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

- only place new entries when the market has at most
  `MAX_TIME_REMAINING_SECONDS=120` seconds left; if price touches `0.80`
  earlier, the bot waits
- wait until chosen side ask is at least `MIN_SIGNAL_PRICE=0.80`
- require signal trades to be confirmed by non-negative model edge by default,
  avoiding sudden side-reversal/chase entries
- buy immediately using py-clob-client-v2 market-order execution in live mode
- skip any candidate above `MAX_ENTRY_PRICE=0.90`
- never trade above `MAX_ENTRY_PRICE=0.90`
- sell immediately if held side bid/mark falls to `STOP_LOSS_PRICE=0.49` or lower
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

Capital accounting:

- when an order is opened, its notional is moved from available cash to used capital
- when the market resolves or stop-loss closes, the settlement/sale value returns
  to available cash
- realized PnL is therefore reflected in the next available-cash balance

## Live mode

Set `EXECUTION_MODE=live` and wallet/CLOB env vars. The bot will still cap each order intent to `$1` and skip duplicate trades per asset/window.

Live execution uses `py-clob-client-v2`:

- buys use FOK market orders for the configured USDC notional
- stop-loss sells use FAK market orders for held shares
- supported signature modes: `EOA`/`0`, `POLY_PROXY`/`1`, `POLY_GNOSIS_SAFE`/`2`, `POLY_1271`/`3`

Example v2 / EIP-1271-style config:

```env
EXECUTION_MODE=live
ORDER_NOTIONAL_USD=1.00
ASSETS=BTC
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_FUNDER=...
POLYMARKET_SIGNATURE_TYPE=POLY_1271
CLOB_API_CREDS_PATH=./clob_api_creds_v2.json
```

To make reversal protection stricter, increase:

```env
MIN_CONFIRMATION_EDGE_CENTS=1.0
```

To restore old behavior, set:

```env
REQUIRE_SIGNAL_EDGE_CONFIRMATION=false
```

On startup the CLI/dashboard emits a `settings_loaded` event with the loaded
mode and `.env` path. If it still says `dry-run`, confirm `.env` exists in the
repo root and contains `EXECUTION_MODE=live` exactly.
