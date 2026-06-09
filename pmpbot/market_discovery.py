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

    async def market_by_slug(self, slug: str) -> dict | None:
        """Fetch the latest Gamma market payload for an existing slug.

        This is intentionally separate from discover(): after a 5m window ends
        the bot moves to the next slug, but any open dry/live position from the
        old slug still needs to be settled from Gamma resolution data.
        """
        try:
            r = await self.http.get(f"{self.gamma_api}/markets/slug/{slug}", params={"_t": int(time.time())})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            raw = r.json()
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            return raw if isinstance(raw, dict) else None
        except Exception as e:
            print(json.dumps({"ts": now_iso(), "event": "market_fetch_error", "slug": slug, "error": str(e)}))
            return None

    @staticmethod
    def resolved_token_price(raw: dict, *, token_id: str | None = None, side: str | None = None) -> float | None:
        """Return terminal settlement value (1.0 or 0.0) for a token/side.

        CLOB books often stop before the market is fully resolved and can leave
        the last bid near 0.98/0.99. For PnL we only settle when Gamma reports a
        terminal outcome price, so an 80c winning share closes at $1 rather than
        at the final tradable bid.
        """
        token_ids = raw.get("clobTokenIds", [])
        outcomes = raw.get("outcomes", [])
        prices = raw.get("outcomePrices", [])
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids or "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes or "[]")
        if isinstance(prices, str):
            prices = json.loads(prices or "[]")

        idx = None
        if token_id and token_id in token_ids:
            idx = token_ids.index(token_id)
        elif side:
            wanted = side.lower()
            aliases = {"up": {"up", "yes"}, "down": {"down", "no"}}.get(wanted, {wanted})
            for i, outcome in enumerate(outcomes):
                if str(outcome).lower() in aliases:
                    idx = i
                    break
        if idx is None or idx >= len(prices):
            return None

        numeric_prices = [float(p) for p in prices]
        if not numeric_prices:
            return None

        # Do not treat 0.98/0.02 as resolved. Wait until Gamma has terminal
        # prices, then normalize to exact binary settlement values.
        has_winner = any(p >= 0.999 for p in numeric_prices)
        has_loser = any(p <= 0.001 for p in numeric_prices)
        if not (has_winner and has_loser):
            return None
        return 1.0 if numeric_prices[idx] >= 0.999 else 0.0

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
