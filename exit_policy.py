from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from runtime_state import QuoteState


_DEC_ZERO = Decimal("0")
_DEC_ONE = Decimal("1")
_BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class OpenPosition:
    side: str
    token_id: str
    entry_price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class ExitPolicyConfig:
    take_profit_bps: int = 1200
    force_exit_tte_us: int = 10_000_000
    order_type: str = "FAK"
    signal: str = "EXIT"
    fak_attempts: int = 3


@dataclass(frozen=True, slots=True)
class ExitDecision:
    action: str
    reason: str
    side: str = ""
    token_id: str = ""
    size: Decimal = _DEC_ZERO
    limit_price: Decimal = _DEC_ZERO
    bid: Decimal = _DEC_ZERO
    ask: Decimal = _DEC_ZERO
    tick: Decimal = Decimal("0.01")
    order_type: str = "FAK"
    signal: str = "EXIT"


def _hold(reason: str, position: OpenPosition | None = None) -> ExitDecision:
    if position is None:
        return ExitDecision("HOLD", reason)
    return ExitDecision("HOLD", reason, side=position.side, token_id=position.token_id)


def _target(entry: Decimal, bps: int) -> Decimal:
    return entry * (_DEC_ONE + (Decimal(int(bps)) / _BPS))


def price_at_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def price_at_or_above_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def sell_decision(
    reason: str,
    position: OpenPosition,
    quote: QuoteState,
    cfg: ExitPolicyConfig,
    size: Decimal,
) -> ExitDecision:
    return ExitDecision(
        "SELL",
        reason,
        side=position.side,
        token_id=position.token_id,
        size=size,
        limit_price=price_at_tick(quote.bid, quote.tick),
        bid=quote.bid,
        ask=quote.ask,
        tick=quote.tick,
        order_type=cfg.order_type,
        signal=cfg.signal,
    )


def take_profit_decision(
    position: OpenPosition,
    quote: QuoteState,
    cfg: ExitPolicyConfig,
    size: Decimal,
) -> ExitDecision:
    return ExitDecision(
        "SELL",
        "take_profit",
        side=position.side,
        token_id=position.token_id,
        size=size,
        limit_price=price_at_or_above_tick(_target(position.entry_price, cfg.take_profit_bps), quote.tick),
        bid=quote.bid,
        ask=quote.ask,
        tick=quote.tick,
        order_type=cfg.order_type,
        signal=cfg.signal,
    )


def decide_exit(
    position: OpenPosition | None,
    quote: QuoteState | None,
    cfg: ExitPolicyConfig,
    *,
    tte_us: int,
    sellable_size: Decimal,
) -> ExitDecision:
    if position is None or position.size <= 0:
        return _hold("no_position", position)
    if quote is None or quote.token_id != position.token_id:
        return _hold("quote_missing", position)
    if sellable_size <= 0:
        return _hold("no_sellable_inventory", position)

    size = min(position.size, sellable_size)
    if tte_us <= cfg.force_exit_tte_us:
        return sell_decision("expiry_ripcord", position, quote, cfg, size)
    return take_profit_decision(position, quote, cfg, size)
