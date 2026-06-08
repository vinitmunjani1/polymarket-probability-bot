from __future__ import annotations

import json
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"orders": {}}
        if path.exists():
            self.data = json.loads(path.read_text())

    def count(self, asset: str, window: int) -> int:
        return int(self.data.setdefault("orders", {}).get(f"{asset}:{window}", 0))

    def record(self, asset: str, window: int) -> None:
        key = f"{asset}:{window}"
        orders = self.data.setdefault("orders", {})
        orders[key] = int(orders.get(key, 0)) + 1
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
