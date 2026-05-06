from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Awaitable, Callable, Protocol

from fast_order_submitter import FastOrderTemplate, canonical_buy_target_for_notional
from hot_path_engine import HotPathGuard


_DEC_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_MIN_CLOB_PRICE = Decimal("0.01")
_MAX_CLOB_PRICE = Decimal("0.99")
_MIN_MARKETABLE_BUY_USDC = Decimal("1.01")
_LOG = logging.getLogger(__name__)


class _Engine(Protocol):
    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        ...

    def update_quote(self, token_id: str, *, bid: Decimal, ask: Decimal, ts_ns: int | None = None) -> None:
        ...


BuildTemplate = Callable[..., Awaitable[FastOrderTemplate]]


def ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def floor_to_2dp(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_FLOOR)


def ceil_to_2dp(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class ArmoryConfig:
    usdc_per_trade: Decimal
    entry_slippage: Decimal = _DEC_ZERO
    min_size: Decimal = _CENT
    min_buy_limit: Decimal = _MIN_CLOB_PRICE
    max_buy_limit: Decimal = _MAX_CLOB_PRICE
    order_type: str = "FAK"
    post_only: bool = False
    max_quote_age_ns: int = 250_000_000
    max_notional_overrun: Decimal = Decimal("0.01")
    max_notional_overrun_bps: int = 0


@dataclass(frozen=True, slots=True)
class _ArmedState:
    token_id: str
    buy_limit: Decimal
    size: Decimal
    tick: Decimal


@dataclass(frozen=True, slots=True)
class _PendingTarget:
    signal: str
    token_id: str
    bid: Decimal
    ask: Decimal
    tick: Decimal
    buy_limit: Decimal
    size: Decimal


class TemplateArmory:
    """Quote-driven entry-template armory with single-flight signing.

    The market WS hot path calls `on_quote` for every quote update. We
    synchronously update the engine quote (cheap), compute the latest valid
    target, and coalesce signing through one background task per side. The
    market WS loop never waits for EIP-712 signing.
    """

    __slots__ = (
        "_cfg",
        "_engine",
        "_build_template",
        "_now_ns",
        "_armed",
        "_pending_target",
        "_inflight_task",
    )

    def __init__(
        self,
        *,
        cfg: ArmoryConfig,
        engine: _Engine,
        build_template: BuildTemplate,
        now_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if cfg.usdc_per_trade < _MIN_MARKETABLE_BUY_USDC:
            raise RuntimeError(
                f"MINIMAL_USDC_PER_TRADE must be >= {_MIN_MARKETABLE_BUY_USDC} for marketable BUY orders."
            )
        self._cfg = cfg
        self._engine = engine
        self._build_template = build_template
        self._now_ns = now_ns
        self._armed: dict[str, _ArmedState] = {}
        self._pending_target: dict[str, _PendingTarget] = {}
        self._inflight_task: dict[str, asyncio.Task[None]] = {}

    async def on_quote(
        self,
        *,
        signal: str,
        token_id: str,
        bid: Decimal,
        ask: Decimal,
        tick: Decimal,
    ) -> bool:
        # Always update the engine's quote synchronously so HotPathEngine sees
        # the freshest top-of-book on every event.
        now_ns = self._now_ns()
        self._engine.update_quote(token_id, bid=bid, ask=ask, ts_ns=now_ns)
        next_target = self._target_from_quote(signal=signal, token_id=token_id, bid=bid, ask=ask, tick=tick)
        if next_target is None:
            return False

        key = signal.upper()
        if self._is_current(key, next_target):
            return False
        if self._pending_target.get(key) == next_target:
            return False

        # Latest target wins. A signer already in flight will drain this slot
        # when it finishes its current SDK call.
        self._pending_target[key] = next_target
        existing = self._inflight_task.get(key)
        if existing is None or existing.done():
            self._inflight_task[key] = asyncio.create_task(
                self._sign_and_arm_loop(key),
                name=f"template-armory-rearm-{key.lower()}",
            )
        return True

    def _target_from_quote(
        self,
        *,
        signal: str,
        token_id: str,
        bid: Decimal,
        ask: Decimal,
        tick: Decimal,
    ) -> _PendingTarget | None:
        if not signal or not token_id or ask <= 0 or tick <= 0:
            return None
        # TODO(spread-surface): once market_feed maintains a live L2 book and
        # passes executable depth into the armory, compute volume-weighted
        # executable price for the intended size.  SF2 (Dubach 2026) shows L1
        # is only ~14% of top-10 depth.  Do not consume snapshot-only depth here.
        buy_limit = ceil_to_tick(ask + self._cfg.entry_slippage, tick)
        min_buy_limit = max(_MIN_CLOB_PRICE, self._cfg.min_buy_limit)
        max_buy_limit = min(_MAX_CLOB_PRICE, self._cfg.max_buy_limit)
        if buy_limit < min_buy_limit or buy_limit > max_buy_limit:
            return None
        try:
            target = canonical_buy_target_for_notional(
                price=buy_limit,
                target_usdc=self._cfg.usdc_per_trade,
                tick=tick,
                min_size=self._cfg.min_size,
                min_maker_amount=_MIN_MARKETABLE_BUY_USDC,
                max_notional_overrun=self._cfg.max_notional_overrun,
                max_notional_overrun_bps=self._cfg.max_notional_overrun_bps,
            )
        except ValueError as exc:
            _LOG.debug("template_armory_buy_target_rejected signal=%s error=%s", signal, exc)
            return None
        return _PendingTarget(
            signal=signal.upper(),
            token_id=token_id,
            bid=bid,
            ask=ask,
            tick=tick,
            buy_limit=target.price,
            size=target.size,
        )

    def _is_current(self, key: str, target: _PendingTarget) -> bool:
        prev = self._armed.get(key)
        return (
            prev is not None
            and prev.token_id == target.token_id
            and prev.tick == target.tick
            and prev.buy_limit == target.buy_limit
            and prev.size == target.size
        )

    async def _sign_and_arm_loop(self, key: str) -> None:
        # Drain the pending-target slot until current. This gives single-flight
        # semantics: at most one signing task per signal, with fresh targets
        # replacing older targets while the SDK signs.
        try:
            while True:
                target = self._pending_target.pop(key, None)
                if target is None:
                    return
                template = await self._build_template(
                    name=key.lower(),
                    token_id=target.token_id,
                    side="BUY",
                    price=target.buy_limit,
                    size=target.size,
                    tick=target.tick,
                    order_type=self._cfg.order_type,
                    post_only=self._cfg.post_only,
                )
                guard = HotPathGuard(
                    max_age_ns=self._cfg.max_quote_age_ns,
                )
                if template.price != target.buy_limit or template.size != target.size:
                    _LOG.warning(
                        "template_armory_size_diverged signal=%s "
                        "target_price=%s template_price=%s target_size=%s template_size=%s",
                        key,
                        target.buy_limit,
                        template.price,
                        target.size,
                        template.size,
                    )
                    continue
                newer = self._pending_target.get(key)
                if newer is not None:
                    if newer != target:
                        continue
                    self._pending_target.pop(key, None)
                self._armed[key] = _ArmedState(
                    token_id=template.token_id,
                    buy_limit=template.price,
                    size=template.size,
                    tick=target.tick,
                )
                self._engine.arm(key, template, guard)
                # Loop continues if a newer target was queued during signing.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Drop pending state on failure so the next quote re-attempts.
            self._pending_target.pop(key, None)
            _LOG.warning("template_armory_rearm_failed signal=%s error=%r", key, exc)

    def reset(self) -> None:
        self._armed.clear()
        self._pending_target.clear()
        for task in list(self._inflight_task.values()):
            task.cancel()
        self._inflight_task.clear()

    def retire(self, signal: str) -> None:
        key = signal.upper()
        self._armed.pop(key, None)
        self._pending_target.pop(key, None)
