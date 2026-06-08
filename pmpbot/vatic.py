from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class VaticPriceFeed:
    """Fetch Polymarket's Chainlink/Vatic price-to-beat for crypto windows."""

    def __init__(self, http: httpx.AsyncClient, base_url: str = "https://api.vatic.trading", ttl_seconds: float = 300.0):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}
        self._locks: dict[tuple[str, int, str], asyncio.Lock] = {}

    async def price_to_beat(self, asset: str, window_start: int, market_type: str = "5min") -> dict[str, Any] | None:
        key = (asset.lower(), int(window_start), market_type)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] <= self.ttl_seconds:
            return cached[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            now = time.time()
            if cached and now - cached[0] <= self.ttl_seconds:
                return cached[1]
            r = await self.http.get(
                f"{self.base_url}/api/v1/targets/timestamp",
                params={"asset": asset.lower(), "type": market_type, "timestamp": int(window_start)},
            )
            r.raise_for_status()
            data = r.json()
            price = float(data.get("price") or 0)
            if price <= 0:
                return None
            result = {
                "price": price,
                "source": data.get("source") or data.get("provider") or "vatic_chainlink",
                "provider": data.get("provider"),
                "requested_timestamp": data.get("requestedTimestamp"),
                "window_start": (data.get("_cache") or {}).get("windowStart", window_start),
                "raw": data,
            }
            self._cache[key] = (now, result)
            return result
