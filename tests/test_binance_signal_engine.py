import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_signal_engine import BinanceSignalEngine, BinanceSignalConfig, BinanceTick


def _tick(
    *,
    event_time_us: int,
    update_id: int,
    bid: float,
    ask: float,
    bid_qty: float = 3.0,
    ask_qty: float = 3.0,
) -> BinanceTick:
    return BinanceTick(
        event_time_us=event_time_us,
        update_id=update_id,
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
    )


def test_engine_rejects_non_monotonic_updates_without_changing_state() -> None:
    engine = BinanceSignalEngine(BinanceSignalConfig(strike=100.0, min_window_us=200_000, max_lag_us=0))

    assert engine.on_tick(_tick(event_time_us=1_000_000, update_id=10, bid=99.9, ask=100.1)) is None
    before = engine.snapshot()

    stale = engine.on_tick(_tick(event_time_us=1_100_000, update_id=10, bid=100.5, ask=100.7))

    assert stale is None
    assert engine.snapshot() == before
    assert engine.stats.stale_updates == 1


def test_engine_rejects_ticks_when_exchange_lag_is_too_high() -> None:
    engine = BinanceSignalEngine(
        BinanceSignalConfig(strike=100.0, max_lag_us=50_000),
        now_us=lambda: 1_200_001,
    )

    signal = engine.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=100.0, ask=100.2))

    assert signal is None
    assert engine.stats.stale_lag == 1
    assert engine.snapshot().ticks == 0


def test_yes_signal_requires_event_time_window_and_positive_ofi() -> None:
    engine = BinanceSignalEngine(
        BinanceSignalConfig(
            strike=100.0,
            max_lag_us=0,
            min_window_us=300_000,
            min_abs_move=0.20,
            min_abs_ofi=0.50,
            min_imbalance=0.10,
            max_spread=1.00,
        )
    )

    assert engine.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=99.90, ask=100.10)) is None
    assert engine.on_tick(_tick(event_time_us=1_100_000, update_id=2, bid=100.20, ask=100.40, bid_qty=5.0, ask_qty=2.0)) is None

    signal = engine.on_tick(_tick(event_time_us=1_400_000, update_id=3, bid=100.50, ask=100.70, bid_qty=6.0, ask_qty=2.0))

    assert signal is not None
    assert signal.side == "YES"
    assert signal.update_id == 3
    assert signal.reason == "microprice_momentum"
    assert signal.ofi > 0
    assert signal.imbalance > 0


def test_no_signal_requires_downward_move_and_negative_ofi() -> None:
    engine = BinanceSignalEngine(
        BinanceSignalConfig(
            strike=100.0,
            max_lag_us=0,
            min_window_us=300_000,
            min_abs_move=0.20,
            min_abs_ofi=0.50,
            min_imbalance=0.10,
            max_spread=1.00,
        )
    )

    engine.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=100.20, ask=100.40, bid_qty=2.0, ask_qty=5.0))
    engine.on_tick(_tick(event_time_us=1_200_000, update_id=2, bid=99.90, ask=100.10, bid_qty=2.0, ask_qty=6.0))

    signal = engine.on_tick(_tick(event_time_us=1_500_000, update_id=3, bid=99.60, ask=99.80, bid_qty=2.0, ask_qty=7.0))

    assert signal is not None
    assert signal.side == "NO"
    assert signal.ofi < 0
    assert signal.imbalance < 0


def test_zero_strike_does_not_signal_and_does_not_self_bootstrap() -> None:
    # Strike anchoring is now external (see MinimalBotOrchestrator). The
    # engine must NOT auto-bootstrap from the first tick — that historical
    # behaviour produced silently-wrong strikes.
    engine = BinanceSignalEngine(
        BinanceSignalConfig(
            strike=0.0,
            max_lag_us=0,
            min_window_us=200_000,
            min_abs_move=0.20,
            min_abs_ofi=0.20,
            min_imbalance=0.05,
            max_spread=1.00,
        )
    )

    first = engine.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=99.90, ask=100.10))
    second = engine.on_tick(_tick(event_time_us=1_300_000, update_id=2, bid=100.50, ask=100.70, bid_qty=5.0, ask_qty=2.0))

    assert first is None
    assert second is None
    assert engine.strike == 0.0


def test_set_strike_then_signal_includes_strike_and_sigma_px() -> None:
    engine = BinanceSignalEngine(
        BinanceSignalConfig(
            strike=0.0,
            max_lag_us=0,
            min_window_us=200_000,
            min_abs_move=0.20,
            min_abs_ofi=0.20,
            min_imbalance=0.05,
            max_spread=1.00,
        )
    )
    engine.set_strike(100.0, reset_window=True)

    engine.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=99.90, ask=100.10))
    signal = engine.on_tick(_tick(event_time_us=1_300_000, update_id=2, bid=100.50, ask=100.70, bid_qty=5.0, ask_qty=2.0))

    assert signal is not None
    assert signal.side == "YES"
    assert signal.move > 0
    assert signal.strike == 100.0
    # Two distinct microprice samples in the window, so sigma_px must be > 0.
    assert signal.sigma_px > 0.0


def test_listener_style_callback_fires_hot_path_once() -> None:
    async def _run() -> list[str]:
        calls: list[str] = []

        async def on_signal(side: str) -> None:
            calls.append(side)

        engine = BinanceSignalEngine(
            BinanceSignalConfig(
                strike=100.0,
                max_lag_us=0,
                min_window_us=200_000,
                min_abs_move=0.15,
                min_abs_ofi=0.20,
                min_imbalance=0.05,
            )
        )
        engine.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=99.90, ask=100.10))
        signal = engine.on_tick(_tick(event_time_us=1_300_000, update_id=2, bid=100.20, ask=100.40, bid_qty=5.0, ask_qty=2.0))
        if signal is not None:
            await on_signal(signal.side)
        duplicate = engine.on_tick(_tick(event_time_us=1_400_000, update_id=3, bid=100.30, ask=100.50, bid_qty=5.0, ask_qty=2.0))
        if duplicate is not None:
            await on_signal(duplicate.side)
        return calls

    assert asyncio.run(_run()) == ["YES"]


def test_field_api_matches_tick_api_without_tick_allocation() -> None:
    cfg = BinanceSignalConfig(
        strike=100.0,
        max_lag_us=0,
        min_window_us=200_000,
        min_abs_move=0.15,
        min_abs_ofi=0.20,
        min_imbalance=0.05,
    )
    from_tick = BinanceSignalEngine(cfg)
    from_fields = BinanceSignalEngine(cfg)

    assert from_tick.on_tick(_tick(event_time_us=1_000_000, update_id=1, bid=99.90, ask=100.10)) is None
    assert from_fields.on_tick_fields(1_000_000, 1, 99.90, 100.10, 3.0, 3.0) is None

    tick_signal = from_tick.on_tick(_tick(event_time_us=1_300_000, update_id=2, bid=100.20, ask=100.40, bid_qty=5.0, ask_qty=2.0))
    field_signal = from_fields.on_tick_fields(1_300_000, 2, 100.20, 100.40, 5.0, 2.0)

    assert tick_signal == field_signal
