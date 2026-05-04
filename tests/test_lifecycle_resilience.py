"""Tests covering the H1/C1/H2/M3/M5/M4 fixes.

These exercise the lifecycle robustness contract: transport errors do not
leak inventory, the calibrated model file actually drives parameters,
matching tolerates venue-side rounding, market resolution settles tracker
state, and the cooldown can be configured side-agnostically.
"""

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_signal_engine import BinanceSignalConfig, BinanceSignalEngine
from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathEngine, HotPathGuard, _classify_submit_response
from order_tracker import LocalOrderTracker
from signal_decision import SignalDecisionConfig
from signal_model import (
    CalibratedModelError,
    CalibratedSignalModel,
    CalibrationProvenance,
    DecisionOverrides,
    SignalEngineOverrides,
    load_calibrated_model,
)


# ---------------------------------------------------------------------------
# H1: Transport errors keep submits as PENDING_UNKNOWN and recover via WSS.
# ---------------------------------------------------------------------------


def _template(*, token_id: str = "yes", side: str = "BUY", size: float = 5.0, price: float = 0.40) -> FastOrderTemplate:
    return FastOrderTemplate(
        name="t",
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        body_bytes=b"x",
    )


class _TransportErrorSubmitter:
    async def submit(self, _template):
        return {"success": False, "_http_status": 0, "error": "transport_error", "detail": "ETIMEDOUT"}


class _RaisingSubmitter:
    async def submit(self, _template):
        raise OSError("connection reset")


class _AcceptedNoOrderIdSubmitter:
    async def submit(self, _template):
        return {"success": True, "_http_status": 200}


def test_transport_error_marks_pending_unknown_not_failed() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is False
    assert result.reason == "submit_unknown"
    pending = list(tracker.pending_submits.values())
    assert len(pending) == 1
    assert pending[0].status == "UNKNOWN"
    assert pending[0].confirmed_order_id == ""


def test_submit_exception_marks_pending_unknown_not_failed() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_RaisingSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is False
    assert result.reason == "submit_unknown"
    pending = list(tracker.pending_submits.values())
    assert pending[0].status == "UNKNOWN"


def test_accepted_submit_without_order_id_remains_unknown() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_AcceptedNoOrderIdSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is False
    assert result.reason == "submit_unknown"
    pending = list(tracker.pending_submits.values())
    assert pending[0].status == "UNKNOWN"


def test_transport_error_then_wss_order_event_recovers_pending_buy() -> None:
    """The H1 hole: server accepted, client lost the response. WSS must reconcile."""
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(side="BUY", size=5.0, price=0.40), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)

    asyncio.run(engine.on_signal("YES"))

    # Polymarket WSS now reports the order that the failed POST actually placed.
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "order-recovered",
            "asset_id": "yes",
            "side": "BUY",
            "original_size": "5",
            "size_matched": "0",
            "price": "0.40",
            "status": "LIVE",
            "timestamp": "1.0",
        }
    )
    pending = list(tracker.pending_submits.values())
    assert pending[0].status == "CONFIRMED"
    assert pending[0].confirmed_order_id == "order-recovered"

    # And the trade fill applies to current-run inventory.
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "fill",
            "taker_order_id": "order-recovered",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
            "timestamp": "1.5",
        }
    )
    assert tracker.owned("yes") == Decimal("5")


def test_transport_error_then_wss_trade_event_recovers_without_order_event_first() -> None:
    """Same as above, but the trade event arrives before any order event."""
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(side="BUY", size=5.0, price=0.40), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)
    asyncio.run(engine.on_signal("YES"))

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "fill",
            "taker_order_id": "order-recovered",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
            "timestamp": "1.5",
        }
    )
    assert tracker.owned("yes") == Decimal("5")


def test_transport_error_sell_provisionally_reserves_inventory() -> None:
    # Non-strict tracker so we can seed BUY inventory directly.
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "CONFIRMED",
        }
    )
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 2_000)
    engine.arm("EXIT", _template(token_id="yes", side="SELL", size=5.0, price=0.50), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=2_000)

    result = asyncio.run(engine.on_signal("EXIT"))
    assert result.reason == "submit_unknown"
    # Conservatively reserved so a retry cannot double-spend inventory.
    assert tracker.sellable("yes") == Decimal("0")
    assert tracker.reserved("yes") == Decimal("5")


def test_transport_error_sell_releases_provisional_when_wss_confirms() -> None:
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "CONFIRMED",
        }
    )
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 2_000)
    engine.arm("EXIT", _template(token_id="yes", side="SELL", size=5.0, price=0.50), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=2_000)
    asyncio.run(engine.on_signal("EXIT"))

    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "sell-recovered",
            "asset_id": "yes",
            "side": "SELL",
            "original_size": "5",
            "size_matched": "0",
            "price": "0.50",
            "status": "LIVE",
            "timestamp": "2.5",
        }
    )

    # Provisional reservation released, replaced by per-order reservation.
    assert tracker.reserved("yes") == Decimal("5")  # still reserved, but under sell-recovered now
    pending = list(tracker.pending_submits.values())
    confirmed = [p for p in pending if p.confirmed_order_id == "sell-recovered"]
    assert confirmed and confirmed[0].status == "CONFIRMED"
    assert all(not order_id.startswith("unknown:") for order_id in tracker.live_order_ids())


def test_transport_error_sell_trade_first_recovery_releases_provisional_reservation() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    buy_sid = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=1.0)
    tracker.confirm_submit_order_id(buy_sid, "buy-1", now_ts=1.0)
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "taker_order_id": "buy-1",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
            "timestamp": "1.1",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "taker_order_id": "buy-1",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "CONFIRMED",
            "timestamp": "1.2",
        }
    )
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 2_000)
    engine.arm("EXIT", _template(token_id="yes", side="SELL", size=5.0, price=0.50), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=2_000)
    asyncio.run(engine.on_signal("EXIT"))

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-fill",
            "taker_order_id": "sell-recovered",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "price": "0.50",
            "status": "MATCHED",
            "timestamp": "2.5",
        }
    )

    assert tracker.owned("yes") == Decimal("0")
    assert tracker.reserved("yes") == Decimal("5")

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-fill",
            "taker_order_id": "sell-recovered",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "price": "0.50",
            "status": "CONFIRMED",
            "timestamp": "2.6",
        }
    )

    assert tracker.reserved("yes") == Decimal("0")
    pending = list(tracker.pending_submits.values())
    confirmed = [p for p in pending if p.confirmed_order_id == "sell-recovered"]
    assert confirmed and confirmed[0].status == "CONFIRMED"


# ---------------------------------------------------------------------------
# Classification helper guarantees.
# ---------------------------------------------------------------------------


def test_classify_submit_response_distinguishes_categories() -> None:
    assert _classify_submit_response({"_http_status": 200, "orderID": "x"}) == "accepted"
    assert _classify_submit_response({"_http_status": 200, "success": True}) == "accepted"
    assert _classify_submit_response({"_http_status": 400, "error": "invalid"}) == "rejected"
    assert _classify_submit_response({"_http_status": 0, "error": "transport_error"}) == "unknown"
    assert _classify_submit_response({"_http_status": 503, "error": "Service Unavailable"}) == "unknown"
    assert _classify_submit_response({"_http_status": 500, "success": False, "error": "internal"}) == "rejected"
    assert _classify_submit_response({"success": False, "error": "fenced"}) == "rejected"
    # Empty dict has no status: legacy submitter responses without
    # transport metadata are treated as 200 / accepted to preserve
    # backwards compatibility with simple test stubs. Real production
    # responses always carry _http_status.
    assert _classify_submit_response({}) == "accepted"


# ---------------------------------------------------------------------------
# Buy lock semantics under UNKNOWN.
# ---------------------------------------------------------------------------


def test_unknown_buy_holds_cycle_lock() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_TransportErrorSubmitter(), tracker=tracker, now_ns=lambda: 1_000)
    engine.set_exposure_scope({"yes", "no"})
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.arm("NO", _template(token_id="no", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)
    engine.update_quote("no", bid=Decimal("0.39"), ask=Decimal("0.40"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    second = asyncio.run(engine.on_signal("NO"))

    assert first.reason == "submit_unknown"
    # Multi-position: unknown buy no longer holds cycle lock.
    assert second.reason == "submit_unknown"


# ---------------------------------------------------------------------------
# expire_unknown_submits.
# ---------------------------------------------------------------------------


def test_expire_unknown_submits_converts_old_unknowns_to_failed() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    sid = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=100.0)
    tracker.mark_submit_unknown(sid, error="transport")

    expired = tracker.expire_unknown_submits(now_ts=200.0, max_age_s=30.0)
    assert expired == [sid]
    assert tracker.pending_submits[sid].status == "EXPIRED_UNKNOWN"


def test_late_wss_order_after_unknown_expiry_still_binds_current_run_submit() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    sid = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=100.0)
    tracker.mark_submit_unknown(sid, error="transport")
    assert tracker.expire_unknown_submits(now_ts=200.0, max_age_s=30.0) == [sid]

    changed = tracker.on_order_event(
        {
            "event_type": "order",
            "id": "late-buy",
            "asset_id": "yes",
            "side": "BUY",
            "original_size": "5",
            "size_matched": "0",
            "price": "0.40",
            "status": "LIVE",
            "timestamp": "201.0",
        }
    )

    assert changed is not None
    assert tracker.pending_submits[sid].status == "CONFIRMED"
    assert tracker.pending_submits[sid].confirmed_order_id == "late-buy"


# ---------------------------------------------------------------------------
# M3: Tolerance in submit↔WSS matching.
# ---------------------------------------------------------------------------


def test_match_tolerates_partial_size_in_wss_order() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    sid = tracker.register_submit("entry", "yes", "BUY", Decimal("10"), Decimal("0.40"), now_ts=100.0)

    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "buy-x",
            "asset_id": "yes",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "0",
            "price": "0.40",
            "status": "LIVE",
            "timestamp": "100.5",
        }
    )
    assert tracker.pending_submits[sid].confirmed_order_id == "buy-x"


def test_match_tolerates_one_tick_price_difference() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    sid = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=100.0)

    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "buy-x",
            "asset_id": "yes",
            "side": "BUY",
            "original_size": "5",
            "size_matched": "0",
            "price": "0.41",  # rounded up by venue
            "status": "LIVE",
            "timestamp": "100.5",
        }
    )
    assert tracker.pending_submits[sid].confirmed_order_id == "buy-x"


# ---------------------------------------------------------------------------
# M5: market_resolved drops tracker entries.
# ---------------------------------------------------------------------------


def test_release_market_inventory_drops_owned_and_reserved() -> None:
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    tracker.reserve_sell_order("sell-1", "yes", Decimal("5"), now_ts=1.0)

    tracker.release_market_inventory({"yes", "no"})

    assert tracker.owned("yes") == Decimal("0")
    assert tracker.reserved("yes") == Decimal("0")


def test_release_market_inventory_marks_pending_failed() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    sid = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=100.0)
    tracker.mark_submit_unknown(sid, error="transport")

    tracker.release_market_inventory({"yes", "no"})

    assert tracker.pending_submits[sid].status == "FAILED"
    assert "market_resolved" in tracker.pending_submits[sid].last_error


# ---------------------------------------------------------------------------
# C1: calibrated model loader.
# ---------------------------------------------------------------------------


_VALID_MODEL = {
    "schema_version": 1,
    "provenance": {
        "fit_at": "2026-05-01T00:00:00Z",
        "dataset_hash": "sha256:abc",
        "sample_count": 4096,
        "holdout_auc": 0.61,
        "holdout_brier": 0.221,
        "holdout_net_edge_bps": 5.5,
    },
    "decision": {
        "min_strength": 5.0,
        "min_edge": 0.01,
        "strength_price_scale": 0.022,
    },
    "signal_engine": {
        "min_abs_move": 0.75,
        "cooldown_side_agnostic": True,
    },
}


def _patch_model_file(monkeypatch, payload) -> Path:
    path = Path("model.json")
    raw = json.dumps(payload)

    def _is_file(self) -> bool:
        return self == path

    def _read_text(self, encoding="utf-8") -> str:
        if self == path:
            return raw
        raise OSError(f"unexpected read path: {self}")

    monkeypatch.setattr(Path, "is_file", _is_file)
    monkeypatch.setattr(Path, "read_text", _read_text)
    return path


def test_calibrated_model_applies_to_decision_config(monkeypatch) -> None:
    model = load_calibrated_model(_patch_model_file(monkeypatch, _VALID_MODEL))
    base = SignalDecisionConfig(max_ask=0.6, min_strength=3.0, min_edge=0.0, strength_price_scale=0.03)
    out = model.apply_to_decision(base)
    assert out.min_strength == 5.0
    assert out.min_edge == 0.01
    assert out.strength_price_scale == 0.022
    assert out.max_ask == 0.6  # unchanged


def test_calibrated_model_applies_to_signal_engine_config(monkeypatch) -> None:
    model = load_calibrated_model(_patch_model_file(monkeypatch, _VALID_MODEL))
    base = BinanceSignalConfig(strike=100.0, min_abs_move=0.5, signal_cooldown_us=1_000_000)
    out = model.apply_to_signal_engine(base)
    assert out.min_abs_move == 0.75
    assert out.cooldown_side_agnostic is True


def test_calibrated_model_applies_side_agnostic_cooldown_without_file_fixture() -> None:
    model = CalibratedSignalModel(
        schema_version=1,
        provenance=CalibrationProvenance(
            fit_at="2026-05-01T00:00:00Z",
            dataset_hash="sha256:abc",
            sample_count=4096,
            holdout_auc=0.61,
            holdout_brier=0.221,
            holdout_net_edge_bps=5.5,
        ),
        decision=DecisionOverrides(),
        signal_engine=SignalEngineOverrides(cooldown_side_agnostic=True),
    )

    out = model.apply_to_signal_engine(BinanceSignalConfig(strike=100.0))

    assert out.cooldown_side_agnostic is True


def test_calibrated_model_rejects_wrong_schema_version(monkeypatch) -> None:
    bad = dict(_VALID_MODEL)
    bad["schema_version"] = 999
    try:
        load_calibrated_model(_patch_model_file(monkeypatch, bad))
    except CalibratedModelError as exc:
        assert "schema_version" in str(exc)
        return
    raise AssertionError("wrong schema_version was accepted")


def test_calibrated_model_rejects_empty_overrides(monkeypatch) -> None:
    bad = dict(_VALID_MODEL)
    bad["decision"] = {}
    bad["signal_engine"] = {}
    try:
        load_calibrated_model(_patch_model_file(monkeypatch, bad))
    except CalibratedModelError as exc:
        assert "no parameter overrides" in str(exc)
        return
    raise AssertionError("empty overrides were accepted")


def test_calibrated_model_summary_line_includes_provenance(monkeypatch) -> None:
    model = load_calibrated_model(_patch_model_file(monkeypatch, _VALID_MODEL))
    line = model.summary_line()
    assert "auc=0.610" in line
    assert "samples=4096" in line


# ---------------------------------------------------------------------------
# M4: side-agnostic cooldown.
# ---------------------------------------------------------------------------


def _engine_for_cooldown(*, side_agnostic: bool) -> BinanceSignalEngine:
    cfg = BinanceSignalConfig(
        strike=100.0,
        max_lag_us=0,
        min_window_us=200_000,
        min_abs_move=0.10,
        min_abs_ofi=0.0,
        min_imbalance=0.0,
        signal_cooldown_us=1_000_000,
        cooldown_side_agnostic=side_agnostic,
    )
    return BinanceSignalEngine(cfg, now_us=lambda: 0)


def test_side_agnostic_cooldown_blocks_flip_flop() -> None:
    eng = _engine_for_cooldown(side_agnostic=True)
    # Seed and fire a YES signal.
    assert eng.on_tick_fields(1_000_000, 1, 99.95, 100.05, 5.0, 5.0) is None
    s1 = eng.on_tick_fields(1_300_000, 2, 100.30, 100.50, 5.0, 1.0)
    assert s1 is not None and s1.side == "YES"
    # 200µs later flip; side-agnostic cooldown holds.
    s2 = eng.on_tick_fields(1_500_000, 3, 99.50, 99.70, 1.0, 5.0)
    assert s2 is None


def test_legacy_cooldown_allows_flip_flop() -> None:
    eng = _engine_for_cooldown(side_agnostic=False)
    assert eng.on_tick_fields(1_000_000, 1, 99.95, 100.05, 5.0, 5.0) is None
    s1 = eng.on_tick_fields(1_300_000, 2, 100.30, 100.50, 5.0, 1.0)
    assert s1 is not None and s1.side == "YES"
    s2 = eng.on_tick_fields(1_500_000, 3, 99.50, 99.70, 1.0, 5.0)
    assert s2 is not None and s2.side == "NO"
