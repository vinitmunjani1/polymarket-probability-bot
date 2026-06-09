from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"orders": {}, "positions": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text()))
        self.data.setdefault("orders", {})
        self.data.setdefault("positions", {})

    def count(self, asset: str, window: int) -> int:
        return int(self.data.setdefault("orders", {}).get(f"{asset}:{window}", 0))

    def position(self, asset: str, window: int) -> dict[str, Any] | None:
        return self.data.setdefault("positions", {}).get(f"{asset}:{window}")

    def asset_open_notional(self, asset: str) -> float:
        return sum(
            float(p.get("notional_usd", 0.0))
            for p in self.data.setdefault("positions", {}).values()
            if p.get("asset") == asset and p.get("status", "open") == "open"
        )

    def record_order(self, asset: str, window: int) -> None:
        key = f"{asset}:{window}"
        orders = self.data.setdefault("orders", {})
        orders[key] = int(orders.get(key, 0)) + 1
        self.save()

    def record_position(self, *, asset: str, window: int, slug: str, side: str, token_id: str, entry_price: float, notional_usd: float, charges_usd: float, status: str = "open", trigger_price: float | None = None) -> dict[str, Any]:
        key = f"{asset}:{window}"
        position = {
            "asset": asset,
            "window": window,
            "slug": slug,
            "side": side,
            "token_id": token_id,
            "entry_price": float(entry_price),
            "notional_usd": float(notional_usd),
            "shares": float(notional_usd) / max(float(entry_price), 1e-9),
            "charges_usd": float(charges_usd),
            "status": status,
            "trigger_price": trigger_price,
        }
        self.data.setdefault("positions", {})[key] = position
        self.save()
        return position

    def close_position(self, asset: str, window: int, *, exit_price: float, exit_reason: str, charges_usd: float = 0.0) -> dict[str, Any]:
        key = f"{asset}:{window}"
        position = self.data.setdefault("positions", {}).get(key)
        if not position:
            raise KeyError(f"missing position {key}")
        shares = float(position.get("shares", 0.0))
        notional = float(position.get("notional_usd", 0.0))
        total_charges = float(position.get("charges_usd", 0.0)) + float(charges_usd)
        exit_value = shares * float(exit_price)
        position.update({
            "status": "closed",
            "exit_price": float(exit_price),
            "exit_value_usd": exit_value,
            "exit_reason": exit_reason,
            "charges_usd": total_charges,
            "pnl_usd": exit_value - notional - total_charges,
        })
        self.save()
        return position

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
