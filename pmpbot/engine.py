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
        self._settlement_pending_notified: set[str] = set()

    async def close(self) -> None:
        await self.http.aclose()

    async def run_once(self) -> None:
        await self.settle_open_positions()
        await asyncio.gather(*(self.evaluate_asset(asset) for asset in self.s.assets))

    async def run_forever(self) -> None:
        while True:
            started = time.perf_counter()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._emit({
                    "event": "engine_error",
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "latency_ms": self._latency(started),
                })
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
        execution_price = float(candidate.book.ask)
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
            "trigger_price": candidate.book.ask,
            "execution_price": execution_price,
            "execution_type": "market",
            "dry_order_status": "not_placed",
            "dry_capital_usd": self.state.asset_dry_equity(asset, self.s.dry_capital_per_asset_usd),
            "dry_used_capital_usd": self.state.asset_open_notional(asset),
            "dry_available_capital_usd": self.state.asset_available_notional(asset, self.s.dry_capital_per_asset_usd),
            "latency_ms": self._latency(loop_start),
        }
        if existing_position:
            log.update({"event": "market_snapshot", "reason": "already_ordered_window"})
            log.update(self._position_metrics(existing_position, candidate.side, up_book, down_book))
            log["dry_order_status"] = existing_position.get("status", "open")
            if self._should_stop_loss(log.get("position")):
                stop_event = self._stop_loss_exit(asset, start, existing_position, log.get("position") or {}, up_book, down_book)
                log.update(stop_event)
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
        dry_available = self.state.asset_available_notional(asset, self.s.dry_capital_per_asset_usd)
        if self.s.execution_mode != "live" and notional > dry_available + 1e-9:
            log.update({"event": "skip", "reason": "dry_capital_limit", "dry_used_capital_usd": dry_used, "dry_available_capital_usd": dry_available})
            self._emit(log)
            return

        log["event"] = "order_intent"
        log["dry_order_status"] = "open"
        charges = self._dry_charges(notional)
        if self.s.execution_mode == "live":
            # Polymarket CLOB has no pure market order; this is an aggressive
            # limit at the current ask with post_only=False, i.e. market-style.
            log["live_response"] = self.executor.buy(candidate.token_id, execution_price)
            position = self.state.record_position(
                asset=asset,
                window=start,
                slug=market.slug,
                side=candidate.side,
                token_id=candidate.token_id,
                entry_price=execution_price,
                notional_usd=notional,
                charges_usd=charges,
                status="open",
                trigger_price=candidate.book.ask,
            )
            self.state.record_order(asset, start)
            log["dry_used_capital_usd"] = self.state.asset_open_notional(asset)
            log["dry_available_capital_usd"] = self.state.asset_available_notional(asset, self.s.dry_capital_per_asset_usd)
            log.update(self._position_metrics(position, candidate.side, up_book, down_book))
        else:
            position = self.state.record_position(
                asset=asset,
                window=start,
                slug=market.slug,
                side=candidate.side,
                token_id=candidate.token_id,
                entry_price=execution_price,
                notional_usd=notional,
                charges_usd=charges,
                status="open",
                trigger_price=candidate.book.ask,
            )
            self.state.record_order(asset, start)
            log["dry_run"] = True
            log["dry_used_capital_usd"] = self.state.asset_open_notional(asset)
            log["dry_available_capital_usd"] = self.state.asset_available_notional(asset, self.s.dry_capital_per_asset_usd)
            log.update(self._position_metrics(position, candidate.side, up_book, down_book))
        self._emit(log)

    async def settle_open_positions(self) -> None:
        """Settle expired/open positions using Gamma resolution data.

        The trading book can disappear or freeze near 0.98 when bidding closes.
        PnL must wait for the resolved outcome and close at binary value 1/0.
        """
        positions = list(self.state.data.setdefault("positions", {}).values())
        tasks = [self._settle_position_if_resolved(p) for p in positions if p.get("status", "open") == "open"]
        if tasks:
            await asyncio.gather(*tasks)

    async def _settle_position_if_resolved(self, position: dict) -> None:
        slug = position.get("slug")
        asset = position.get("asset")
        window = position.get("window")
        if not slug or not asset or window is None:
            return
        # Avoid extra calls for active windows; this is only settlement logic.
        if time.time() < int(window) + int(self.s.window_seconds):
            return
        raw = await self.discovery.market_by_slug(str(slug))
        if not raw:
            return
        exit_price = self.discovery.resolved_token_price(
            raw,
            token_id=position.get("token_id"),
            side=position.get("side"),
        )
        if exit_price is None:
            key = f"{asset}:{window}"
            if key not in self._settlement_pending_notified:
                self._settlement_pending_notified.add(key)
                self._emit({
                    "event": "settlement_pending",
                    "asset": asset,
                    "window": window,
                    "slug": slug,
                    "dry_order_status": "settlement_pending",
                    "reason": "market_not_resolved_yet",
                    "settlement_status": "waiting_for_gamma_resolution",
                    **self._position_metrics(position, position.get("side"), None, None),
                })
            return
        closed = self.state.close_position(str(asset), int(window), exit_price=exit_price, exit_reason="resolved")
        self._settlement_pending_notified.discard(f"{asset}:{window}")
        self._emit({
            "event": "position_settled",
            "asset": asset,
            "window": window,
            "slug": slug,
            "settlement_price": exit_price,
            "dry_order_status": closed.get("status"),
            "dry_capital_usd": self.state.asset_dry_equity(str(asset), self.s.dry_capital_per_asset_usd),
            "dry_used_capital_usd": self.state.asset_open_notional(str(asset)),
            "dry_available_capital_usd": self.state.asset_available_notional(str(asset), self.s.dry_capital_per_asset_usd),
            **self._position_metrics(closed, closed.get("side"), None, None),
        })

    def _dry_charges(self, notional: float) -> float:
        return float(self.s.dry_fixed_charge_usd) + float(notional) * float(self.s.dry_charge_rate_bps) / 10_000.0

    def _should_stop_loss(self, marked_position: dict | None) -> bool:
        if not marked_position or marked_position.get("status") != "open":
            return False
        mark_price = marked_position.get("mark_price")
        return mark_price is not None and float(mark_price) <= float(self.s.stop_loss_price)

    def _stop_loss_exit(self, asset: str, window: int, position: dict, marked_position: dict, up_book, down_book) -> dict:
        held_side = position.get("side")
        held_book = up_book if held_side == "UP" else down_book
        exit_price = float(marked_position.get("mark_price") or 0.0)
        event = {
            "event": "stop_loss_sell",
            "reason": "stop_loss_hit",
            "stop_loss_price": self.s.stop_loss_price,
            "sell_price": exit_price,
            "execution_type": "market_sell",
        }
        if self.s.execution_mode == "live" and held_book:
            event["live_sell_response"] = self.executor.sell(
                token_id=position["token_id"],
                price=exit_price,
                size=float(position.get("shares", 0.0)),
            )
        closed = self.state.close_position(asset, window, exit_price=exit_price, exit_reason="stop_loss_hit")
        event["dry_order_status"] = closed.get("status")
        event.update(self._position_metrics(closed, held_side, up_book, down_book))
        event["dry_capital_usd"] = self.state.asset_dry_equity(asset, self.s.dry_capital_per_asset_usd)
        event["dry_used_capital_usd"] = self.state.asset_open_notional(asset)
        event["dry_available_capital_usd"] = self.state.asset_available_notional(asset, self.s.dry_capital_per_asset_usd)
        return event

    @staticmethod
    def _position_metrics(position: dict, candidate_side: str, up_book, down_book) -> dict:
        held_side = position.get("side")
        held_book = up_book if held_side == "UP" else down_book
        mark_price = held_book.bid if held_book else None
        shares = float(position.get("shares", 0.0))
        notional = float(position.get("notional_usd", 0.0))
        charges = float(position.get("charges_usd", 0.0))
        if position.get("status") == "closed" and position.get("exit_price") is not None:
            mark_price = float(position.get("exit_price"))
            mark_value = float(position.get("exit_value_usd", shares * mark_price))
            pnl = float(position.get("pnl_usd", mark_value - notional - charges))
        else:
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
