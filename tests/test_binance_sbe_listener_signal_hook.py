import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_sbe_listener import CompiledMessage, RuntimeConfig, _consume_best_bid_ask
from binance_signal_engine import BinanceTick


class _WS:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = frames

    async def recv(self) -> bytes:
        if not self.frames:
            raise RuntimeError("done")
        return self.frames.pop(0)


class _YieldThenDoneWS:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame
        self.sent = False

    async def recv(self) -> bytes:
        if not self.sent:
            self.sent = True
            return self.frame
        await asyncio.sleep(0)
        raise RuntimeError("done")


def _cfg() -> RuntimeConfig:
    return RuntimeConfig(
        ws_url="",
        api_key="",
        schema_url="",
        message_name="BestBidAskStreamEvent",
        decode_symbol=False,
        status_interval_ms=10_000,
        max_queue=1,
        open_timeout_s=1.0,
        close_timeout_s=1.0,
        reconnect_min_s=0.1,
        reconnect_max_s=1.0,
        reconnect_factor=1.5,
        disable_gc=False,
    )


def _cfg_status_every_tick() -> RuntimeConfig:
    return RuntimeConfig(
        ws_url="",
        api_key="",
        schema_url="",
        message_name="BestBidAskStreamEvent",
        decode_symbol=False,
        status_interval_ms=0,
        max_queue=1,
        open_timeout_s=1.0,
        close_timeout_s=1.0,
        reconnect_min_s=0.1,
        reconnect_max_s=1.0,
        reconnect_factor=1.5,
        disable_gc=False,
    )


def _spec() -> CompiledMessage:
    return CompiledMessage(
        schema_url="",
        schema_id=1,
        schema_version=1,
        message_name="BestBidAskStreamEvent",
        template_id=42,
        header_struct=struct.Struct("<HH"),
        header_index={"blockLength": 0, "templateId": 1},
        root_struct=struct.Struct("<QQqqqqii"),
        root_index={
            "eventTime": 0,
            "bookUpdateId": 1,
            "bidPrice": 2,
            "bidQty": 3,
            "askPrice": 4,
            "askQty": 5,
            "priceExponent": 6,
            "qtyExponent": 7,
        },
        symbol_len_bytes=0,
    )


def test_consumer_calls_tick_hook_before_status_throttle() -> None:
    spec = _spec()
    frame = spec.header_struct.pack(spec.root_struct.size, spec.template_id) + spec.root_struct.pack(
        1_000_000,
        7,
        100_000,
        5_000,
        100_100,
        4_000,
        -3,
        -3,
    )
    seen: list[BinanceTick] = []

    async def _run() -> None:
        try:
            await _consume_best_bid_ask(_WS([frame]), _cfg(), spec, on_tick=seen.append)
        except RuntimeError:
            pass

    asyncio.run(_run())

    assert seen == [
        BinanceTick(
            event_time_us=1_000_000,
            update_id=7,
            bid=100.0,
            ask=100.1,
            bid_qty=5.0,
            ask_qty=4.0,
        )
    ]


def test_consumer_calls_field_hook_without_allocating_tick_object() -> None:
    spec = _spec()
    frame = spec.header_struct.pack(spec.root_struct.size, spec.template_id) + spec.root_struct.pack(
        2_000_000,
        8,
        101_000,
        6_000,
        101_200,
        3_000,
        -3,
        -3,
    )
    seen: list[tuple[int, int, float, float, float, float]] = []

    async def _run() -> None:
        try:
            await _consume_best_bid_ask(
                _WS([frame]),
                _cfg(),
                spec,
                on_tick_fields=lambda event_time_us, update_id, bid, ask, bid_qty, ask_qty: seen.append(
                    (event_time_us, update_id, bid, ask, bid_qty, ask_qty)
                ),
            )
        except RuntimeError:
            pass

    asyncio.run(_run())

    assert seen == [(2_000_000, 8, 101.0, 101.2, 6.0, 3.0)]


def test_consumer_callback_mode_suppresses_status_prints(monkeypatch) -> None:
    spec = _spec()
    frame = spec.header_struct.pack(spec.root_struct.size, spec.template_id) + spec.root_struct.pack(
        3_000_000,
        9,
        102_000,
        7_000,
        102_200,
        2_000,
        -3,
        -3,
    )
    printed: list[str] = []
    monkeypatch.setattr("binance_sbe_listener._safe_print", printed.append)

    async def _run() -> None:
        try:
            await _consume_best_bid_ask(
                _WS([frame]),
                _cfg_status_every_tick(),
                spec,
                on_tick_fields=lambda *_args: None,
            )
        except RuntimeError:
            pass

    asyncio.run(_run())

    assert printed == []


def test_consumer_does_not_await_async_field_hook_before_next_recv() -> None:
    spec = _spec()
    first = spec.header_struct.pack(spec.root_struct.size, spec.template_id) + spec.root_struct.pack(
        4_000_000,
        10,
        103_000,
        8_000,
        103_200,
        2_000,
        -3,
        -3,
    )
    second = spec.header_struct.pack(spec.root_struct.size, spec.template_id) + spec.root_struct.pack(
        4_010_000,
        11,
        103_100,
        8_000,
        103_300,
        2_000,
        -3,
        -3,
    )
    # Record dispatch synchronously at call time (before task runs) so that
    # the assertion holds regardless of when or whether the task executes.
    dispatched: list[int] = []

    def slow_hook(_event_time_us, update_id, _bid, _ask, _bid_qty, _ask_qty):
        dispatched.append(update_id)

        async def _slow_work():
            await asyncio.sleep(10)

        return _slow_work()

    async def _run() -> None:
        try:
            await asyncio.wait_for(
                _consume_best_bid_ask(_WS([first, second]), _cfg(), spec, on_tick_fields=slow_hook),
                timeout=0.05,
            )
        except (RuntimeError, asyncio.TimeoutError):
            pass

    asyncio.run(_run())

    assert dispatched == [10, 11]


def test_consumer_drains_async_callback_tasks_on_exit() -> None:
    spec = _spec()
    frame = spec.header_struct.pack(spec.root_struct.size, spec.template_id) + spec.root_struct.pack(
        4_020_000,
        12,
        103_200,
        8_000,
        103_400,
        2_000,
        -3,
        -3,
    )

    async def _run() -> None:
        started = asyncio.Event()
        cleanup_done = asyncio.Event()

        def slow_hook(*_args):
            async def _slow_work():
                started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cleanup_done.set()
                    raise

            return _slow_work()

        try:
            await _consume_best_bid_ask(_YieldThenDoneWS(frame), _cfg(), spec, on_tick_fields=slow_hook)
        except RuntimeError:
            pass

        assert started.is_set()
        assert cleanup_done.is_set()

    asyncio.run(_run())
