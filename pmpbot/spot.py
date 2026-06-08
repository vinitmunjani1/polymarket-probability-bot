from __future__ import annotations

import asyncio
import math
import time
from statistics import pstdev

import httpx

from .models import BINANCE_SYMBOL, SpotFeatures
from .utils import normal_cdf


class SpotModel:
    """Fast spot feature/fair-probability service.

    Keeps a short TTL cache for klines so the hot path does not refetch 30 candles
    on every loop for every asset.
    """

    def __init__(self, http: httpx.AsyncClient, binance_api: str, kline_ttl_seconds: float = 10.0):
        self.http = http
        self.binance_api = binance_api.rstrip("/")
        self.kline_ttl_seconds = kline_ttl_seconds
        self._kline_cache: dict[str, tuple[float, list]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def features(self, asset: str, start: int, seconds_left: float) -> SpotFeatures:
        symbol = BINANCE_SYMBOL[asset]
        ticker_task = asyncio.create_task(self._ticker(symbol))
        klines_task = asyncio.create_task(self._klines(symbol))
        spot, rows = await asyncio.gather(ticker_task, klines_task)
        open_px = self._window_open(rows, start) or float(rows[-1][1])
        closes = [float(r[4]) for r in rows]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        sigma_per_sec = (pstdev(rets[-10:]) if len(rets) >= 3 else 0.0005) / math.sqrt(60)
        sigma_per_sec = max(sigma_per_sec, 1e-6)
        momentum = (math.log(closes[-1] / closes[-4]) / 180) if len(closes) >= 4 else 0.0
        distance = math.log(spot / open_px)
        z = (distance + momentum * seconds_left) / (sigma_per_sec * math.sqrt(max(seconds_left, 1.0)))
        p_up = min(0.995, max(0.005, normal_cdf(z)))
        return SpotFeatures(spot=spot, open=open_px, p_up=p_up, p_down=1.0 - p_up, z=z)

    async def _ticker(self, symbol: str) -> float:
        r = await self.http.get(f"{self.binance_api}/api/v3/ticker/price", params={"symbol": symbol})
        r.raise_for_status()
        return float(r.json()["price"])

    async def _klines(self, symbol: str) -> list:
        cached = self._kline_cache.get(symbol)
        now = time.time()
        if cached and now - cached[0] <= self.kline_ttl_seconds:
            return cached[1]
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            cached = self._kline_cache.get(symbol)
            now = time.time()
            if cached and now - cached[0] <= self.kline_ttl_seconds:
                return cached[1]
            r = await self.http.get(f"{self.binance_api}/api/v3/klines", params={"symbol": symbol, "interval": "1m", "limit": 30})
            r.raise_for_status()
            rows = r.json()
            self._kline_cache[symbol] = (now, rows)
            return rows

    @staticmethod
    def _window_open(rows: list, start: int) -> float | None:
        for row in rows:
            if int(row[0]) // 1000 <= start < int(row[6]) // 1000:
                return float(row[1])
        return None
