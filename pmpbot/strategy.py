from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .models import Book, SpotFeatures


@dataclass(frozen=True)
class Candidate:
    edge: float
    side: str
    token_id: str
    fair: float
    book: Book


@dataclass(frozen=True)
class Decision:
    should_trade: bool
    reason: str | None
    candidate: Candidate


def choose_candidate(settings: Settings, up_token: str, down_token: str, features: SpotFeatures, up_book: Book | None, down_book: Book | None) -> Candidate | None:
    candidates: list[Candidate] = []
    if up_book:
        candidates.append(Candidate(features.p_up - up_book.ask, "UP", up_token, features.p_up, up_book))
    if down_book:
        candidates.append(Candidate(features.p_down - down_book.ask, "DOWN", down_token, features.p_down, down_book))
    if not candidates:
        return None
    if settings.trade_trigger_mode in {"signal", "edge_or_signal"}:
        signal_candidates = [c for c in candidates if c.book.ask >= settings.min_signal_price]
        if signal_candidates:
            return max(signal_candidates, key=lambda c: c.book.ask)
    return max(candidates, key=lambda c: c.edge)


def apply_gates(settings: Settings, candidate: Candidate) -> Decision:
    reason = None
    edge_pass = candidate.edge >= settings.min_edge_cents / 100
    signal_pass = candidate.book.ask >= settings.min_signal_price
    mode = settings.trade_trigger_mode
    trigger_pass = edge_pass if mode == "edge" else (signal_pass if mode == "signal" else edge_pass or signal_pass)

    if not trigger_pass:
        reason = "edge_too_small"
    elif candidate.book.spread > settings.max_spread_cents / 100:
        reason = "spread_too_wide"
    elif not (settings.min_entry_price <= candidate.book.ask <= settings.max_entry_price):
        reason = "entry_price_gate"
    return Decision(should_trade=reason is None, reason=reason, candidate=candidate)
