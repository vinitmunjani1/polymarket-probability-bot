#!/usr/bin/env python3
"""Polymarket 5-minute crypto probability bot v0.

Safe defaults:
- dry-run unless EXECUTION_MODE=live
- $1 max order notional per asset/window
- one order max per market/window
- trade only when model fair probability exceeds executable ask by configured edge
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import pstdev
from typing import Any

import httpx
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_: Any, **__: Any) -> None: pass

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
BINANCE_API = "https://api.binance.com"
ASSET_PREFIX = {"BTC": "btc", "ETH": "eth", "SOL": "sol", "XRP": "xrp"}
BINANCE_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}


@dataclass
class Settings:
    assets: list[str]
    window_seconds: int = 300
    execution_mode: str = "dry-run"
    order_notional_usd: float = 1.0
    max_orders_per_market_window: int = 1
    min_edge_cents: float = 3.0
    max_spread_cents: float = 3.0
    min_time_remaining_seconds: int = 20
    max_time_remaining_seconds: int = 260
    min_entry_price: float = 0.70
    max_entry_price: float = 0.95
    state_path: Path = Path("state.json")

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        assets = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL,XRP").split(",") if a.strip()]
        return cls(
            assets=assets,
            window_seconds=int(os.getenv("WINDOW_SECONDS", "300")),
            execution_mode=os.getenv("EXECUTION_MODE", "dry-run").strip().lower(),
            order_notional_usd=float(os.getenv("ORDER_NOTIONAL_USD", "1.0")),
            max_orders_per_market_window=int(os.getenv("MAX_ORDERS_PER_MARKET_WINDOW", "1")),
            min_edge_cents=float(os.getenv("MIN_EDGE_CENTS", "3.0")),
            max_spread_cents=float(os.getenv("MAX_SPREAD_CENTS", "3.0")),
            min_time_remaining_seconds=int(os.getenv("MIN_TIME_REMAINING_SECONDS", "20")),
            max_time_remaining_seconds=int(os.getenv("MAX_TIME_REMAINING_SECONDS", "260")),
            min_entry_price=float(os.getenv("MIN_ENTRY_PRICE", "0.70")),
            max_entry_price=float(os.getenv("MAX_ENTRY_PRICE", "0.95")),
            state_path=Path(os.getenv("STATE_PATH", "state.json")),
        )


@dataclass
class Market:
    asset: str
    slug: str
    condition_id: str
    token_up: str
    token_down: str
    outcomes: list[str]
    start_ts: int
    end_ts: float

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.end_ts - time.time())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def window_start(ts: float, seconds: int) -> int:
    return int(ts // seconds) * seconds


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


class Bot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.state = State(settings.state_path)
        self.http = httpx.AsyncClient(timeout=10.0)
        self.live_client = None

    async def close(self) -> None:
        await self.http.aclose()

    async def discover_market(self, asset: str, start: int) -> Market | None:
        prefix = ASSET_PREFIX.get(asset, asset.lower())
        # Current observed Polymarket crypto format; fallback tries updown-15m-style just in case.
        slugs = [f"{prefix}-updown-5m-{start}", f"{prefix}-up-or-down-5m-{start}"]
        for slug in slugs:
            try:
                r = await self.http.get(f"{GAMMA_API}/markets/slug/{slug}", params={"_t": int(time.time())})
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                raw = r.json()
                if isinstance(raw, list):
                    raw = raw[0] if raw else None
                if not raw or not raw.get("conditionId"):
                    continue
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
            except Exception as e:
                print(json.dumps({"ts": now_iso(), "event": "discover_error", "asset": asset, "slug": slug, "error": str(e)}))
        return None

    async def book(self, token_id: str) -> dict[str, float] | None:
        r = await self.http.get(f"{CLOB_HOST}/book", params={"token_id": token_id})
        r.raise_for_status()
        b = r.json()
        bids = [float(x["price"]) for x in b.get("bids", [])]
        asks = [float(x["price"]) for x in b.get("asks", [])]
        if not bids or not asks:
            return None
        return {"bid": max(bids), "ask": min(asks), "spread": min(asks) - max(bids)}

    async def spot_features(self, asset: str, start: int, seconds_left: float) -> dict[str, float]:
        symbol = BINANCE_SYMBOL[asset]
        ticker = await self.http.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol})
        ticker.raise_for_status()
        spot = float(ticker.json()["price"])

        # Pull recent 1m klines; use the kline containing window start as approximate open.
        kl = await self.http.get(f"{BINANCE_API}/api/v3/klines", params={"symbol": symbol, "interval": "1m", "limit": 30})
        kl.raise_for_status()
        rows = kl.json()
        open_px = None
        for row in rows:
            if int(row[0]) // 1000 <= start < int(row[6]) // 1000:
                open_px = float(row[1])
                break
        if open_px is None:
            open_px = float(rows[-1][1])
        closes = [float(r[4]) for r in rows]
        rets = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes)) if closes[i-1] > 0]
        sigma_per_sec = (pstdev(rets[-10:]) if len(rets) >= 3 else 0.0005) / math.sqrt(60)
        sigma_per_sec = max(sigma_per_sec, 1e-6)
        momentum = (math.log(closes[-1] / closes[-4]) / 180) if len(closes) >= 4 else 0.0
        distance = math.log(spot / open_px)
        z = (distance + momentum * seconds_left) / (sigma_per_sec * math.sqrt(max(seconds_left, 1.0)))
        p_up = min(0.995, max(0.005, normal_cdf(z)))
        return {"spot": spot, "open": open_px, "p_up": p_up, "p_down": 1.0 - p_up, "z": z}

    def live_clob_client(self):
        if self.live_client:
            return self.live_client
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON
        key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        funder = os.getenv("POLYMARKET_FUNDER", "").strip()
        sig = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        chain_id = int(os.getenv("CHAIN_ID", str(POLYGON)))
        if not key or not funder:
            raise RuntimeError("missing POLYMARKET_PRIVATE_KEY/POLYMARKET_FUNDER for live mode")
        tmp = ClobClient(CLOB_HOST, chain_id=chain_id, key=key, signature_type=sig, funder=funder)
        cred_path = Path(os.getenv("CLOB_API_CREDS_PATH", "clob_api_creds.json"))
        if cred_path.exists():
            c = json.loads(cred_path.read_text())
            creds = ApiCreds(c.get("apiKey") or c.get("api_key"), c.get("secret") or c.get("api_secret"), c.get("passphrase") or c.get("api_passphrase"))
        else:
            raw = tmp.create_or_derive_api_creds()
            creds = ApiCreds(raw.api_key, raw.api_secret, raw.api_passphrase)
            cred_path.write_text(json.dumps({"api_key": raw.api_key, "api_secret": raw.api_secret, "api_passphrase": raw.api_passphrase}, indent=2))
        self.live_client = ClobClient(CLOB_HOST, chain_id=chain_id, key=key, creds=creds, signature_type=sig, funder=funder)
        return self.live_client

    def place_live_buy(self, token_id: str, price: float) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY
        client = self.live_clob_client()
        # Hard cap notional at $1. Do not bump size upward for exchange minimums.
        notional = min(1.0, float(self.s.order_notional_usd))
        size = math.floor((notional / price) * 10000) / 10000
        if size <= 0 or size * price > 1.0001:
            raise RuntimeError(f"invalid $1-capped size={size} price={price}")
        order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY, expiration=0))
        return client.post_order(order, orderType="GTC", post_only=False)

    async def evaluate_asset(self, asset: str) -> None:
        now = time.time()
        start = window_start(now, self.s.window_seconds)
        if self.state.count(asset, start) >= self.s.max_orders_per_market_window:
            print(json.dumps({"ts": now_iso(), "event": "skip_duplicate_window", "asset": asset, "window": start}))
            return
        market = await self.discover_market(asset, start)
        if not market:
            print(json.dumps({"ts": now_iso(), "event": "skip_no_market", "asset": asset, "window": start}))
            return
        if not (self.s.min_time_remaining_seconds <= market.seconds_left <= self.s.max_time_remaining_seconds):
            print(json.dumps({"ts": now_iso(), "event": "skip_time_gate", "asset": asset, "seconds_left": market.seconds_left}))
            return
        features = await self.spot_features(asset, market.start_ts, market.seconds_left)
        candidates = [("UP", market.token_up, features["p_up"]), ("DOWN", market.token_down, features["p_down"])]
        decisions = []
        for side, token, fair in candidates:
            ob = await self.book(token)
            if not ob:
                continue
            edge = fair - ob["ask"]
            decisions.append((edge, side, token, fair, ob))
        if not decisions:
            print(json.dumps({"ts": now_iso(), "event": "skip_empty_books", "asset": asset, "slug": market.slug}))
            return
        edge, side, token, fair, ob = max(decisions, key=lambda x: x[0])
        reason = None
        if edge < self.s.min_edge_cents / 100:
            reason = "edge_too_small"
        elif ob["spread"] > self.s.max_spread_cents / 100:
            reason = "spread_too_wide"
        elif not (self.s.min_entry_price <= ob["ask"] <= self.s.max_entry_price):
            reason = "entry_price_gate"
        log = {"ts": now_iso(), "asset": asset, "slug": market.slug, "side": side, "ask": ob["ask"], "bid": ob["bid"], "spread": ob["spread"], "fair": fair, "edge": edge, "features": features, "notional_usd": min(1.0, self.s.order_notional_usd)}
        if reason:
            log.update({"event": "skip", "reason": reason})
            print(json.dumps(log))
            return
        log["event"] = "order_intent"
        if self.s.execution_mode == "live":
            log["live_response"] = self.place_live_buy(token, ob["ask"])
            self.state.record(asset, start)
        else:
            log["dry_run"] = True
            self.state.record(asset, start)
        print(json.dumps(log, default=str))

    async def run_once(self) -> None:
        await asyncio.gather(*(self.evaluate_asset(a) for a in self.s.assets))

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(5)


async def amain() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    settings = Settings.load()
    bot = Bot(settings)
    try:
        if args.once:
            await bot.run_once()
        else:
            await bot.run_forever()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(amain())
