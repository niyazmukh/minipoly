from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Callable, Protocol

from basis_estimator import BasisEstimator
from binance_signal_engine import BinanceSignalConfig, BinanceSignalEngine
from exit_policy import ExitDecision, ExitPolicyConfig, OpenPosition, decide_exit
from order_tracker import LocalOrderTracker, TradeState
from polymarket_market_feed import apply_market_event
from runtime_state import MinimalMarket, MinimalRuntimeState
from signal_decision import SignalDecision, SignalDecisionConfig, decide_buy


CONTEXT_EVENT_TYPE = "minimal_market_context"
INACTIVE_EVENT_TYPE = "minimal_market_inactive"

_DEC_TWO = Decimal("2")


class _Armory(Protocol):
    async def on_quote(self, **kwargs: Any) -> bool:
        ...

    def reset(self) -> None:
        ...


class _HotPath(Protocol):
    async def on_signal(self, signal: str) -> Any:
        ...

    def set_exposure_scope(self, token_ids: set[str]) -> None:
        ...

    def disarm_all(self) -> None:
        ...


class _ExitArmory(Protocol):
    async def arm_exit(self, decision: ExitDecision, *, quote_ts_ns: int) -> bool:
        ...


class MinimalBotOrchestrator:
    __slots__ = (
        "state",
        "signal_engine",
        "_decision_cfg",
        "_hot_path",
        "_armory",
        "_now_s",
        "_exit_cfg",
        "_exit_armory",
        "_tracker",
        "_basis",
        "_polymarket_strike",
        "_exit_task",
        "_exit_dirty",
    )

    def __init__(
        self,
        *,
        state: MinimalRuntimeState,
        armory: _Armory,
        hot_path: _HotPath,
        signal_cfg: BinanceSignalConfig,
        decision_cfg: SignalDecisionConfig,
        now_s: Callable[[], float],
        basis_estimator: BasisEstimator | None = None,
    ) -> None:
        self.state = state
        self.signal_engine = BinanceSignalEngine(signal_cfg)
        self._decision_cfg = decision_cfg
        self._hot_path = hot_path
        self._armory = armory
        self._now_s = now_s
        self._exit_cfg: ExitPolicyConfig | None = None
        self._exit_armory: _ExitArmory | None = None
        self._tracker: LocalOrderTracker | None = None
        self._basis = basis_estimator
        self._polymarket_strike: float = 0.0
        self._exit_task: asyncio.Task[None] | None = None
        self._exit_dirty: bool = False

    def configure_exit_policy(
        self,
        cfg: ExitPolicyConfig,
        *,
        exit_armory: _ExitArmory,
        tracker: LocalOrderTracker,
    ) -> None:
        self._exit_cfg = cfg
        self._exit_armory = exit_armory
        self._tracker = tracker
        self._sync_position_from_tracker()

    async def on_market_event(self, event: dict[str, Any]) -> bool:
        et = str(event.get("event_type") or event.get("eventType") or event.get("type") or "").strip().lower()
        if et == CONTEXT_EVENT_TYPE:
            self._apply_market_context(event)
            return True
        if et == INACTIVE_EVENT_TYPE:
            self._apply_market_inactive(event)
            return True
        changed = await apply_market_event(event, self.state, self._armory)
        if changed:
            self._sync_hot_path_exposure_scope()
            self._update_basis_from_state()
        return changed

    async def on_user_event(self, event: dict[str, Any]) -> bool:
        if self._tracker is None:
            return False
        kind = str(event.get("event_type") or event.get("eventType") or event.get("type") or "").strip().lower()
        changed = None
        if kind == "trade":
            changed = self._tracker.on_trade_event(event)
            if isinstance(changed, TradeState):
                self._sync_position_from_tracker(changed.asset_id)
                if changed.side == "BUY" and changed.applied:
                    # Schedule exit evaluation off the user-WS recv loop. The
                    # signing+HTTP cost of arm_exit must not block dispatch
                    # of subsequent user events. Single-flight: if an exit
                    # evaluation is already in flight, mark it dirty so it
                    # re-runs after completion.
                    self._schedule_exit_evaluation()
        elif kind == "order" or kind in {"placement", "update", "cancellation"}:
            changed = self._tracker.on_order_event(event)
        return changed is not None

    def _schedule_exit_evaluation(self) -> None:
        if self._exit_armory is None or self._tracker is None:
            return
        self._exit_dirty = True
        existing = self._exit_task
        if existing is not None and not existing.done():
            return
        try:
            self._exit_task = asyncio.create_task(
                self._exit_evaluation_loop(),
                name="minimal-fill-driven-exit",
            )
        except RuntimeError:
            # No running loop available. The dirty flag stays set so the
            # next call (e.g. the periodic exit loop) will re-evaluate.
            self._exit_dirty = False

    async def _exit_evaluation_loop(self) -> None:
        try:
            while self._exit_dirty:
                self._exit_dirty = False
                await self.evaluate_exit()
        finally:
            self._exit_task = None

    async def on_binance_tick_fields(
        self,
        event_time_us: int,
        update_id: int,
        bid: float,
        ask: float,
        bid_qty: float,
        ask_qty: float,
    ) -> SignalDecision | None:
        if not self.state.trading_active:
            return SignalDecision("NO_BUY", "market_inactive")
        signal = self.signal_engine.on_tick_fields(event_time_us, update_id, bid, ask, bid_qty, ask_qty)
        if signal is None:
            return None

        quote = self.state.quote_for_side(signal.side)
        if quote is None:
            return SignalDecision("NO_BUY", "quote_missing", side=signal.side)
        ask_float = float(quote.ask)
        quote_age_us = self.state.quote_age_us(quote.token_id)
        market = self.state.market
        tte_us = int(max(0.0, (market.end_ts - self._now_s()) if market is not None else 0.0) * 1_000_000)
        decision = decide_buy(
            signal,
            self.state.contract,
            self._decision_cfg,
            ask=ask_float,
            quote_age_us=quote_age_us,
            tte_us=tte_us,
        )
        if decision.action == "BUY":
            await self._hot_path.on_signal(decision.side)
        return decision

    async def evaluate_exit(self) -> ExitDecision:
        if self._exit_cfg is None or self._exit_armory is None or self._tracker is None:
            return ExitDecision("HOLD", "exit_policy_unconfigured")
        if not self.state.trading_active:
            return ExitDecision("HOLD", "market_inactive")
        self._sync_position_from_tracker()
        position = self.state.position
        quote = self.state.quotes.get(position.token_id) if position is not None else None
        market = self.state.market
        tte_us = int(max(0.0, (market.end_ts - self._now_s()) if market is not None else 0.0) * 1_000_000)
        sellable = self._tracker.sellable(position.token_id) if position is not None else Decimal("0")
        decision = decide_exit(
            position,
            quote,
            self._exit_cfg,
            now_ns=self.state.now_ns(),
            tte_us=tte_us,
            sellable_size=sellable,
        )
        if decision.action != "SELL" or quote is None:
            return decision
        armed = await self._exit_armory.arm_exit(decision, quote_ts_ns=quote.ts_ns)
        if armed:
            await self._hot_path.on_signal(decision.signal)
        return decision

    # ---- market context plumbing -----------------------------------------

    def _apply_market_context(self, event: dict[str, Any]) -> None:
        market = MinimalMarket(
            slug=str(event.get("slug") or ""),
            condition_id=str(event.get("condition_id") or event.get("conditionId") or ""),
            yes_token_id=str(event.get("yes_token_id") or ""),
            no_token_id=str(event.get("no_token_id") or ""),
            yes_label=str(event.get("yes_label") or "Up"),
            no_label=str(event.get("no_label") or "Down"),
            start_ts=float(event.get("start_ts") or 0.0),
            end_ts=float(event.get("end_ts") or 0.0),
            strike=float(event.get("strike") or 0.0),
        )
        prior = self.state.market
        rotated = prior is None or prior.condition_id != market.condition_id or prior.slug != market.slug

        if market.strike <= 0.0:
            self._apply_market_inactive({"reason": "missing_strike"})
            return
        self.state.set_market(market)
        self._polymarket_strike = market.strike

        if rotated:
            # Drop stale entry templates and pending sign tasks on rotation;
            # also clear hot-path armed state and exposure lock for this scope.
            self._armory.reset()
            self._hot_path.disarm_all()
            reference = self.signal_engine.fresh_microprice(self.signal_engine.max_lag_us)
            self.signal_engine.set_strike(reference, reset_window=True)
        self._sync_hot_path_exposure_scope()

    def _apply_market_inactive(self, event: dict[str, Any]) -> None:
        reason = str(event.get("reason") or "inactive")
        prior_market = self.state.market
        self.state.mark_market_inactive(reason)
        self._armory.reset()
        self._hot_path.disarm_all()
        self._hot_path.set_exposure_scope(set())
        # Polymarket settles the resolved market; our locally-tracked owned
        # tokens for that market are no longer ours to sell. Drop them so a
        # subsequent rotation does not see ghost inventory.
        if (
            self._tracker is not None
            and prior_market is not None
            and reason in {"resolved", "market_resolved"}
        ):
            self._tracker.release_market_inventory(
                {prior_market.yes_token_id, prior_market.no_token_id}
            )

    def _update_basis_from_state(self) -> None:
        if self._basis is None or self._polymarket_strike <= 0.0:
            return
        market = self.state.market
        if market is None:
            return
        yes_quote = self.state.quotes.get(market.yes_token_id)
        if yes_quote is None or yes_quote.bid <= 0 or yes_quote.ask <= 0:
            return
        yes_mid = float((yes_quote.bid + yes_quote.ask) / _DEC_TWO)
        binance_micro = self.signal_engine.last_microprice
        if binance_micro <= 0.0:
            return
        max_age_us = self.signal_engine.max_lag_us
        if max_age_us > 0 and self.signal_engine.last_microprice_age_us() > max_age_us:
            return
        tte_us = int(max(0.0, market.end_ts - self._now_s()) * 1_000_000)
        self._basis.update(
            binance_microprice=binance_micro,
            yes_mid=yes_mid,
            strike=self._polymarket_strike,
            tte_us=tte_us,
        )

    # ---- position sync ---------------------------------------------------

    def _sync_position_from_tracker(self, token_id: str = "") -> None:
        if self._tracker is None:
            return
        market = self.state.market
        if market is None:
            return
        candidates = (token_id,) if token_id else (market.yes_token_id, market.no_token_id)
        for asset_id in candidates:
            side = self.state.side_for_token(asset_id)
            if not side:
                continue
            owned, entry = self._tracker.position_size_and_entry(asset_id)
            if owned <= 0:
                if self.state.position is not None and self.state.position.token_id == asset_id:
                    self.state.clear_position()
                continue
            if entry <= 0:
                continue
            current = self.state.position
            if (
                current is not None
                and current.token_id == asset_id
                and current.size == owned
                and current.entry_price == entry
            ):
                return  # no-op when tracker matches state
            opened_ns = current.opened_ns if current is not None and current.token_id == asset_id else self.state.now_ns()
            self.state.set_position(
                OpenPosition(
                    side=side,
                    token_id=asset_id,
                    entry_price=entry,
                    size=owned,
                    opened_ns=opened_ns,
                )
            )
            return

    def _sync_hot_path_exposure_scope(self) -> None:
        market = self.state.market
        if market is None:
            self._hot_path.set_exposure_scope(set())
            return
        self._hot_path.set_exposure_scope({market.yes_token_id, market.no_token_id})
