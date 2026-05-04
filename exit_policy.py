from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

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
    opened_ns: int


@dataclass(frozen=True, slots=True)
class ExitPolicyConfig:
    take_profit_bps: int = 1200
    stop_loss_bps: int = 1800
    max_hold_us: int = 0
    force_exit_tte_us: int = 10_000_000
    max_quote_age_us: int = 250_000
    min_bid: Decimal = Decimal("0.01")
    order_type: str = "FAK"
    signal: str = "EXIT"


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
    order_type: str = "FAK"
    signal: str = "EXIT"


def _hold(reason: str, position: OpenPosition | None = None) -> ExitDecision:
    if position is None:
        return ExitDecision("HOLD", reason)
    return ExitDecision("HOLD", reason, side=position.side, token_id=position.token_id)


def _target(entry: Decimal, bps: int) -> Decimal:
    return entry * (_DEC_ONE + (Decimal(int(bps)) / _BPS))


def _floor(entry: Decimal, bps: int) -> Decimal:
    return entry * (_DEC_ONE - (Decimal(int(bps)) / _BPS))


def _price_at_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _sell(
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
        limit_price=_price_at_tick(quote.bid, quote.tick),
        bid=quote.bid,
        ask=quote.ask,
        order_type=cfg.order_type,
        signal=cfg.signal,
    )


def decide_exit(
    position: OpenPosition | None,
    quote: QuoteState | None,
    cfg: ExitPolicyConfig,
    *,
    now_ns: int,
    tte_us: int,
    sellable_size: Decimal,
) -> ExitDecision:
    if position is None or position.size <= 0:
        return _hold("no_position", position)
    if quote is None or quote.token_id != position.token_id:
        return _hold("quote_missing", position)
    if sellable_size <= 0:
        return _hold("no_sellable_inventory", position)

    quote_age_us = max(0, (int(now_ns) - quote.ts_ns) // 1000)
    if quote_age_us > cfg.max_quote_age_us:
        return _hold("quote_stale", position)
    if quote.bid < cfg.min_bid:
        return _hold("bid_below_min", position)

    size = min(position.size, sellable_size)
    if tte_us <= cfg.force_exit_tte_us:
        return _sell("expiry_ripcord", position, quote, cfg, size)
    if cfg.max_hold_us > 0 and (int(now_ns) - position.opened_ns) // 1000 >= cfg.max_hold_us:
        return _sell("time_stop", position, quote, cfg, size)
    if quote.bid >= _target(position.entry_price, cfg.take_profit_bps):
        return _sell("take_profit", position, quote, cfg, size)
    if cfg.stop_loss_bps > 0 and quote.bid <= _floor(position.entry_price, cfg.stop_loss_bps):
        return _sell("stop_loss", position, quote, cfg, size)
    return _hold("hold", position)
