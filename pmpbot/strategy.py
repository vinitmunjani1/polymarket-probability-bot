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
        if settings.require_signal_edge_confirmation:
            min_confirm_edge = settings.min_confirmation_edge_cents / 100
            signal_candidates = [c for c in signal_candidates if c.edge >= min_confirm_edge]
        if signal_candidates:
            return max(signal_candidates, key=lambda c: c.book.ask)
    return max(candidates, key=lambda c: c.edge)


def apply_gates(settings: Settings, candidate: Candidate) -> Decision:
    reason = None
    edge_pass = candidate.edge >= settings.min_edge_cents / 100
    signal_pass = candidate.book.ask >= settings.min_signal_price
    confirmation_pass = True
    if settings.require_signal_edge_confirmation and signal_pass:
        confirmation_pass = candidate.edge >= settings.min_confirmation_edge_cents / 100
    mode = settings.trade_trigger_mode
    confirmed_signal_pass = signal_pass and confirmation_pass
    trigger_pass = edge_pass if mode == "edge" else (confirmed_signal_pass if mode == "signal" else edge_pass or confirmed_signal_pass)

    if not trigger_pass:
        if signal_pass and not confirmation_pass:
            reason = "signal_not_confirmed_by_edge"
        else:
            reason = "signal_price_below_min" if mode == "signal" else "edge_too_small"
    elif candidate.book.spread > settings.max_spread_cents / 100:
        reason = "spread_too_wide"
    elif not (settings.min_entry_price <= candidate.book.ask <= settings.max_entry_price):
        reason = "entry_price_gate"
    return Decision(should_trade=reason is None, reason=reason, candidate=candidate)
