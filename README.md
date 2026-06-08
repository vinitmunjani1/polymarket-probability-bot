# Polymarket Probability Bot v0

Trades Polymarket 5-minute crypto Up/Down markets using a fair-probability edge gate.

Default mode is `dry-run`. Live mode requires explicit env config and review.

Core v0 guardrail: **at most $1 notional per asset per 5-minute market window**.

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
