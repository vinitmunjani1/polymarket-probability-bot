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
from .vatic import VaticPriceFeed


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
        self.vatic = VaticPriceFeed(self.http)
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
        already_ordered = self.state.count(asset, start) >= self.s.max_orders_per_market_window
        existing_position = self.state.position(asset, start)

        market = await self.discovery.discover(asset, start)
        if not market:
            self._emit({"event": "skip_no_market", "asset": asset, "window": start, "latency_ms": self._latency(loop_start)})
            return

        seconds_left = market.seconds_left
        if not existing_position and not (self.s.min_time_remaining_seconds <= seconds_left <= self.s.max_time_remaining_seconds):
            self._emit({"event": "skip_time_gate", "asset": asset, "seconds_left": seconds_left, "latency_ms": self._latency(loop_start)})
            return

        # Fetch oracle target and both books concurrently. Features depend on
        # the oracle target, but books do not, so start all network I/O now.
        price_to_beat_task = asyncio.create_task(self.vatic.price_to_beat(asset, market.start_ts, "5min"))
        up_book_task = asyncio.create_task(self.clob.book(market.token_up))
        down_book_task = asyncio.create_task(self.clob.book(market.token_down))

        price_to_beat = await price_to_beat_task
        if not price_to_beat:
            up_book_task.cancel()
            down_book_task.cancel()
            self._emit({"event": "skip", "reason": "price_to_beat_unavailable", "asset": asset, "window": start, "slug": market.slug, "latency_ms": self._latency(loop_start)})
            return

        features_task = asyncio.create_task(self.spot.features(asset, market.start_ts, seconds_left, price_to_beat=float(price_to_beat["price"])))
        features, up_book, down_book = await asyncio.gather(features_task, up_book_task, down_book_task)

        candidate = choose_candidate(self.s, market.token_up, market.token_down, features, up_book, down_book)
        if not candidate:
            self._emit({"event": "skip_empty_books", "asset": asset, "slug": market.slug, "latency_ms": self._latency(loop_start)})
            return

        decision = apply_gates(self.s, candidate)
        log = {
            "asset": asset,
            "window": start,
            "slug": market.slug,
            "side": candidate.side,
            "ask": candidate.book.ask,
            "bid": candidate.book.bid,
            "spread": candidate.book.spread,
            "fair": candidate.fair,
            "edge": candidate.edge,
            "features": features.as_dict(),
            "price_to_beat": price_to_beat["price"],
            "price_to_beat_source": price_to_beat["source"],
            "price_to_beat_provider": price_to_beat.get("provider"),
            "notional_usd": min(1.0, self.s.order_notional_usd),
            "dry_capital_usd": self.s.dry_capital_per_asset_usd,
            "dry_used_capital_usd": self.state.asset_open_notional(asset),
            "latency_ms": self._latency(loop_start),
        }
        if existing_position:
            log.update({"event": "market_snapshot", "reason": "already_ordered_window"})
            log.update(self._position_metrics(existing_position, candidate.side, up_book, down_book))
            self._emit(log)
            return

        if already_ordered:
            # Legacy/order-count state without a recorded position: do not spam duplicate skips.
            log.update({"event": "market_snapshot", "reason": "already_ordered_window"})
            self._emit(log)
            return

        if not decision.should_trade:
            log.update({"event": "skip", "reason": decision.reason})
            self._emit(log)
            return

        notional = min(1.0, self.s.order_notional_usd)
        dry_used = self.state.asset_open_notional(asset)
        if self.s.execution_mode != "live" and dry_used + notional > self.s.dry_capital_per_asset_usd + 1e-9:
            log.update({"event": "skip", "reason": "dry_capital_limit", "dry_used_capital_usd": dry_used})
            self._emit(log)
            return

        log["event"] = "order_intent"
        if self.s.execution_mode == "live":
            log["live_response"] = self.executor.buy(candidate.token_id, candidate.book.ask)
            self.state.record_order(asset, start)
        else:
            charges = self._dry_charges(notional)
            position = self.state.record_position(
                asset=asset,
                window=start,
                slug=market.slug,
                side=candidate.side,
                token_id=candidate.token_id,
                entry_price=candidate.book.ask,
                notional_usd=notional,
                charges_usd=charges,
            )
            self.state.record_order(asset, start)
            log["dry_run"] = True
            log.update(self._position_metrics(position, candidate.side, up_book, down_book))
        self._emit(log)

    def _dry_charges(self, notional: float) -> float:
        return float(self.s.dry_fixed_charge_usd) + float(notional) * float(self.s.dry_charge_rate_bps) / 10_000.0

    @staticmethod
    def _position_metrics(position: dict, candidate_side: str, up_book, down_book) -> dict:
        held_side = position.get("side")
        held_book = up_book if held_side == "UP" else down_book
        mark_price = held_book.bid if held_book else None
        shares = float(position.get("shares", 0.0))
        notional = float(position.get("notional_usd", 0.0))
        charges = float(position.get("charges_usd", 0.0))
        mark_value = None if mark_price is None else shares * float(mark_price)
        pnl = None if mark_value is None else mark_value - notional - charges
        return {
            "position": {
                **position,
                "mark_price": mark_price,
                "mark_value_usd": mark_value,
                "pnl_usd": pnl,
                "charges_usd": charges,
            },
            "position_pnl_usd": pnl,
            "position_charges_usd": charges,
            "held_side": held_side,
            "best_candidate_side": candidate_side,
        }

    @staticmethod
    def _latency(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    def _emit(self, payload: dict) -> None:
        payload = {"ts": now_iso(), **payload}
        if self.event_sink:
            self.event_sink(payload)
        print(json.dumps(payload, default=str), flush=True)
