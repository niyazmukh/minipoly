from __future__ import annotations

from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol

from exit_policy import ExitDecision
from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathGuard


class _Engine(Protocol):
    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        ...

    def update_quote(self, token_id: str, *, bid: Any, ask: Any, ts_ns: int) -> None:
        ...


BuildTemplate = Callable[..., Awaitable[FastOrderTemplate]]


class ExitArmory:
    __slots__ = ("_engine", "_build_template", "_owner", "_max_quote_age_ns")

    def __init__(
        self,
        *,
        engine: _Engine,
        build_template: BuildTemplate,
        owner: str,
        max_quote_age_ns: int,
    ) -> None:
        self._engine = engine
        self._build_template = build_template
        self._owner = owner
        self._max_quote_age_ns = int(max_quote_age_ns)

    async def arm_exit(self, decision: ExitDecision, *, quote_ts_ns: int) -> bool:
        if decision.action != "SELL" or decision.size <= 0 or decision.limit_price <= 0:
            return False
        template = await self._build_template(
            name=f"exit-{decision.reason}",
            token_id=decision.token_id,
            side="SELL",
            price=float(decision.limit_price),
            size=float(decision.size),
            owner=self._owner,
            order_type=decision.order_type,
            post_only=False,
        )
        self._engine.update_quote(
            decision.token_id,
            bid=decision.bid,
            ask=decision.ask,
            ts_ns=quote_ts_ns,
        )
        self._engine.arm(
            decision.signal,
            template,
            HotPathGuard(max_ask=Decimal("1"), min_bid=decision.limit_price, max_age_ns=self._max_quote_age_ns),
        )
        return True
