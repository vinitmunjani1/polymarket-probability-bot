from __future__ import annotations

import json
import time
from datetime import datetime

import httpx

from .models import ASSET_PREFIX, Market
from .utils import now_iso


class MarketDiscovery:
    def __init__(self, http: httpx.AsyncClient, gamma_api: str):
        self.http = http
        self.gamma_api = gamma_api.rstrip("/")

    async def discover(self, asset: str, start: int) -> Market | None:
        prefix = ASSET_PREFIX.get(asset, asset.lower())
        slugs = [f"{prefix}-updown-5m-{start}", f"{prefix}-up-or-down-5m-{start}"]
        for slug in slugs:
            try:
                r = await self.http.get(f"{self.gamma_api}/markets/slug/{slug}", params={"_t": int(time.time())})
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                raw = r.json()
                if isinstance(raw, list):
                    raw = raw[0] if raw else None
                if not raw or not raw.get("conditionId"):
                    continue
                return self._parse(asset, slug, start, raw)
            except Exception as e:
                print(json.dumps({"ts": now_iso(), "event": "discover_error", "asset": asset, "slug": slug, "error": str(e)}))
        return None

    def _parse(self, asset: str, slug: str, start: int, raw: dict) -> Market:
        token_ids = raw.get("clobTokenIds", "[]")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        outcomes = raw.get("outcomes", '["Up","Down"]')
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        token_by_outcome = {str(o).lower(): token_ids[i] for i, o in enumerate(outcomes[:len(token_ids)])}
        end_dt = datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00"))
        return Market(
            asset=asset,
            slug=slug,
            condition_id=raw["conditionId"],
            token_up=token_by_outcome.get("up") or token_by_outcome.get("yes") or token_ids[0],
            token_down=token_by_outcome.get("down") or token_by_outcome.get("no") or token_ids[1],
            outcomes=outcomes,
            start_ts=start,
            end_ts=end_dt.timestamp(),
        )
