from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Protocol

from runtime_state import MinimalRuntimeState


_DEFAULT_TICK = Decimal("0.01")


class _Armory(Protocol):
    async def on_quote(self, *, signal: str, token_id: str, bid: Decimal, ask: Decimal, tick: Decimal) -> bool:
        ...


async def apply_market_event(
    event: dict[str, Any],
    state: MinimalRuntimeState,
    armory: _Armory,
) -> bool:
    if _is_market_resolved(event, state):
        state.mark_market_inactive("resolved")
        return True
    if not state.trading_active:
        return False

    changed = False
    for item in _iter_quote_items(event):
        token_id = item.token_id
        side = state.side_for_token(token_id)
        if not side:
            continue
        quote = state.update_quote(token_id, bid=item.bid, ask=item.ask, tick=item.tick)
        # The armory implementation is required to be effectively non-blocking
        # here (single-flight rearm scheduled internally). The await is
        # retained only to honour the existing protocol signature.
        await armory.on_quote(signal=side, token_id=token_id, bid=quote.bid, ask=quote.ask, tick=quote.tick)
        changed = True
    return changed


def _is_market_resolved(event: dict[str, Any], state: MinimalRuntimeState) -> bool:
    et = str(event.get("event_type") or event.get("eventType") or "").strip().lower()
    if et != "market_resolved":
        return False
    market = state.market
    if market is None:
        return True
    event_market = str(event.get("market") or event.get("condition_id") or event.get("conditionId") or "").strip()
    if not event_market:
        return True
    return event_market.lower() == market.condition_id.lower()


class _QuoteItem:
    __slots__ = ("token_id", "bid", "ask", "tick")

    def __init__(self, token_id: str, bid: Decimal, ask: Decimal, tick: Decimal) -> None:
        self.token_id = token_id
        self.bid = bid
        self.ask = ask
        self.tick = tick


def _iter_quote_items(event: dict[str, Any]) -> Iterator[_QuoteItem]:
    et = str(event.get("event_type") or event.get("eventType") or "").strip().lower()
    if et == "book":
        item = _item_from_book(event)
        if item is not None:
            yield item
        return

    changes = event.get("price_changes") or event.get("priceChanges")
    if isinstance(changes, list):
        for item in changes:
            if not isinstance(item, dict):
                continue
            parsed = _item_from_dict(item)
            if parsed is not None:
                yield parsed
        return
    item = _item_from_dict(event)
    if item is not None:
        yield item


def _item_from_book(raw: dict[str, Any]) -> _QuoteItem | None:
    token_id = str(raw.get("asset_id") or raw.get("assetId") or raw.get("token_id") or raw.get("tokenId") or "")
    if not token_id:
        return None
    bid = _best_book_side(raw.get("bids"), reverse=True)
    ask = _best_book_side(raw.get("asks"), reverse=False)
    tick = _dec(raw.get("tick_size") or raw.get("tickSize") or _DEFAULT_TICK)
    if bid <= 0 and ask <= 0:
        return None
    return _QuoteItem(token_id, bid, ask, tick if tick > 0 else _DEFAULT_TICK)


def _best_book_side(levels: Any, *, reverse: bool) -> Decimal:
    if not isinstance(levels, list):
        return Decimal("0")
    best: Decimal | None = None
    for level in levels:
        if not isinstance(level, dict):
            continue
        size = _dec(level.get("size") or level.get("qty") or 0)
        if size <= 0:
            continue
        price = _dec(level.get("price") or 0)
        if price <= 0:
            continue
        if best is None or (reverse and price > best) or (not reverse and price < best):
            best = price
    return best if best is not None else Decimal("0")


def _item_from_dict(raw: dict[str, Any]) -> _QuoteItem | None:
    token_id = str(raw.get("asset_id") or raw.get("assetId") or raw.get("token_id") or raw.get("tokenId") or "")
    if not token_id:
        return None
    # Strict: only accept events that carry both top-of-book sides. A bare
    # `price` field on a price_change row refers to one side only and must
    # not be used to synthesize the opposite side.
    bid = _dec(raw.get("best_bid") or raw.get("bestBid") or raw.get("bid") or 0)
    ask = _dec(raw.get("best_ask") or raw.get("bestAsk") or raw.get("ask") or 0)
    tick = _dec(raw.get("tick_size") or raw.get("tickSize") or _DEFAULT_TICK)
    if bid <= 0 and ask <= 0:
        return None
    return _QuoteItem(token_id, bid, ask, tick if tick > 0 else _DEFAULT_TICK)


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
