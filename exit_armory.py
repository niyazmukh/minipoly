from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Protocol

from exit_policy import ExitDecision
from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathGuard

_LOG = logging.getLogger(__name__)


class _Engine(Protocol):
    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        ...

    def update_quote(self, token_id: str, *, bid: Any, ask: Any, ts_ns: int) -> None:
        ...


BuildTemplate = Callable[..., Awaitable[FastOrderTemplate]]


@dataclass(frozen=True, slots=True)
class _PreparedExit:
    decision: ExitDecision
    template: FastOrderTemplate


class ExitArmory:
    __slots__ = (
        "_engine",
        "_build_template",
        "_owner",
        "_max_quote_age_ns",
        "_prepared",
        "_pending",
        "_task",
    )

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
        self._prepared: _PreparedExit | None = None
        self._pending: tuple[ExitDecision, int] | None = None
        self._task: asyncio.Task[None] | None = None

    async def arm_exit(self, decision: ExitDecision, *, quote_ts_ns: int) -> bool:
        if decision.action != "SELL" or decision.size <= 0 or decision.limit_price <= 0:
            return False
        prepared = self._prepared
        if prepared is None or not _same_exit(prepared.decision, decision):
            self.prepare_exit(decision, quote_ts_ns=quote_ts_ns)
            # Await the in-flight signing task so the fill-driven evaluation
            # submits immediately after signing rather than waiting for the
            # next periodic exit-loop iteration.
            task = self._task
            if task is not None and not task.done():
                try:
                    await task
                except Exception:
                    pass
            prepared = self._prepared
            if prepared is None or not _same_exit(prepared.decision, decision):
                return False
        self._arm_prepared(prepared, quote_ts_ns=quote_ts_ns, quote_decision=decision)
        return True

    def prepare_exit(self, decision: ExitDecision, *, quote_ts_ns: int) -> bool:
        if decision.action != "SELL" or decision.size <= 0 or decision.limit_price <= 0:
            return False
        prepared = self._prepared
        if prepared is not None and _same_exit(prepared.decision, decision):
            self._arm_prepared(prepared, quote_ts_ns=quote_ts_ns, quote_decision=decision)
            return False
        pending = self._pending
        if pending is not None and _same_exit(pending[0], decision):
            return False
        self._pending = (decision, int(quote_ts_ns))
        task = self._task
        if task is None or task.done():
            try:
                self._task = asyncio.create_task(self._prepare_loop(), name="minimal-exit-prearm")
            except RuntimeError:
                self._pending = None
                return False
        return True

    def reset(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._prepared = None
        self._pending = None
        self._task = None

    def retire(self, signal: str) -> None:
        prepared = self._prepared
        if prepared is not None and prepared.decision.signal.upper() == signal.upper():
            self._prepared = None

    async def _prepare_loop(self) -> None:
        try:
            while self._pending is not None:
                decision, quote_ts_ns = self._pending
                self._pending = None
                raw = float(decision.limit_price)
                rounded = round(raw, 2)
                _LOG.warning(
                    "exit_sign limit_price=%s raw_float=%.10f rounded=%.10f reason=%s",
                    decision.limit_price, raw, rounded, decision.reason,
                )
                template = await self._build_template(
                    name=f"exit-{decision.reason}",
                    token_id=decision.token_id,
                    side="SELL",
                    price=rounded,
                    size=float(decision.size),
                    owner=self._owner,
                    order_type=decision.order_type,
                    post_only=False,
                )
                prepared = _PreparedExit(decision=decision, template=template)
                self._prepared = prepared
                _LOG.warning(
                    "exit_body_dump price_raw=%.10f price_template=%.10f body=%s",
                    rounded, template.price, template.body_bytes.decode("utf-8", errors="replace"),
                )
                self._arm_prepared(prepared, quote_ts_ns=quote_ts_ns, quote_decision=decision)
        except asyncio.CancelledError:
            raise
        finally:
            if self._task is asyncio.current_task():
                if self._pending is None:
                    self._task = None
                else:
                    self._task = asyncio.create_task(self._prepare_loop(), name="minimal-exit-prearm")

    def _arm_prepared(
        self,
        prepared: _PreparedExit,
        *,
        quote_ts_ns: int,
        quote_decision: ExitDecision | None = None,
    ) -> None:
        decision = quote_decision or prepared.decision
        self._engine.update_quote(
            decision.token_id,
            bid=decision.bid,
            ask=decision.ask,
            ts_ns=quote_ts_ns,
        )
        self._engine.arm(
            decision.signal,
            prepared.template,
            HotPathGuard(max_ask=Decimal("1"), min_bid=decision.limit_price, max_age_ns=self._max_quote_age_ns),
        )


def _same_exit(left: ExitDecision, right: ExitDecision) -> bool:
    return (
        left.signal == right.signal
        and left.token_id == right.token_id
        and left.side == right.side
        and left.size == right.size
        and left.limit_price == right.limit_price
        and left.order_type == right.order_type
    )
