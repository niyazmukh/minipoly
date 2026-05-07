from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from signal_decision import MarketSignalContract


_DEC_ZERO = Decimal("0")
_DEFAULT_TICK = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class MinimalMarket:
    slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_label: str
    no_label: str
    start_ts: float
    end_ts: float
    strike: float = 0.0
    slug_ts: int = 0


@dataclass(frozen=True, slots=True)
class QuoteState:
    token_id: str
    bid: Decimal
    ask: Decimal
    tick: Decimal
    ts_ns: int


class MinimalRuntimeState:
    __slots__ = ("_now_ns", "market", "contract", "quotes", "trading_active")

    def __init__(self, *, now_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._now_ns = now_ns
        self.market: MinimalMarket | None = None
        self.contract = MarketSignalContract("", "", "", "")
        self.quotes: dict[str, QuoteState] = {}
        self.trading_active = False

    def set_market(self, market: MinimalMarket) -> None:
        self.market = market
        self.contract = MarketSignalContract(
            market.yes_token_id,
            market.no_token_id,
            market.yes_label,
            market.no_label,
        )
        live = {market.yes_token_id, market.no_token_id}
        self.quotes = {token_id: quote for token_id, quote in self.quotes.items() if token_id in live}
        self.trading_active = True

    def token_for_side(self, side: str) -> str:
        return self.contract.token_for_signal(side)

    def side_for_token(self, token_id: str) -> str:
        if token_id == self.contract.yes_token_id:
            return "YES"
        if token_id == self.contract.no_token_id:
            return "NO"
        return ""

    def update_quote(
        self,
        token_id: str,
        *,
        bid: Decimal,
        ask: Decimal,
        tick: Decimal = _DEFAULT_TICK,
        ts_ns: int | None = None,
    ) -> QuoteState:
        quote = QuoteState(
            token_id=token_id,
            bid=bid if bid > 0 else _DEC_ZERO,
            ask=ask if ask > 0 else _DEC_ZERO,
            tick=tick if tick > 0 else _DEFAULT_TICK,
            ts_ns=int(ts_ns if ts_ns is not None else self._now_ns()),
        )
        self.quotes[token_id] = quote
        return quote

    def quote_for_side(self, side: str) -> QuoteState | None:
        token_id = self.token_for_side(side)
        if not token_id:
            return None
        return self.quotes.get(token_id)

    def quote_age_us(self, token_id: str, *, now_ns: int | None = None) -> int:
        quote = self.quotes.get(token_id)
        if quote is None:
            return 2**63 - 1
        current_ns = int(now_ns if now_ns is not None else self._now_ns())
        return max(0, (current_ns - quote.ts_ns) // 1000)

    def now_ns(self) -> int:
        return int(self._now_ns())

    def mark_market_inactive(self, reason: str) -> None:
        self.trading_active = False
        self.quotes.clear()
