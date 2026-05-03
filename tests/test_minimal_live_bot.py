import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minimal_live_bot import (
    LiveBot,
    _binance_signal_cfg,
    _entry_decision_cfg,
    _dry_run_order_mode,
    _entry_boundary_config,
    _ensure_calibrated_model,
    _order_type_env,
    cancel_open_orders_once,
    cancel_stale_orders_once,
    ensure_flat_start,
    run_supervised,
)
from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathEngine, HotPathGuard
from order_tracker import LocalOrderTracker


class _Orchestrator:
    def __init__(self) -> None:
        self.exit_calls = 0

    async def on_market_event(self, event: dict) -> None:
        pass

    async def on_user_event(self, event: dict) -> None:
        pass

    async def on_binance_tick_fields(self, *args) -> None:
        pass

    async def evaluate_exit(self) -> None:
        self.exit_calls += 1
        if self.exit_calls >= 2:
            raise RuntimeError("stop")


class _Runtime:
    def __init__(self) -> None:
        self.orchestrator = _Orchestrator()


class _UnknownSubmitter:
    async def submit(self, _template):
        return {"success": False, "_http_status": 0, "error": "transport_error"}


def _fast_template(*, token_id: str = "yes", side: str = "BUY") -> FastOrderTemplate:
    return FastOrderTemplate(
        name="t",
        token_id=token_id,
        side=side,
        price=0.40,
        size=5.0,
        body_bytes=b"x",
    )


def test_supervisor_wires_callbacks_directly_and_runs_exit_tick() -> None:
    runtime = _Runtime()
    seen: list[tuple[str, object]] = []

    async def market_listener(callback):
        seen.append(("market_cb", callback))

    async def user_listener(callback):
        seen.append(("user_cb", callback))

    async def binance_listener(callback):
        seen.append(("binance_cb", callback))

    async def _run() -> None:
        try:
            await run_supervised(
                runtime,
                market_listener=market_listener,
                user_listener=user_listener,
                binance_listener=binance_listener,
                exit_interval_s=0.0,
            )
        except RuntimeError as exc:
            assert str(exc) == "stop"

    asyncio.run(_run())

    assert ("market_cb", runtime.orchestrator.on_market_event) in seen
    assert ("user_cb", runtime.orchestrator.on_user_event) in seen
    assert ("binance_cb", runtime.orchestrator.on_binance_tick_fields) in seen
    assert runtime.orchestrator.exit_calls == 2


def test_supervisor_cancels_siblings_on_listener_failure() -> None:
    runtime = _Runtime()
    cancelled = {"market": False, "binance": False}

    async def market_listener(_callback):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["market"] = True
            raise

    async def user_listener(_callback):
        raise RuntimeError("user failed")

    async def binance_listener(_callback):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["binance"] = True
            raise

    async def _run() -> None:
        try:
            await run_supervised(
                runtime,
                market_listener=market_listener,
                user_listener=user_listener,
                binance_listener=binance_listener,
                exit_interval_s=10.0,
            )
        except RuntimeError as exc:
            assert str(exc) == "user failed"

    asyncio.run(_run())

    assert cancelled == {"market": True, "binance": True}


def test_expire_unknown_submits_once_releases_unknown_buy_cycle_lock() -> None:
    from minimal_live_bot import expire_unknown_submits_once

    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_UnknownSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.set_exposure_scope({"yes", "no"})
    engine.arm("YES", _fast_template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.arm("NO", _fast_template(token_id="no", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)
    engine.update_quote("no", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    blocked = asyncio.run(engine.on_signal("NO"))

    assert first.reason == "submit_unknown"
    assert blocked.reason == "open_exposure"

    runtime = type("_RuntimeWithSubmitState", (), {})()
    runtime.hot_path = engine
    runtime.tracker = tracker

    expired = expire_unknown_submits_once(runtime, now_ts=lambda: 100.0, max_age_s=1.0)
    retried = asyncio.run(engine.on_signal("NO"))

    assert expired == 1
    assert retried.reason == "submit_unknown"


def test_binance_signal_config_uses_current_engine_field_names(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_STRIKE", "100")
    monkeypatch.setenv("MINIMAL_SIGNAL_MAX_SPREAD", "1.5")
    monkeypatch.setenv("MINIMAL_SIGNAL_MIN_ABS_MOVE", "0.25")
    monkeypatch.setenv("MINIMAL_SIGNAL_MIN_ABS_OFI", "2.5")

    cfg = _binance_signal_cfg()

    assert cfg.strike == 100.0
    assert cfg.max_spread == 1.5
    assert cfg.min_abs_move == 0.25
    assert cfg.min_abs_ofi == 2.5


def test_order_type_defaults_to_fak_for_non_resting_hot_path(monkeypatch) -> None:
    monkeypatch.delenv("MINIMAL_ENTRY_ORDER_TYPE", raising=False)
    monkeypatch.delenv("MINIMAL_ALLOW_RESTING_ORDERS", raising=False)

    assert _order_type_env("MINIMAL_ENTRY_ORDER_TYPE", "GTC") == "FAK"


def test_resting_order_type_requires_explicit_guard(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_EXIT_ORDER_TYPE", "GTC")
    monkeypatch.delenv("MINIMAL_ALLOW_RESTING_ORDERS", raising=False)

    try:
        _order_type_env("MINIMAL_EXIT_ORDER_TYPE", "FAK")
    except RuntimeError as exc:
        assert "MINIMAL_ALLOW_RESTING_ORDERS=true" in str(exc)
        return
    raise AssertionError("resting order type was accepted without explicit guard")


def test_resting_order_type_can_be_enabled_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_EXIT_ORDER_TYPE", "GTC")
    monkeypatch.setenv("MINIMAL_ALLOW_RESTING_ORDERS", "true")

    assert _order_type_env("MINIMAL_EXIT_ORDER_TYPE", "FAK") == "GTC"


def test_required_calibrated_model_fails_closed_when_missing() -> None:
    missing = Path("C:/tmp/minimal-missing-signal-model.json")

    try:
        _ensure_calibrated_model(missing, required=True)
    except RuntimeError as exc:
        assert "calibrated signal model" in str(exc)
        return
    raise AssertionError("missing calibrated model did not fail closed")


def test_calibrated_model_guard_accepts_valid_file() -> None:
    path = Path(__file__).resolve().parents[1] / ".pytest-signal-model.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version":1,'
            '"provenance":{"fit_at":"2026-05-01T00:00:00Z","dataset_hash":"abc123",'
            '"sample_count":1024,"holdout_auc":0.62,"holdout_brier":0.22,'
            '"holdout_net_edge_bps":4.0},'
            '"decision":{"min_strength":4.0,"min_edge":0.005,"strength_price_scale":0.025}}',
            encoding="utf-8",
        )
        _ensure_calibrated_model(path, required=True)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def test_calibrated_model_rejects_legacy_unversioned_file() -> None:
    path = Path(__file__).resolve().parents[1] / ".pytest-signal-model-bad.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version":1}', encoding="utf-8")
        try:
            _ensure_calibrated_model(path, required=True)
        except RuntimeError as exc:
            assert "schema_version" in str(exc) or "missing" in str(exc).lower()
            return
        raise AssertionError("legacy file was accepted as a calibrated model")
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def test_startup_ignores_historical_positions_current_run_only_design() -> None:
    class _Http:
        async def dataapi_positions_abs_sum(self, _address: str):
            raise AssertionError("startup must not query historical positions")

        async def clob_open_order_count(self):
            return 0

    asyncio.run(ensure_flat_start(_Http(), "0xabc", allow_dirty_start=False))


def test_dry_run_order_mode_allows_non_transactional_startup(monkeypatch) -> None:
    monkeypatch.delenv("POLY_ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.setenv("MINIMAL_DRY_RUN_ORDERS", "true")

    assert _dry_run_order_mode() is True


def test_order_mode_refuses_startup_without_live_or_dry(monkeypatch) -> None:
    monkeypatch.delenv("POLY_ALLOW_LIVE_ORDERS", raising=False)
    monkeypatch.delenv("MINIMAL_DRY_RUN_ORDERS", raising=False)

    try:
        _dry_run_order_mode()
    except RuntimeError as exc:
        assert "POLY_ALLOW_LIVE_ORDERS=true" in str(exc)
        assert "MINIMAL_DRY_RUN_ORDERS=true" in str(exc)
        return
    raise AssertionError("startup accepted neither live nor dry-run order mode")


def test_entry_boundary_config_requires_live_min_buy_limit(monkeypatch) -> None:
    monkeypatch.delenv("MINIMAL_MIN_BUY_LIMIT", raising=False)
    monkeypatch.setenv("MINIMAL_DECISION_MIN_TTE_US", "30000000")

    try:
        _entry_boundary_config()
    except RuntimeError as exc:
        assert "MINIMAL_MIN_BUY_LIMIT" in str(exc)
        return
    raise AssertionError("startup accepted missing MINIMAL_MIN_BUY_LIMIT")


def test_entry_boundary_config_requires_live_no_entry_window(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_MIN_BUY_LIMIT", "0.10")
    monkeypatch.delenv("MINIMAL_DECISION_MIN_TTE_US", raising=False)

    try:
        _entry_boundary_config()
    except RuntimeError as exc:
        assert "MINIMAL_DECISION_MIN_TTE_US" in str(exc)
        return
    raise AssertionError("startup accepted missing MINIMAL_DECISION_MIN_TTE_US")


def test_entry_boundary_config_accepts_explicit_sane_bounds(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_MIN_BUY_LIMIT", "0.10")
    monkeypatch.setenv("MINIMAL_MAX_BUY_LIMIT", "0.85")
    monkeypatch.setenv("MINIMAL_DECISION_MIN_TTE_US", "30000000")

    assert _entry_boundary_config() == (Decimal("0.10"), Decimal("0.85"), 30_000_000)


def test_entry_decision_config_uses_same_min_max_buy_bounds(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAL_MAX_ASK", "0.60")

    cfg = _entry_decision_cfg(Decimal("0.10"), Decimal("0.85"), 45_000_000)

    assert cfg.min_ask == 0.10
    assert cfg.max_ask == 0.85
    assert cfg.min_tte_us == 45_000_000


def test_startup_open_order_check_fails_closed_on_existing_orders() -> None:
    class _Http:
        async def dataapi_positions_abs_sum(self, _address: str):
            return Decimal("0"), 0

        async def clob_open_order_count(self):
            return 1

    try:
        asyncio.run(ensure_flat_start(_Http(), "0xabc", allow_dirty_start=False))
    except RuntimeError as exc:
        assert "open Polymarket orders" in str(exc)
        return
    raise AssertionError("startup accepted existing open orders")


def test_startup_position_override_flag_bypasses_dirty_state_checks() -> None:
    class _Http:
        async def dataapi_positions_abs_sum(self, _address: str):
            raise AssertionError("startup should not query historical positions")

        async def clob_open_order_count(self):
            raise AssertionError("startup should not query open orders")

    asyncio.run(ensure_flat_start(_Http(), "0xabc", allow_dirty_start=True))


def test_cancel_stale_orders_once_batches_tracker_ids() -> None:
    class _Tracker:
        def stale_live_order_ids(self, *, now_ts: float, max_age_s: float, limit: int):
            assert now_ts == 120.0
            assert max_age_s == 10.0
            assert limit == 50
            return ["oid-1", "oid-2"]

    class _Submitter:
        def __init__(self) -> None:
            self.calls = []

        async def cancel_orders(self, order_ids):
            self.calls.append(order_ids)
            return [{"ok": True}]

    submitter = _Submitter()

    cancelled = asyncio.run(
        cancel_stale_orders_once(
            tracker=_Tracker(),
            submitter=submitter,
            now_ts=lambda: 120.0,
            max_age_s=10.0,
            limit=50,
        )
    )

    assert cancelled == 2
    assert submitter.calls == [["oid-1", "oid-2"]]


def test_cancel_stale_orders_releases_local_sell_reservation_on_accepted_cancel() -> None:
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    tracker.reserve_sell_order("sell-1", "yes", Decimal("5"), now_ts=100.0)

    class _Submitter:
        async def cancel_orders(self, order_ids):
            assert order_ids == ["sell-1"]
            return {"success": True}

    cancelled = asyncio.run(
        cancel_stale_orders_once(
            tracker=tracker,
            submitter=_Submitter(),
            now_ts=lambda: 120.0,
            max_age_s=10.0,
            limit=50,
        )
    )

    assert cancelled == 1
    assert tracker.reserved("yes") == Decimal("0")
    assert tracker.sellable("yes") == Decimal("5")


def test_cancel_open_orders_once_batches_tracker_ids() -> None:
    class _Tracker:
        def live_order_ids(self, *, limit: int):
            assert limit == 50
            return ["oid-1", "oid-2"]

    class _Submitter:
        def __init__(self) -> None:
            self.calls = []

        async def cancel_orders(self, order_ids):
            self.calls.append(order_ids)
            return [{"ok": True}]

    submitter = _Submitter()

    cancelled = asyncio.run(cancel_open_orders_once(tracker=_Tracker(), submitter=submitter, limit=50))

    assert cancelled == 2
    assert submitter.calls == [["oid-1", "oid-2"]]


def test_live_bot_close_cancels_open_orders_before_closing_clients() -> None:
    events = []

    class _Tracker:
        def live_order_ids(self, *, limit: int):
            assert limit == 512
            return ["oid-1"]

    class _RuntimeWithTracker:
        tracker = _Tracker()

    class _Submitter:
        async def cancel_orders(self, order_ids):
            events.append(("cancel", order_ids))
            return [{"ok": True}]

    class _Closable:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            events.append((self.name, None))

    bot = LiveBot(
        runtime=_RuntimeWithTracker(),
        market_cfg=object(),
        http=_Closable("http"),
        session=_Closable("session"),
        submitter=_Submitter(),
    )

    asyncio.run(bot.close())

    assert events == [("cancel", ["oid-1"]), ("http", None), ("session", None)]
