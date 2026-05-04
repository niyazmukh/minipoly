import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_signal_engine import BinanceSignalConfig
from exit_policy import ExitPolicyConfig
from fast_order_submitter import FastOrderTemplate
from runtime_state import MinimalMarket, MinimalRuntimeState
from runtime_wiring import RuntimeWiringConfig, build_runtime
from signal_decision import SignalDecisionConfig
from template_armory import ArmoryConfig


class _Submitter:
    def __init__(self) -> None:
        self.calls: list[FastOrderTemplate] = []

    async def submit(self, template: FastOrderTemplate) -> dict:
        self.calls.append(template)
        return {"success": True, "orderID": "oid-exit"}


class _Builder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> FastOrderTemplate:
        self.calls.append(kwargs)
        return FastOrderTemplate(
            name=kwargs["name"],
            token_id=kwargs["token_id"],
            side=kwargs["side"],
            price=float(kwargs["price"]),
            size=float(kwargs["size"]),
            body_bytes=b'{"order":1}',
        )


def _state() -> MinimalRuntimeState:
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
    return state


def _runtime():
    submitter = _Submitter()
    builder = _Builder()
    runtime = build_runtime(
        RuntimeWiringConfig(owner="owner"),
        state=_state(),
        submitter=submitter,
        build_template=builder,
        entry_cfg=ArmoryConfig(usdc_per_trade=Decimal("10")),
        exit_cfg=ExitPolicyConfig(take_profit_bps=1000, stop_loss_bps=1500),
        signal_cfg=BinanceSignalConfig(strike=100.0),
        decision_cfg=SignalDecisionConfig(max_ask=0.60),
        now_s=lambda: 100.0,
        now_ns=lambda: 1_000_000_000,
    )
    return runtime, submitter, builder


def test_runtime_wires_exit_to_user_confirmation_before_submit() -> None:
    runtime, submitter, _builder = _runtime()
    asyncio.run(
        runtime.orchestrator.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.56", "best_ask": "0.57"}
        )
    )

    before = asyncio.run(runtime.orchestrator.evaluate_exit())
    assert before.action == "HOLD"
    assert before.reason == "no_sellable_position"
    assert submitter.calls == []

    submit_id = runtime.tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.50"), now_ts=99.9)
    runtime.tracker.confirm_submit_order_id(submit_id, "buy-order-1", now_ts=100.0)
    asyncio.run(
        runtime.orchestrator.on_user_event(
            {
                "event_type": "trade",
                "id": "buy-1",
                "taker_order_id": "buy-order-1",
                "asset_id": "yes",
                "side": "BUY",
                "size": "5",
                "price": "0.50",
                "status": "MATCHED",
            }
        )
    )

    assert submitter.calls == []

    asyncio.run(
        runtime.orchestrator.on_user_event(
            {
                "event_type": "trade",
                "id": "buy-1",
                "taker_order_id": "buy-order-1",
                "asset_id": "yes",
                "side": "BUY",
                "size": "5",
                "price": "0.50",
                "status": "CONFIRMED",
            }
        )
    )

    assert len(submitter.calls) == 1
    assert submitter.calls[0].side == "SELL"
    assert submitter.calls[0].size == 5.0


def test_runtime_uses_same_tracker_for_policy_and_hot_path_sell_gate() -> None:
    runtime, submitter, _builder = _runtime()
    submit_id = runtime.tracker.register_submit("entry", "yes", "BUY", Decimal("4"), Decimal("0.50"), now_ts=99.9)
    runtime.tracker.confirm_submit_order_id(submit_id, "buy-order-2", now_ts=100.0)
    asyncio.run(
        runtime.orchestrator.on_user_event(
            {
                "event_type": "trade",
                "id": "buy-2",
                "taker_order_id": "buy-order-2",
                "asset_id": "yes",
                "side": "BUY",
                "size": "4",
                "price": "0.50",
                "status": "MATCHED",
            }
        )
    )
    asyncio.run(
        runtime.orchestrator.on_user_event(
            {
                "event_type": "trade",
                "id": "buy-2",
                "taker_order_id": "buy-order-2",
                "asset_id": "yes",
                "side": "BUY",
                "size": "4",
                "price": "0.50",
                "status": "CONFIRMED",
            }
        )
    )
    asyncio.run(
        runtime.orchestrator.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.56", "best_ask": "0.57"}
        )
    )

    first = asyncio.run(runtime.orchestrator.evaluate_exit())
    assert first.action == "SELL"
    assert len(submitter.calls) == 1

    runtime.tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-1",
            "taker_order_id": "oid-exit",
            "asset_id": "yes",
            "side": "SELL",
            "size": "4",
            "price": "0.56",
            "status": "MATCHED",
        }
    )
    runtime.tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-1",
            "taker_order_id": "oid-exit",
            "asset_id": "yes",
            "side": "SELL",
            "size": "4",
            "price": "0.56",
            "status": "CONFIRMED",
        }
    )
    runtime.hot_path.disarm("EXIT")

    second = asyncio.run(runtime.orchestrator.evaluate_exit())

    assert second.action == "HOLD"
    assert second.reason == "no_sellable_position"
    assert len(submitter.calls) == 1
