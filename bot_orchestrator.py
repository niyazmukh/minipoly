from __future__ import annotations

import asyncio
import logging
from collections import deque
from decimal import Decimal
from typing import Any, Callable, Protocol

from basis_estimator import BasisEstimator
from binance_signal_engine import BinanceSignalConfig, BinanceSignalEngine
from exit_policy import ExitDecision, ExitPolicyConfig, OpenPosition, decide_exit, price_at_tick
from order_tracker import LocalOrderTracker, TradeState
from polymarket_market_feed import apply_market_event
from runtime_state import MinimalMarket, MinimalRuntimeState
from signal_decision import SignalDecision, SignalDecisionConfig, decide_buy


# Window past slug_ts (microseconds) over which Binance microprice samples are
# averaged to anchor the per-market strike. The user-specified design uses a
# 0.3s window starting at the market slug timestamp.
ANCHOR_WINDOW_END_US = 300_000

# How long the orchestrator retains Binance ticks for retro-anchor
# reconstruction. Must comfortably exceed ANCHOR_WINDOW_END_US plus the
# expected gap between slug_ts and arrival of the gamma context event
# (a few seconds in practice).
ANCHOR_BUFFER_HORIZON_US = 10_000_000


CONTEXT_EVENT_TYPE = "minimal_market_context"
INACTIVE_EVENT_TYPE = "minimal_market_inactive"

_DEC_TWO = Decimal("2")
_LOG = logging.getLogger(__name__)


def _result_attempted_submit(result: Any) -> bool:
    return int(getattr(result, "latency_ns", 0) or 0) > 0


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

    def disarm(self, signal: str) -> None:
        ...


class _ExitArmory(Protocol):
    async def arm_exit(self, decision: ExitDecision, *, quote_ts_ns: int) -> bool:
        ...

    def prepare_exit(self, decision: ExitDecision, *, quote_ts_ns: int) -> bool:
        ...

    def reset(self) -> None:
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
        "_last_signal_status_ns",
        "_anchor_buffer",
        "_pending_anchor_slug_ts",
        "_exit_balance_cooldown",
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
        self._last_signal_status_ns = 0
        self._anchor_buffer: deque[tuple[int, float]] = deque()
        self._pending_anchor_slug_ts: int = 0
        self._exit_balance_cooldown: dict[str, float] = {}

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
        self._prepare_exit_from_state()

    async def on_market_event(self, event: dict[str, Any]) -> bool:
        et = str(event.get("event_type") or event.get("eventType") or event.get("type") or "").strip().lower()
        if et == CONTEXT_EVENT_TYPE:
            self._apply_market_context(event)
            return True
        if et == INACTIVE_EVENT_TYPE:
            self._apply_market_inactive(event)
            return True
        arm_entries = self._entry_allowed()
        if not arm_entries:
            self._disarm_entry_templates()
        changed = await apply_market_event(event, self.state, self._armory, arm_entries=arm_entries)
        if changed:
            self._sync_hot_path_exposure_scope()
            self._update_basis_from_state()
            self._prepare_exit_from_state()
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
                    self._prepare_exit_from_state()
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
        # Append to the anchor buffer BEFORE the trading_active gate so that
        # ticks accumulating before the first market context event are still
        # available for retro-anchor reconstruction.
        self._append_anchor_sample(event_time_us, bid, ask, bid_qty, ask_qty)
        # If a market rotation deferred its anchor pending more samples, retry
        # now that the buffer has grown (or its window has closed).
        if self._pending_anchor_slug_ts > 0:
            self._try_resolve_pending_anchor()
        if not self.state.trading_active:
            return SignalDecision("NO_BUY", "market_inactive")
        signal = self.signal_engine.on_tick_fields(event_time_us, update_id, bid, ask, bid_qty, ask_qty)
        self._maybe_log_signal_status()
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
            bid=float(quote.bid),
            ask=ask_float,
            quote_age_us=quote_age_us,
            tte_us=tte_us,
        )
        _LOG.info(
            "binance_signal_decision action=%s reason=%s side=%s token_id=%s ask=%.4f quote_age_us=%s tte_us=%s edge=%.4f",
            decision.action,
            decision.reason,
            decision.side,
            decision.token_id,
            ask_float,
            quote_age_us,
            tte_us,
            decision.edge,
        )
        if decision.action == "BUY":
            result = await self._hot_path.on_signal(decision.side)
            _LOG.warning("entry_hot_path_result side=%s result=%r", decision.side, result)
            if _result_attempted_submit(result):
                retire = getattr(self._armory, "retire", None)
                if callable(retire):
                    retire(decision.side)
        return decision

    def _maybe_log_signal_status(self) -> None:
        now_ns = self.state.now_ns()
        if now_ns - self._last_signal_status_ns < 5_000_000_000:
            return
        self._last_signal_status_ns = now_ns
        snap = self.signal_engine.snapshot()
        stats = self.signal_engine.stats
        _LOG.info(
            "binance_signal_status accepted=%s signals=%s stale=%s stale_lag=%s spread_rejects=%s strike=%.4f microprice=%.4f ofi=%.4f imbalance=%.4f",
            stats.accepted,
            stats.signals,
            stats.stale_updates + stats.stale_event_time,
            stats.stale_lag,
            stats.spread_rejects,
            self.signal_engine.strike,
            snap.last_microprice,
            snap.last_ofi,
            snap.last_imbalance,
        )

    def _entry_allowed(self) -> bool:
        market = self.state.market
        if market is None:
            return False
        min_tte_us = max(0, int(self._decision_cfg.min_tte_us))
        if min_tte_us <= 0:
            return True
        tte_us = int(max(0.0, market.end_ts - self._now_s()) * 1_000_000)
        return tte_us >= min_tte_us

    def _disarm_entry_templates(self) -> None:
        self._armory.reset()
        self._hot_path.disarm("YES")
        self._hot_path.disarm("NO")

    async def evaluate_exit(self) -> ExitDecision:
        if self._exit_cfg is None or self._exit_armory is None or self._tracker is None:
            return ExitDecision("HOLD", "exit_policy_unconfigured")
        if not self.state.trading_active:
            return ExitDecision("HOLD", "market_inactive")
        if getattr(self._hot_path, "_in_flight_sell", False):
            return ExitDecision("HOLD", "sell_in_flight")
        market = self.state.market
        tte_us = int(max(0.0, (market.end_ts - self._now_s()) if market is not None else 0.0) * 1_000_000)
        now_ns = self.state.now_ns()
        # Iterate ALL positions — not just state.position. Each position is
        # evaluated independently so position B doesn't wait behind position A.
        for asset_id in list(getattr(self._tracker, "owned_by_asset", {}).keys()):
            sellable_size = self._tracker.sellable(asset_id)
            if sellable_size <= 0:
                continue
            owned, entry = self._tracker.position_size_and_entry(asset_id)
            if owned <= 0 or entry <= 0:
                continue
            side = self.state.side_for_token(asset_id)
            if not side:
                continue
            quote = self.state.quotes.get(asset_id)
            if quote is None:
                continue
            # Skip assets that recently got "not enough balance" from the venue.
            # Polymarket confirms trades before tokens settle in the wallet;
            # retrying immediately just wastes API calls against a zero balance.
            cooldown_until = self._exit_balance_cooldown.get(asset_id, 0.0)
            if self._now_s() < cooldown_until:
                continue
            position = OpenPosition(
                side=side,
                token_id=asset_id,
                entry_price=entry,
                size=owned,
                opened_ns=0,
            )
            decision = decide_exit(
                position,
                quote,
                self._exit_cfg,
                now_ns=now_ns,
                tte_us=tte_us,
                sellable_size=sellable_size,
            )
            if decision.action != "SELL":
                continue
            armed = await self._exit_armory.arm_exit(decision, quote_ts_ns=quote.ts_ns)
            if armed:
                result = await self._hot_path.on_signal(decision.signal)
                _LOG.warning("exit_hot_path_result side=%s result=%r", decision.side, result)
                # Detect venue-level "not enough balance" — Polymarket confirmed
                # the trade but tokens haven't settled in the wallet yet.
                # Suppress retries for 2s to let settlement complete.
                resp = getattr(result, "response", None) or {}
                if isinstance(resp, dict) and "not enough balance" in str(resp.get("error", "")):
                    self._exit_balance_cooldown[asset_id] = self._now_s() + 2.0
                if _result_attempted_submit(result):
                    retire = getattr(self._exit_armory, "retire", None)
                    if callable(retire):
                        retire(decision.signal)
                return decision
        return ExitDecision("HOLD", "no_sellable_position")

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
            slug_ts=int(event.get("slug_ts") or 0),
        )
        prior = self.state.market
        rotated = prior is None or prior.condition_id != market.condition_id or prior.slug != market.slug

        self.state.set_market(market)
        self._polymarket_strike = market.strike

        if rotated:
            # Drop stale entry templates and pending sign tasks on rotation;
            # also clear hot-path armed state and exposure lock for this scope.
            self._armory.reset()
            self._hot_path.disarm_all()
            if self._exit_armory is not None:
                self._exit_armory.reset()
            self._anchor_strike_on_rotation(market)
        self._sync_hot_path_exposure_scope()

    def _anchor_strike_on_rotation(self, market: MinimalMarket) -> None:
        """Seed the signal engine's strike for the new market.

        Preference order:
          1. Polymarket Gamma-provided explicit strike (markets like "BTC above
             $X"). Used directly when > 0.
          2. Median Binance microprice over [slug_ts, slug_ts + 0.3s] from the
             tick buffer.
          3. If neither available now, defer until the anchor window closes.
             If still empty after the window, fail closed.
        """
        if market.strike > 0.0:
            self.signal_engine.set_strike(float(market.strike), reset_window=True)
            self._pending_anchor_slug_ts = 0
            return
        if market.slug_ts <= 0:
            # No slug timestamp: cannot anchor; fail closed for this market.
            _LOG.error(
                "anchor_unavailable reason=missing_slug_ts slug=%s condition_id=%s",
                market.slug,
                market.condition_id,
            )
            self.signal_engine.set_strike(0.0, reset_window=True)
            self._pending_anchor_slug_ts = 0
            self._apply_market_inactive({"reason": "anchor_unavailable"})
            return
        # Reset the engine's window first; we'll set the strike either now (if
        # the buffer covers the anchor window) or once it does.
        self.signal_engine.set_strike(0.0, reset_window=True)
        self._pending_anchor_slug_ts = int(market.slug_ts)
        self._try_resolve_pending_anchor()

    def _try_resolve_pending_anchor(self) -> None:
        slug_ts = self._pending_anchor_slug_ts
        if slug_ts <= 0:
            return
        window_start_us = slug_ts * 1_000_000
        window_end_us = window_start_us + ANCHOR_WINDOW_END_US
        samples = [mp for ts, mp in self._anchor_buffer if window_start_us <= ts <= window_end_us]
        # If the latest buffered tick has not yet crossed the window end, we
        # can still get more samples — keep waiting.
        latest_ts = self._anchor_buffer[-1][0] if self._anchor_buffer else 0
        if not samples and latest_ts < window_end_us:
            return
        if not samples:
            # Window closed without any samples — fail closed for this market.
            _LOG.error(
                "anchor_unavailable reason=no_ticks_in_window slug_ts=%s window_us=[%s,%s]",
                slug_ts,
                window_start_us,
                window_end_us,
            )
            self._pending_anchor_slug_ts = 0
            self._apply_market_inactive({"reason": "anchor_unavailable"})
            return
        samples.sort()
        n = len(samples)
        if n % 2 == 1:
            anchor = samples[n // 2]
        else:
            anchor = 0.5 * (samples[n // 2 - 1] + samples[n // 2])
        self.signal_engine.set_strike(float(anchor), reset_window=True)
        self._pending_anchor_slug_ts = 0
        _LOG.warning(
            "anchor_resolved slug_ts=%s samples=%s strike=%.4f", slug_ts, n, anchor
        )

    def _append_anchor_sample(
        self,
        event_time_us: int,
        bid: float,
        ask: float,
        bid_qty: float,
        ask_qty: float,
    ) -> None:
        total_qty = float(bid_qty) + float(ask_qty)
        if bid <= 0.0 or ask <= 0.0 or total_qty <= 0.0:
            return
        microprice = (float(bid) * float(ask_qty) + float(ask) * float(bid_qty)) / total_qty
        if microprice <= 0.0:
            return
        buf = self._anchor_buffer
        buf.append((int(event_time_us), microprice))
        # Trim left side beyond the buffer horizon. Use the latest sample as
        # the reference to keep this O(1) amortized regardless of clock skew.
        cutoff = int(event_time_us) - ANCHOR_BUFFER_HORIZON_US
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _apply_market_inactive(self, event: dict[str, Any]) -> None:
        reason = str(event.get("reason") or "inactive")
        prior_market = self.state.market
        self.state.mark_market_inactive(reason)
        self._armory.reset()
        self._hot_path.disarm_all()
        if self._exit_armory is not None:
            self._exit_armory.reset()
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
        # Telemetry-only: BasisEstimator no longer feeds back into the signal
        # threshold. The signal engine's strike is anchored once per market
        # rotation (see _anchor_strike_on_rotation) and is immutable until
        # the next rotation.
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

    def _prepare_exit_from_state(self) -> None:
        if self._exit_cfg is None or self._exit_armory is None or self._tracker is None:
            return
        if not self.state.trading_active:
            return
        if self.state.position is None:
            self._sync_position_from_tracker()
        position = self.state.position
        if position is None:
            return
        quote = self.state.quotes.get(position.token_id)
        if quote is None or quote.bid <= 0:
            return
        if position.size <= 0:
            return
        self._exit_armory.prepare_exit(
            ExitDecision(
                "SELL",
                "prearm",
                side=position.side,
                token_id=position.token_id,
                size=position.size,
                limit_price=price_at_tick(quote.bid, quote.tick),
                bid=quote.bid,
                ask=quote.ask,
                order_type=self._exit_cfg.order_type,
                signal=self._exit_cfg.signal,
            ),
            quote_ts_ns=quote.ts_ns,
        )

    def _sync_hot_path_exposure_scope(self) -> None:
        market = self.state.market
        if market is None:
            self._hot_path.set_exposure_scope(set())
            return
        self._hot_path.set_exposure_scope({market.yes_token_id, market.no_token_id})
