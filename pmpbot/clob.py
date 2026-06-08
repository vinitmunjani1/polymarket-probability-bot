from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import httpx

from .models import Book


class ClobData:
    def __init__(self, http: httpx.AsyncClient, clob_host: str):
        self.http = http
        self.clob_host = clob_host.rstrip("/")

    async def book(self, token_id: str) -> Book | None:
        r = await self.http.get(f"{self.clob_host}/book", params={"token_id": token_id})
        r.raise_for_status()
        data = r.json()
        bids = [float(x["price"]) for x in data.get("bids", [])]
        asks = [float(x["price"]) for x in data.get("asks", [])]
        if not bids or not asks:
            return None
        return Book(bid=max(bids), ask=min(asks))


class LiveExecutor:
    def __init__(self, clob_host: str, max_notional_usd: float):
        self.clob_host = clob_host.rstrip("/")
        self.max_notional_usd = min(1.0, float(max_notional_usd))
        self._client = None

    def _client_or_create(self):
        if self._client:
            return self._client
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON

        key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        funder = os.getenv("POLYMARKET_FUNDER", "").strip()
        sig = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        chain_id = int(os.getenv("CHAIN_ID", str(POLYGON)))
        if not key or not funder:
            raise RuntimeError("missing POLYMARKET_PRIVATE_KEY/POLYMARKET_FUNDER for live mode")
        tmp = ClobClient(self.clob_host, chain_id=chain_id, key=key, signature_type=sig, funder=funder)
        cred_path = Path(os.getenv("CLOB_API_CREDS_PATH", "clob_api_creds.json"))
        if cred_path.exists():
            c = json.loads(cred_path.read_text())
            creds = ApiCreds(c.get("apiKey") or c.get("api_key"), c.get("secret") or c.get("api_secret"), c.get("passphrase") or c.get("api_passphrase"))
        else:
            raw = tmp.create_or_derive_api_creds()
            creds = ApiCreds(raw.api_key, raw.api_secret, raw.api_passphrase)
            cred_path.write_text(json.dumps({"api_key": raw.api_key, "api_secret": raw.api_secret, "api_passphrase": raw.api_passphrase}, indent=2))
        self._client = ClobClient(self.clob_host, chain_id=chain_id, key=key, creds=creds, signature_type=sig, funder=funder)
        return self._client

    def buy(self, token_id: str, price: float) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        size = math.floor((self.max_notional_usd / price) * 10000) / 10000
        if size <= 0 or size * price > 1.0001:
            raise RuntimeError(f"invalid $1-capped size={size} price={price}")
        client = self._client_or_create()
        order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY, expiration=0))
        return client.post_order(order, orderType="GTC", post_only=False)
