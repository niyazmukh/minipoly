import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basis_estimator import BasisEstimator, BasisEstimatorConfig
from binance_signal_engine import BinanceSignalConfig
from bot_orchestrator import MinimalBotOrchestrator
from exit_policy import ExitPolicyConfig, OpenPosition
from order_tracker import LocalOrderTracker
from runtime_state import MinimalMarket, MinimalRuntimeState
from signal_decision import SignalDecisionConfig


class _HotPath:
    def __init__(self) -> None:
        self.signals: list[str] = []
        self.scopes: list[set[str]] = []
        self.disarmed: list[str] = []

    async def on_signal(self, signal: str):
        self.signals.append(signal)
        return {"submitted": True}

    def set_exposure_scope(self, token_ids: set[str]) -> None:
        self.scopes.append(token_ids)

    def disarm_all(self) -> None:
        self.signals.clear()

    def disarm(self, signal: str) -> None:
        self.disarmed.append(signal)


class _Armory:
    def __init__(self) -> None:
        self.quotes: list[str] = []

    async def on_quote(self, *, signal: str, token_id: str, bid: Decimal, ask: Decimal, tick: Decimal) -> bool:
        self.quotes.append(signal)
        return True

    def reset(self) -> None:
        self.quotes.clear()


class _ExitArmory:
    def __init__(self) -> None:
        self.decisions = []
        self.prepared = []

    async def arm_exit(self, decision, *, quote_ts_ns: int) -> bool:
        self.decisions.append((decision, quote_ts_ns))
        return True

    def prepare_exit(self, decision, *, quote_ts_ns: int) -> bool:
        self.prepared.append((decision, quote_ts_ns))
        return True

    def reset(self) -> None:
        self.decisions.clear()
        self.prepared.clear()


class _CountingTracker(LocalOrderTracker):
    def __init__(self) -> None:
        super().__init__()
        self.owned_calls: list[str] = []

    def owned(self, asset_id: str) -> Decimal:
        self.owned_calls.append(asset_id)
        return super().owned(asset_id)


def _orchestrator() -> tuple[MinimalBotOrchestrator, _HotPath, _Armory]:
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    state.set_market(
        MinimalMarket(
            slug="btc-updown",
            condition_id="c",
            yes_token_id="yes",
            no_token_id="no",
            yes_label="Up",
            no_label="Down",
            start_ts=0.0,
            end_ts=300.0,
        )
    )
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(
            strike=100.0,
            max_lag_us=0,
            min_window_us=200_000,
            min_abs_move=0.15,
            min_abs_ofi=0.20,
            min_imbalance=0.05,
        ),
        # Tests of legacy-shape orchestrator behaviour: opt into the legacy
        # strength-based fair-value path with a permissive prob floor so the
        # decision logic is exercised by the same micro-fixtures that pre-date
        # the Brownian barrier-cross probability model.
        # Tests of legacy-shape orchestrator behaviour: opt into the legacy
        # strength-based fair-value path so the decision logic is exercised
        # by the same micro-fixtures that pre-date the Brownian barrier-cross
        # probability model.
        decision_cfg=SignalDecisionConfig(
            max_ask=0.60,
            min_strength=3.0,
            min_edge=0.0,
            use_legacy_fair=True,
        ),
        now_s=lambda: 100.0,
    )
    return bot, hot_path, armory


def test_market_context_uses_gamma_strike_without_basis_offset() -> None:
    # BasisEstimator is now telemetry-only and must not influence the
    # threshold. The signal engine's strike is the Gamma-supplied value
    # verbatim when present.
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    basis = BasisEstimator(BasisEstimatorConfig(seed_basis=7.5, seed_weight=1.0))
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
        basis_estimator=basis,
    )

    asyncio.run(
        bot.on_market_event(
            {
                "event_type": "minimal_market_context",
                "slug": "btc-updown",
                "condition_id": "cond",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "start_ts": 0.0,
                "end_ts": 300.0,
                "strike": 100.0,
            }
        )
    )

    assert bot.signal_engine.strike == 100.0


def test_orchestrator_updates_quotes_and_submits_on_valid_binance_signal() -> None:
    bot, hot_path, armory = _orchestrator()

    asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.49", "best_ask": "0.51"}
        )
    )
    assert armory.quotes == ["YES"]

    assert asyncio.run(bot.on_binance_tick_fields(1_000_000, 1, 99.90, 100.10, 3.0, 3.0)) is None
    decision = asyncio.run(bot.on_binance_tick_fields(1_300_000, 2, 100.20, 100.40, 5.0, 2.0))

    assert decision is not None
    assert decision.action == "BUY"
    assert decision.side == "YES"
    assert hot_path.signals == ["YES"]
    assert {"yes", "no"} in hot_path.scopes


def test_orchestrator_updates_exposure_scope_when_market_changes() -> None:
    bot, hot_path, _armory = _orchestrator()
    bot.state.set_market(
        MinimalMarket(
            slug="btc-next",
            condition_id="c2",
            yes_token_id="yes2",
            no_token_id="no2",
            yes_label="Up",
            no_label="Down",
            start_ts=300.0,
            end_ts=600.0,
        )
    )

    asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes2", "best_bid": "0.49", "best_ask": "0.51"}
        )
    )

    assert hot_path.scopes[-1] == {"yes2", "no2"}


def test_orchestrator_blocks_when_polymarket_quote_is_missing_or_expensive() -> None:
    bot, hot_path, _armory = _orchestrator()

    bot.signal_engine.on_tick_fields(1_000_000, 1, 99.90, 100.10, 3.0, 3.0)
    missing = asyncio.run(bot.on_binance_tick_fields(1_300_000, 2, 100.20, 100.40, 5.0, 2.0))
    assert missing is not None
    assert missing.reason == "quote_missing"
    assert hot_path.signals == []

    asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.80", "best_ask": "0.80"}
        )
    )
    bot.signal_engine.reset_signal_cooldown()
    expensive = asyncio.run(bot.on_binance_tick_fields(1_600_000, 3, 100.50, 100.70, 5.0, 2.0))
    assert expensive is not None
    assert expensive.reason == "ask_above_limit"
    assert hot_path.signals == []


def test_near_expiry_quote_updates_state_but_disarms_entry_templates() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    state.set_market(
        MinimalMarket(
            slug="btc-updown",
            condition_id="c",
            yes_token_id="yes",
            no_token_id="no",
            yes_label="Up",
            no_label="Down",
            start_ts=0.0,
            end_ts=101.0,
        )
    )
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=100.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60, min_tte_us=2_000_000),
        now_s=lambda: 100.0,
    )
    armory.quotes.append("YES")

    changed = asyncio.run(
        bot.on_market_event(
            {
                "event_type": "best_bid_ask",
                "asset_id": "yes",
                "best_bid": "0.49",
                "best_ask": "0.51",
            }
        )
    )

    assert changed is True
    assert state.quote_for_side("YES").ask == Decimal("0.51")  # type: ignore[union-attr]
    assert armory.quotes == []
    assert hot_path.disarmed == ["YES", "NO"]


def test_orchestrator_arms_and_fires_exit_when_policy_triggers() -> None:
    bot, hot_path, _armory = _orchestrator()
    exit_armory = _ExitArmory()
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-1",
            "asset_id": "yes",
            "side": "BUY",
            "size": "6",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    bot.configure_exit_policy(
        ExitPolicyConfig(take_profit_bps=1000, stop_loss_bps=1500),
        exit_armory=exit_armory,
        tracker=tracker,
    )
    bot.state.set_position(
        OpenPosition(
            side="YES",
            token_id="yes",
            entry_price=Decimal("0.50"),
            size=Decimal("10"),
            opened_ns=900_000_000,
        )
    )
    asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.56", "best_ask": "0.57"}
        )
    )

    decision = asyncio.run(bot.evaluate_exit())

    assert decision.action == "SELL"
    assert decision.reason == "take_profit"
    assert decision.size == Decimal("6")
    assert exit_armory.decisions[0][0] == decision
    assert hot_path.signals[-1] == "EXIT"


def test_orchestrator_derives_position_from_user_wss_buy_before_exit_can_arm() -> None:
    bot, hot_path, _armory = _orchestrator()
    exit_armory = _ExitArmory()
    tracker = LocalOrderTracker()
    bot.configure_exit_policy(
        ExitPolicyConfig(take_profit_bps=1000, stop_loss_bps=1500),
        exit_armory=exit_armory,
        tracker=tracker,
    )
    asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.56", "best_ask": "0.57"}
        )
    )

    before_confirm = asyncio.run(bot.evaluate_exit())
    assert before_confirm.action == "HOLD"
    assert before_confirm.reason == "no_position"
    assert hot_path.signals == []

    asyncio.run(
        bot.on_user_event(
            {
                "event_type": "trade",
                "id": "buy-2",
                "asset_id": "yes",
                "side": "BUY",
                "size": "7",
                "price": "0.50",
                "status": "MATCHED",
            }
        )
    )

    decision = asyncio.run(bot.evaluate_exit())

    assert bot.state.position is not None
    assert bot.state.position.size == Decimal("7")
    assert bot.state.position.entry_price == Decimal("0.50")
    assert decision.action == "SELL"
    assert decision.size == Decimal("7")
    assert hot_path.signals[-1] == "EXIT"


def test_orchestrator_blocks_entry_and_exit_when_market_inactive() -> None:
    bot, hot_path, armory = _orchestrator()
    exit_armory = _ExitArmory()
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-3",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    bot.configure_exit_policy(
        ExitPolicyConfig(take_profit_bps=1000, stop_loss_bps=1500),
        exit_armory=exit_armory,
        tracker=tracker,
    )
    asyncio.run(bot.on_market_event({"event_type": "market_resolved", "market": "c"}))
    ignored = asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.56", "best_ask": "0.57"}
        )
    )

    bot.signal_engine.on_tick_fields(1_000_000, 1, 99.90, 100.10, 3.0, 3.0)
    entry = asyncio.run(bot.on_binance_tick_fields(1_300_000, 2, 100.20, 100.40, 5.0, 2.0))
    exit_decision = asyncio.run(bot.evaluate_exit())

    assert ignored is False
    assert entry is not None
    assert entry.reason == "market_inactive"
    assert exit_decision.reason == "market_inactive"
    assert armory.quotes == []
    assert hot_path.signals == []


def test_orchestrator_skips_binance_signal_engine_when_market_inactive() -> None:
    bot, _hot_path, _armory = _orchestrator()
    asyncio.run(bot.on_market_event({"event_type": "market_resolved", "market": "c"}))

    decision = asyncio.run(bot.on_binance_tick_fields(1_000_000, 1, 99.90, 100.10, 3.0, 3.0))

    assert decision is not None
    assert decision.reason == "market_inactive"
    assert bot.signal_engine.stats.accepted == 0


def test_exit_sync_checks_current_position_token_without_scanning_both_sides() -> None:
    bot, _hot_path, _armory = _orchestrator()
    exit_armory = _ExitArmory()
    tracker = _CountingTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-current",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    bot.configure_exit_policy(
        ExitPolicyConfig(take_profit_bps=1000, stop_loss_bps=1500),
        exit_armory=exit_armory,
        tracker=tracker,
    )
    tracker.owned_calls.clear()
    asyncio.run(
        bot.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.52", "best_ask": "0.53"}
        )
    )

    decision = asyncio.run(bot.evaluate_exit())

    assert decision.action == "HOLD"
    assert tracker.owned_calls == ["yes"]


def test_zero_strike_market_context_without_slug_ts_fails_closed() -> None:
    # Direction-only ("Up or Down") markets have strike=0 in metadata. Without
    # a slug_ts the orchestrator cannot anchor the strike and must fail closed
    # rather than trading on uninitialised threshold.
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
    )

    asyncio.run(
        bot.on_market_event(
            {
                "event_type": "minimal_market_context",
                "slug": "btc-updown-5m-1234",
                "condition_id": "cond",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "start_ts": 0.0,
                "end_ts": 300.0,
                "strike": 0.0,
                # slug_ts intentionally absent → fail closed
            }
        )
    )

    assert state.trading_active is False
    assert bot.signal_engine.strike == 0.0


def test_zero_strike_anchor_resolves_from_buffered_binance_ticks() -> None:
    # Direction-only market with slug_ts=1234. Pre-feed Binance ticks that
    # fall inside [slug_ts*1e6, slug_ts*1e6 + 0.3s] and the orchestrator must
    # set the engine strike to the median microprice and activate trading.
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
    )

    slug_ts = 1234
    base_us = slug_ts * 1_000_000
    # Three ticks inside the anchor window — same microprice 100.0.
    asyncio.run(bot.on_binance_tick_fields(base_us + 50_000, 1, 99.90, 100.10, 1.0, 1.0))
    asyncio.run(bot.on_binance_tick_fields(base_us + 150_000, 2, 99.90, 100.10, 1.0, 1.0))
    asyncio.run(bot.on_binance_tick_fields(base_us + 250_000, 3, 99.90, 100.10, 1.0, 1.0))
    # One tick AFTER the window so resolve sees the window has closed.
    asyncio.run(bot.on_binance_tick_fields(base_us + 500_000, 4, 99.90, 100.10, 1.0, 1.0))

    asyncio.run(
        bot.on_market_event(
            {
                "event_type": "minimal_market_context",
                "slug": "btc-updown-5m-1234",
                "condition_id": "cond",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "start_ts": 0.0,
                "end_ts": 300.0,
                "strike": 0.0,
                "slug_ts": slug_ts,
            }
        )
    )

    assert state.trading_active is True
    assert bot.signal_engine.strike == 100.0


def test_zero_strike_anchor_window_passed_without_ticks_fails_closed() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
    )

    slug_ts = 1234
    base_us = slug_ts * 1_000_000
    # Tick OUTSIDE the [slug_ts, slug_ts+0.3s] window but AFTER the window
    # closed — this signals "window is closed, no samples available".
    asyncio.run(bot.on_binance_tick_fields(base_us + 600_000, 1, 99.90, 100.10, 1.0, 1.0))

    asyncio.run(
        bot.on_market_event(
            {
                "event_type": "minimal_market_context",
                "slug": "btc-updown-5m-1234",
                "condition_id": "cond",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "start_ts": 0.0,
                "end_ts": 300.0,
                "strike": 0.0,
                "slug_ts": slug_ts,
            }
        )
    )

    assert state.trading_active is False
    assert bot.signal_engine.strike == 0.0


def test_zero_strike_anchor_defers_when_no_samples_yet_and_window_open() -> None:
    # Context arrives before any Binance ticks have entered the anchor
    # window. The orchestrator marks anchor pending and the strike stays
    # unset until a tick lands inside the window.
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
    )

    slug_ts = 1234
    base_us = slug_ts * 1_000_000

    asyncio.run(
        bot.on_market_event(
            {
                "event_type": "minimal_market_context",
                "slug": "btc-updown-5m-1234",
                "condition_id": "cond",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "start_ts": 0.0,
                "end_ts": 300.0,
                "strike": 0.0,
                "slug_ts": slug_ts,
            }
        )
    )

    assert bot.signal_engine.strike == 0.0
    assert bot._pending_anchor_slug_ts == slug_ts

    # In-window tick triggers anchor resolution.
    asyncio.run(bot.on_binance_tick_fields(base_us + 100_000, 1, 99.90, 100.10, 1.0, 1.0))

    assert bot.signal_engine.strike == 100.0
    assert bot._pending_anchor_slug_ts == 0
    assert state.trading_active is True


def test_basis_estimator_does_not_alter_signal_engine_strike() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    basis = BasisEstimator(BasisEstimatorConfig(seed_basis=11.0, seed_weight=1.0))
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
        basis_estimator=basis,
    )
    asyncio.run(
        bot.on_market_event(
            {
                "event_type": "minimal_market_context",
                "slug": "btc-updown",
                "condition_id": "cond",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "start_ts": 0.0,
                "end_ts": 300.0,
                "strike": 50.0,
            }
        )
    )
    # Even with a non-zero basis, the engine's strike must be the gamma value.
    assert bot.signal_engine.strike == 50.0


def test_anchor_buffer_evicts_beyond_horizon() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    hot_path = _HotPath()
    armory = _Armory()
    bot = MinimalBotOrchestrator(
        state=state,
        armory=armory,
        hot_path=hot_path,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
    )
    asyncio.run(bot.on_binance_tick_fields(1_000_000, 1, 99.90, 100.10, 1.0, 1.0))
    # Many seconds later, the old tick must be evicted.
    asyncio.run(bot.on_binance_tick_fields(50_000_000, 2, 99.90, 100.10, 1.0, 1.0))
    times = [ts for ts, _ in bot._anchor_buffer]
    assert times == [50_000_000]
