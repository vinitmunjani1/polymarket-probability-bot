from __future__ import annotations

import asyncio
import json
import time

import httpx

from .clob import ClobData, LiveExecutor
from .config import Settings
from .market_discovery import MarketDiscovery
from .spot import SpotModel
from .state import State
from .strategy import apply_gates, choose_candidate
from .utils import now_iso, window_start


class BotEngine:
    def __init__(self, settings: Settings, event_sink=None):
        self.s = settings
        limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
        timeout = httpx.Timeout(settings.http_timeout_seconds)
        self.http = httpx.AsyncClient(timeout=timeout, limits=limits, http2=True)
        self.state = State(settings.state_path)
        self.discovery = MarketDiscovery(self.http, settings.gamma_api)
        self.clob = ClobData(self.http, settings.clob_host)
        self.spot = SpotModel(self.http, settings.binance_api)
        self.executor = LiveExecutor(settings.clob_host, settings.order_notional_usd)
        self.event_sink = event_sink

    async def close(self) -> None:
        await self.http.aclose()

    async def run_once(self) -> None:
        await asyncio.gather(*(self.evaluate_asset(asset) for asset in self.s.assets))

    async def run_forever(self) -> None:
        while True:
            started = time.perf_counter()
            await self.run_once()
            # Fast enough for 5m markets, but avoid hammering APIs unnecessarily.
            await asyncio.sleep(max(0.5, 2.0 - (time.perf_counter() - started)))

    async def evaluate_asset(self, asset: str) -> None:
        loop_start = time.perf_counter()
        start = window_start(time.time(), self.s.window_seconds)
        if self.state.count(asset, start) >= self.s.max_orders_per_market_window:
            self._emit({"event": "skip_duplicate_window", "asset": asset, "window": start, "latency_ms": self._latency(loop_start)})
            return

        market = await self.discovery.discover(asset, start)
        if not market:
            self._emit({"event": "skip_no_market", "asset": asset, "window": start, "latency_ms": self._latency(loop_start)})
            return

        seconds_left = market.seconds_left
        if not (self.s.min_time_remaining_seconds <= seconds_left <= self.s.max_time_remaining_seconds):
            self._emit({"event": "skip_time_gate", "asset": asset, "seconds_left": seconds_left, "latency_ms": self._latency(loop_start)})
            return

        # Hot path: spot model and both side books are independent, fetch together.
        features_task = asyncio.create_task(self.spot.features(asset, market.start_ts, seconds_left))
        up_book_task = asyncio.create_task(self.clob.book(market.token_up))
        down_book_task = asyncio.create_task(self.clob.book(market.token_down))
        features, up_book, down_book = await asyncio.gather(features_task, up_book_task, down_book_task)

        candidate = choose_candidate(market.token_up, market.token_down, features, up_book, down_book)
        if not candidate:
            self._emit({"event": "skip_empty_books", "asset": asset, "slug": market.slug, "latency_ms": self._latency(loop_start)})
            return

        decision = apply_gates(self.s, candidate)
        log = {
            "asset": asset,
            "slug": market.slug,
            "side": candidate.side,
            "ask": candidate.book.ask,
            "bid": candidate.book.bid,
            "spread": candidate.book.spread,
            "fair": candidate.fair,
            "edge": candidate.edge,
            "features": features.as_dict(),
            "notional_usd": min(1.0, self.s.order_notional_usd),
            "latency_ms": self._latency(loop_start),
        }
        if not decision.should_trade:
            log.update({"event": "skip", "reason": decision.reason})
            self._emit(log)
            return

        log["event"] = "order_intent"
        if self.s.execution_mode == "live":
            log["live_response"] = self.executor.buy(candidate.token_id, candidate.book.ask)
            self.state.record(asset, start)
        else:
            log["dry_run"] = True
            self.state.record(asset, start)
        self._emit(log)

    @staticmethod
    def _latency(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    def _emit(self, payload: dict) -> None:
        payload = {"ts": now_iso(), **payload}
        if self.event_sink:
            self.event_sink(payload)
        print(json.dumps(payload, default=str), flush=True)
