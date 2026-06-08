from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, is_dataclass
from typing import Any

from .config import Settings
from .utils import now_iso


class DashboardState:
    """In-memory read model for the real-time dashboard.

    Keeps only recent events and latest per-asset snapshots. This is intentionally
    lightweight for v0; durable storage can be added later for PnL/history.
    """

    def __init__(self, settings: Settings, max_events: int = 300):
        self.settings = settings
        self.events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.markets: dict[str, dict[str, Any]] = {}
        self.health: dict[str, Any] = {
            "mode": settings.execution_mode,
            "status": "starting",
            "last_update": None,
            "active_assets": settings.assets,
            "max_order_usd": min(1.0, settings.order_notional_usd),
            "max_orders_per_window": settings.max_orders_per_market_window,
            "loop_latency_ms": None,
        }
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, event: dict[str, Any]) -> None:
        event = {"ts": now_iso(), **event} if "ts" not in event else event
        self.events.appendleft(event)
        self.health["last_update"] = event.get("ts")
        self.health["status"] = "running"
        if "latency_ms" in event:
            self.health["loop_latency_ms"] = event["latency_ms"]
        if asset := event.get("asset"):
            self.markets[str(asset)] = event
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "config": self._settings_dict(),
            "markets": self.markets,
            "events": list(self.events)[:100],
        }

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _settings_dict(self) -> dict[str, Any]:
        if is_dataclass(self.settings):
            data = asdict(self.settings)
        else:
            data = dict(self.settings.__dict__)
        data["state_path"] = str(data.get("state_path"))
        return data
