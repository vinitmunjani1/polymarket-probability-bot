from __future__ import annotations

import time
from dataclasses import dataclass

ASSET_PREFIX = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
BINANCE_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}


@dataclass(frozen=True)
class Market:
    asset: str
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    outcomes: list[str]
    start_ts: int
    end_ts: float

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.end_ts - time.time())


@dataclass(frozen=True)
class Book:
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class SpotFeatures:
    spot: float
    open: float
    p_up: float
    p_down: float
    z: float

    def as_dict(self) -> dict[str, float]:
        return {"spot": self.spot, "open": self.open, "p_up": self.p_up, "p_down": self.p_down, "z": self.z}
