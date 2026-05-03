import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basis_estimator import BasisEstimator, BasisEstimatorConfig
from binance_signal_engine import BinanceSignalConfig
from bot_orchestrator import CONTEXT_EVENT_TYPE, INACTIVE_EVENT_TYPE
from exit_policy import ExitPolicyConfig
from runtime_state import MinimalRuntimeState
from runtime_wiring import RuntimeWiringConfig, build_runtime
from signal_decision import SignalDecisionConfig
from template_armory import ArmoryConfig
from fast_order_submitter import FastOrderTemplate


class _Submitter:
    def __init__(self) -> None:
        self.calls: list[FastOrderTemplate] = []

    async def submit(self, template):
        self.calls.append(template)
        return {"orderID": "oid-" + template.name}


async def _build_template(**kwargs) -> FastOrderTemplate:
    return FastOrderTemplate(
        name=kwargs.get("name", "tpl"),
        token_id=kwargs.get("token_id", "yes"),
        side=kwargs.get("side", "BUY"),
        price=float(kwargs.get("price", 0.5)),
        size=float(kwargs.get("size", 1.0)),
        body_bytes=b'{"order":"x"}',
    )


def _runtime(basis: BasisEstimator | None = None, signal_cfg: BinanceSignalConfig | None = None):
    submitter = _Submitter()
    rt = build_runtime(
        RuntimeWiringConfig(owner="api-key"),
        state=MinimalRuntimeState(now_ns=lambda: 1),
        submitter=submitter,
        build_template=_build_template,
        entry_cfg=ArmoryConfig(usdc_per_trade=Decimal("10")),
        exit_cfg=ExitPolicyConfig(),
        signal_cfg=signal_cfg or BinanceSignalConfig(strike=0.0, max_lag_us=0, max_spread=0.0),
        decision_cfg=SignalDecisionConfig(max_ask=0.6),
        now_s=lambda: 100.0,
        now_ns=lambda: 1,
        basis_estimator=basis,
    )
    return rt, submitter


def test_market_context_event_bootstraps_state_and_waits_for_binance_reference() -> None:
    rt, _ = _runtime()
    ctx = {
        "event_type": CONTEXT_EVENT_TYPE,
        "slug": "btc-updown-5m-1",
        "condition_id": "cond-1",
        "yes_token_id": "yes",
        "no_token_id": "no",
        "yes_label": "Up",
        "no_label": "Down",
        "start_ts": 0,
        "end_ts": 400.0,
        "strike": 40000.0,
    }

    asyncio.run(rt.orchestrator.on_market_event(ctx))

    assert rt.state.market is not None
    assert rt.state.market.condition_id == "cond-1"
    assert rt.state.trading_active is True
    assert rt.state.contract.is_valid is True
    # Binance is the trend source; the signal threshold is captured from the
    # first fresh Binance microprice for this market, not from Polymarket text.
    assert rt.orchestrator.signal_engine.strike == 0.0


def test_market_context_event_does_not_apply_basis_offset_as_binance_threshold() -> None:
    basis = BasisEstimator(BasisEstimatorConfig(seed_basis=20.0, seed_weight=1.0))
    rt, _ = _runtime(basis)
    asyncio.run(
        rt.orchestrator.on_market_event(
            {
                "event_type": CONTEXT_EVENT_TYPE,
                "slug": "s",
                "condition_id": "c",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "end_ts": 400.0,
                "strike": 40000.0,
            }
        )
    )
    assert rt.orchestrator.signal_engine.strike == 0.0


def test_market_inactive_event_clears_state_and_armory() -> None:
    rt, _ = _runtime()
    asyncio.run(
        rt.orchestrator.on_market_event(
            {
                "event_type": CONTEXT_EVENT_TYPE,
                "slug": "s",
                "condition_id": "c",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "end_ts": 400.0,
                "strike": 40000.0,
            }
        )
    )
    assert rt.state.trading_active is True

    asyncio.run(rt.orchestrator.on_market_event({"event_type": INACTIVE_EVENT_TYPE, "reason": "disconnected"}))

    assert rt.state.trading_active is False


def test_rotation_resets_signal_window_and_lock() -> None:
    rt, _ = _runtime()
    # Boot first market and pretend the signal engine has accumulated state.
    asyncio.run(
        rt.orchestrator.on_market_event(
            {
                "event_type": CONTEXT_EVENT_TYPE,
                "slug": "btc-updown-5m-1",
                "condition_id": "c1",
                "yes_token_id": "yes1",
                "no_token_id": "no1",
                "yes_label": "Up",
                "no_label": "Down",
                "end_ts": 400.0,
                "strike": 40000.0,
            }
        )
    )
    rt.orchestrator.signal_engine.on_tick_fields(1, 1, 39990.0, 40010.0, 1.0, 1.0)
    rt.orchestrator.signal_engine.on_tick_fields(2, 2, 39995.0, 40005.0, 1.0, 1.0)
    assert rt.orchestrator.signal_engine.snapshot().ticks > 0

    asyncio.run(
        rt.orchestrator.on_market_event(
            {
                "event_type": CONTEXT_EVENT_TYPE,
                "slug": "btc-updown-5m-2",
                "condition_id": "c2",
                "yes_token_id": "yes2",
                "no_token_id": "no2",
                "yes_label": "Up",
                "no_label": "Down",
                "end_ts": 700.0,
                "strike": 41000.0,
            }
        )
    )

    assert rt.orchestrator.signal_engine.snapshot().ticks == 0
    assert rt.orchestrator.signal_engine.strike == 0.0


def test_basis_updated_from_quote_when_yes_mid_near_50() -> None:
    basis = BasisEstimator(
        BasisEstimatorConfig(alpha=1.0, mid_tol=0.05, min_tte_us=1_000_000, seed_basis=0.0, seed_weight=0.0)
    )
    rt, _ = _runtime(basis)
    asyncio.run(
        rt.orchestrator.on_market_event(
            {
                "event_type": CONTEXT_EVENT_TYPE,
                "slug": "s",
                "condition_id": "c",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "end_ts": 1000.0,
                "strike": 40000.0,
            }
        )
    )
    # Push a Binance tick to populate last_microprice.
    rt.orchestrator.signal_engine.on_tick_fields(1, 1, 40015.0, 40025.0, 1.0, 1.0)
    # Now apply a Polymarket best_bid_ask for YES at midprice 0.50.
    asyncio.run(
        rt.orchestrator.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.49", "best_ask": "0.51"}
        )
    )

    assert basis.initialized is True
    # microprice = (40015*1 + 40025*1)/2 = 40020. basis = 40020 - 40000 = 20.
    assert abs(basis.basis - 20.0) < 1e-6
    # The market-start Binance reference is already the signal threshold.
    assert abs(rt.orchestrator.signal_engine.strike - 40020.0) < 1e-6


def test_basis_ignores_stale_binance_microprice() -> None:
    now_us = [1_000_100]
    basis = BasisEstimator(
        BasisEstimatorConfig(alpha=1.0, mid_tol=0.05, min_tte_us=1_000_000, seed_basis=0.0, seed_weight=0.0)
    )
    rt, _ = _runtime(
        basis,
        signal_cfg=BinanceSignalConfig(strike=0.0, max_lag_us=500, max_spread=0.0),
    )
    rt.orchestrator.signal_engine._now_us = lambda: now_us[0]
    asyncio.run(
        rt.orchestrator.on_market_event(
            {
                "event_type": CONTEXT_EVENT_TYPE,
                "slug": "s",
                "condition_id": "c",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "yes_label": "Up",
                "no_label": "Down",
                "end_ts": 1000.0,
                "strike": 40000.0,
            }
        )
    )
    rt.orchestrator.signal_engine.on_tick_fields(1_000_000, 1, 40015.0, 40025.0, 1.0, 1.0)
    now_us[0] = 2_000_000

    asyncio.run(
        rt.orchestrator.on_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.49", "best_ask": "0.51"}
        )
    )

    assert basis.initialized is False
    assert basis.basis == 0.0
