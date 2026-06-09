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
        self.positions: dict[str, dict[str, Any]] = {}
        self.health: dict[str, Any] = {
            "mode": settings.execution_mode,
            "status": "starting",
            "last_update": None,
            "active_assets": settings.assets,
            "max_order_usd": min(1.0, settings.order_notional_usd),
            "max_orders_per_window": settings.max_orders_per_market_window,
            "loop_latency_ms": None,
        }
        self.pnl: dict[str, Any] = {
            "overall_starting_capital_usd": settings.dry_capital_per_asset_usd * len(settings.assets),
            "overall_equity_usd": settings.dry_capital_per_asset_usd * len(settings.assets),
            "overall_cash_balance_usd": settings.dry_capital_per_asset_usd * len(settings.assets),
            "overall_settled_cash_usd": settings.dry_capital_per_asset_usd * len(settings.assets),
            "overall_used_capital_usd": 0.0,
            "overall_available_capital_usd": settings.dry_capital_per_asset_usd * len(settings.assets),
            "overall_pnl_usd": 0.0,
            "overall_charges_usd": 0.0,
            "assets": {},
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
            asset_key = str(asset)
            self.markets[asset_key] = self._merge_market_event(asset_key, event)
            if position := self.markets[asset_key].get("position"):
                position_key = f"{position.get('asset', asset_key)}:{position.get('window', event.get('window', 'unknown'))}"
                self.positions[position_key] = position
            self._recompute_pnl()
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
            "positions": self.positions,
            "pnl": self.pnl,
            "events": list(self.events)[:100],
        }

    def load_positions(self, positions: dict[str, dict[str, Any]]) -> None:
        """Seed dashboard PnL from persisted dry-run state on startup."""
        self.positions.update(positions or {})
        self._recompute_pnl()

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

    def _merge_market_event(self, asset: str, event: dict[str, Any]) -> dict[str, Any]:
        """Keep the dashboard useful when late-window books temporarily vanish.

        Near expiry Polymarket can return empty books. Those events are sparse
        (`skip_empty_books`, `skip_no_market`, etc.). If we replace the latest
        rich market snapshot with that sparse event, the card becomes blank even
        though the last good price/position is still the best available display.
        Preserve rich fields and overlay the latest status/reason/latency.
        """
        previous = self.markets.get(asset)
        if not previous:
            return event

        sparse_events = {"skip_empty_books", "skip_no_market", "skip_time_gate"}
        rich_keys = {"bid", "ask", "fair", "edge", "features", "position", "price_to_beat"}
        is_sparse = event.get("event") in sparse_events and not any(k in event for k in rich_keys)
        if not is_sparse:
            return event

        preserved = {**previous, **event}
        preserved["stale_market_data"] = True
        preserved["stale_reason"] = event.get("event")
        preserved["last_good_ts"] = previous.get("ts")
        return preserved

    def _recompute_pnl(self) -> None:
        assets: dict[str, Any] = {}
        total_pnl = 0.0
        total_charges = 0.0
        total_used = 0.0
        for asset in self.settings.assets:
            asset_positions = [p for p in self.positions.values() if p.get("asset") == asset]
            pnl = sum(float(p.get("pnl_usd") or 0.0) for p in asset_positions)
            charges = sum(float(p.get("charges_usd") or 0.0) for p in asset_positions)
            used = sum(float(p.get("notional_usd") or 0.0) for p in asset_positions if p.get("status", "open") == "open")
            realized_pnl = sum(float(p.get("pnl_usd") or 0.0) for p in asset_positions if p.get("status") == "closed")
            cash = self.settings.dry_capital_per_asset_usd + realized_pnl - used
            equity = self.settings.dry_capital_per_asset_usd + pnl
            total_used += used
            assets[asset] = {
                "capital_usd": self.settings.dry_capital_per_asset_usd,
                "equity_usd": equity,
                "cash_balance_usd": cash,
                "settled_cash_usd": self.settings.dry_capital_per_asset_usd + realized_pnl,
                "used_capital_usd": used,
                "available_capital_usd": max(0.0, cash),
                "pnl_usd": pnl,
                "charges_usd": charges,
                "positions": asset_positions,
                "position": asset_positions[-1] if asset_positions else None,
            }
            total_pnl += pnl
            total_charges += charges
        total_capital = self.settings.dry_capital_per_asset_usd * len(self.settings.assets)
        self.pnl = {
            "overall_starting_capital_usd": total_capital,
            "overall_equity_usd": total_capital + total_pnl,
            "overall_cash_balance_usd": sum(float(a.get("cash_balance_usd") or 0.0) for a in assets.values()),
            "overall_settled_cash_usd": total_capital + sum(
                float(p.get("pnl_usd") or 0.0)
                for p in self.positions.values()
                if p.get("status") == "closed"
            ),
            "overall_used_capital_usd": total_used,
            "overall_available_capital_usd": max(0.0, sum(float(a.get("cash_balance_usd") or 0.0) for a in assets.values())),
            "overall_pnl_usd": total_pnl,
            "overall_charges_usd": total_charges,
            "assets": assets,
        }
