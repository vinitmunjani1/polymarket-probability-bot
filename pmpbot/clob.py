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

    @staticmethod
    def _signature_type(raw: str | None):
        from py_clob_client_v2 import SignatureTypeV2

        value = (raw or "0").strip().upper()
        aliases = {
            "": SignatureTypeV2.EOA,
            "0": SignatureTypeV2.EOA,
            "EOA": SignatureTypeV2.EOA,
            "1": SignatureTypeV2.POLY_PROXY,
            "POLY_PROXY": SignatureTypeV2.POLY_PROXY,
            "PROXY": SignatureTypeV2.POLY_PROXY,
            "MAGIC": SignatureTypeV2.POLY_PROXY,
            "EMAIL": SignatureTypeV2.POLY_PROXY,
            "2": SignatureTypeV2.POLY_GNOSIS_SAFE,
            "POLY_GNOSIS_SAFE": SignatureTypeV2.POLY_GNOSIS_SAFE,
            "GNOSIS_SAFE": SignatureTypeV2.POLY_GNOSIS_SAFE,
            "SAFE": SignatureTypeV2.POLY_GNOSIS_SAFE,
            "3": SignatureTypeV2.POLY_1271,
            "POLY_1271": SignatureTypeV2.POLY_1271,
            "1271": SignatureTypeV2.POLY_1271,
            "EIP_1271": SignatureTypeV2.POLY_1271,
            "EIP1271": SignatureTypeV2.POLY_1271,
        }
        if value not in aliases:
            raise RuntimeError(f"unsupported POLYMARKET_SIGNATURE_TYPE={raw!r}; use EOA/0, POLY_PROXY/1, POLY_GNOSIS_SAFE/2, or POLY_1271/3")
        return aliases[value]

    def _client_or_create(self):
        if self._client:
            return self._client
        from py_clob_client_v2 import ApiCreds, ClobClient

        key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        funder = os.getenv("POLYMARKET_FUNDER", "").strip()
        sig = self._signature_type(os.getenv("POLYMARKET_SIGNATURE_TYPE", "POLY_PROXY"))
        chain_id = int(os.getenv("CHAIN_ID", "137"))
        if not key:
            raise RuntimeError("missing POLYMARKET_PRIVATE_KEY for live mode")
        if sig.name != "EOA" and not funder:
            raise RuntimeError("missing POLYMARKET_FUNDER for non-EOA live signature mode")
        client_kwargs = {"host": self.clob_host, "chain_id": chain_id, "key": key, "signature_type": sig}
        if funder:
            client_kwargs["funder"] = funder
        tmp = ClobClient(**client_kwargs)
        cred_path = Path(os.getenv("CLOB_API_CREDS_PATH", "clob_api_creds_v2.json"))
        if cred_path.exists():
            c = json.loads(cred_path.read_text())
            creds = ApiCreds(
                c.get("apiKey") or c.get("api_key"),
                c.get("secret") or c.get("api_secret"),
                c.get("passphrase") or c.get("api_passphrase"),
            )
        else:
            raw = tmp.create_or_derive_api_key()
            creds = ApiCreds(raw.api_key, raw.api_secret, raw.api_passphrase)
            cred_path.write_text(json.dumps({"api_key": raw.api_key, "api_secret": raw.api_secret, "api_passphrase": raw.api_passphrase}, indent=2))
        self._client = ClobClient(**client_kwargs, creds=creds, retry_on_error=True)
        return self._client

    def buy(self, token_id: str, price: float) -> dict[str, Any]:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side

        amount = math.floor(self.max_notional_usd * 10000) / 10000
        if amount <= 0 or amount > 1.0001:
            raise RuntimeError(f"invalid $1-capped buy amount={amount}")
        return self._client_or_create().create_and_post_market_order(
            MarketOrderArgs(token_id=token_id, amount=amount, side=Side.BUY, price=price, order_type=OrderType.FOK),
            order_type=OrderType.FOK,
        )

    def sell(self, token_id: str, price: float, size: float) -> dict[str, Any]:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side

        size = math.floor(float(size) * 10000) / 10000
        if size <= 0:
            raise RuntimeError(f"invalid sell size={size}")
        return self._client_or_create().create_and_post_market_order(
            MarketOrderArgs(token_id=token_id, amount=size, side=Side.SELL, price=price, order_type=OrderType.FAK),
            order_type=OrderType.FAK,
        )
