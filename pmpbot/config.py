from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_: Any, **__: Any) -> None:
        return None


@dataclass(frozen=True)
class Settings:
    assets: list[str]
    window_seconds: int = 300
    execution_mode: str = "dry-run"
    order_notional_usd: float = 1.0
    dry_capital_per_asset_usd: float = 10.0
    dry_charge_rate_bps: float = 0.0
    dry_fixed_charge_usd: float = 0.0
    min_signal_price: float = 0.80
    trade_trigger_mode: str = "signal"
    max_orders_per_market_window: int = 1
    min_edge_cents: float = 3.0
    max_spread_cents: float = 3.0
    min_time_remaining_seconds: int = 20
    max_time_remaining_seconds: int = 260
    min_entry_price: float = 0.80
    max_entry_price: float = 0.90
    state_path: Path = Path("state.json")
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"
    binance_api: str = "https://api.binance.com"
    http_timeout_seconds: float = 4.0

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        assets = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL,XRP").split(",") if a.strip()]
        return cls(
            assets=assets,
            window_seconds=int(os.getenv("WINDOW_SECONDS", "300")),
            execution_mode=os.getenv("EXECUTION_MODE", "dry-run").strip().lower(),
            order_notional_usd=float(os.getenv("ORDER_NOTIONAL_USD", "1.0")),
            dry_capital_per_asset_usd=float(os.getenv("DRY_CAPITAL_PER_ASSET_USD", "10.0")),
            dry_charge_rate_bps=float(os.getenv("DRY_CHARGE_RATE_BPS", "0.0")),
            dry_fixed_charge_usd=float(os.getenv("DRY_FIXED_CHARGE_USD", "0.0")),
            min_signal_price=float(os.getenv("MIN_SIGNAL_PRICE", "0.80")),
            trade_trigger_mode=os.getenv("TRADE_TRIGGER_MODE", "signal").strip().lower(),
            max_orders_per_market_window=int(os.getenv("MAX_ORDERS_PER_MARKET_WINDOW", "1")),
            min_edge_cents=float(os.getenv("MIN_EDGE_CENTS", "3.0")),
            max_spread_cents=float(os.getenv("MAX_SPREAD_CENTS", "3.0")),
            min_time_remaining_seconds=int(os.getenv("MIN_TIME_REMAINING_SECONDS", "20")),
            max_time_remaining_seconds=int(os.getenv("MAX_TIME_REMAINING_SECONDS", "260")),
            min_entry_price=float(os.getenv("MIN_ENTRY_PRICE", "0.80")),
            max_entry_price=float(os.getenv("MAX_ENTRY_PRICE", "0.90")),
            state_path=Path(os.getenv("STATE_PATH", "state.json")),
            gamma_api=os.getenv("GAMMA_API", "https://gamma-api.polymarket.com"),
            clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
            binance_api=os.getenv("BINANCE_API", "https://api.binance.com"),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "4.0")),
        )
