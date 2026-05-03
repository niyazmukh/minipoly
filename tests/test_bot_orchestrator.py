import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

    async def on_signal(self, signal: str):
        self.signals.append(signal)
        return {"submitted": True}

    def set_exposure_scope(self, token_ids: set[str]) -> None:
        self.scopes.append(token_ids)


class _Armory:
    def __init__(self) -> None:
        self.quotes: list[str] = []

    async def on_quote(self, *, signal: str, token_id: str, bid: Decimal, ask: Decimal, tick: Decimal) -> bool:
        self.quotes.append(signal)
        return True


class _ExitArmory:
    def __init__(self) -> None:
        self.decisions = []

    async def arm_exit(self, decision, *, quote_ts_ns: int) -> bool:
        self.decisions.append((decision, quote_ts_ns))
        return True


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
        decision_cfg=SignalDecisionConfig(max_ask=0.60, min_strength=3.0, min_edge=0.0),
        now_s=lambda: 100.0,
    )
    return bot, hot_path, armory


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
