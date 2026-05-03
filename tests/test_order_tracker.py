import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_tracker import LocalOrderTracker


def test_tracker_derives_sellable_and_weighted_entry_from_matched_buy_trades() -> None:
    tracker = LocalOrderTracker()

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "t1",
            "asset_id": "yes",
            "side": "BUY",
            "size": "4",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "t2",
            "asset_id": "yes",
            "side": "BUY",
            "size": "6",
            "price": "0.50",
            "status": "MATCHED",
        }
    )

    assert tracker.sellable("yes") == Decimal("10")
    assert tracker.average_entry_price("yes") == Decimal("0.46")


def test_tracker_reduces_cost_basis_when_confirmed_sell_matches() -> None:
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy",
            "asset_id": "yes",
            "side": "BUY",
            "size": "10",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell",
            "asset_id": "yes",
            "side": "SELL",
            "size": "4",
            "price": "0.60",
            "status": "MATCHED",
        }
    )

    assert tracker.owned("yes") == Decimal("6")
    assert tracker.sellable("yes") == Decimal("6")
    assert tracker.average_entry_price("yes") == Decimal("0.50")


def test_local_sell_reservation_is_reduced_by_trade_events() -> None:
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

    tracker.reserve_sell_order("sell-1", "yes", Decimal("5"), now_ts=10.0)
    assert tracker.sellable("yes") == Decimal("0")

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-fill",
            "taker_order_id": "sell-1",
            "asset_id": "yes",
            "side": "SELL",
            "size": "2",
            "price": "0.55",
            "status": "MATCHED",
        }
    )

    assert tracker.reserved("yes") == Decimal("3")
    assert tracker.sellable("yes") == Decimal("0")

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-fill-2",
            "taker_order_id": "sell-1",
            "asset_id": "yes",
            "side": "SELL",
            "size": "3",
            "price": "0.56",
            "status": "MATCHED",
        }
    )

    assert tracker.reserved("yes") == Decimal("0")
    assert tracker.owned("yes") == Decimal("0")


def test_tracker_returns_stale_live_order_ids_without_terminal_orders() -> None:
    tracker = LocalOrderTracker()
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "old-live",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "size_matched": "1",
            "status": "LIVE",
            "timestamp": "100",
        }
    )
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "fresh-live",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "status": "LIVE",
            "timestamp": "119",
        }
    )
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "old-canceled",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "status": "CANCELED",
            "timestamp": "100",
        }
    )

    assert tracker.stale_live_order_ids(now_ts=120.0, max_age_s=10.0) == ["old-live"]


def test_tracker_returns_live_order_ids_without_terminal_or_filled_orders() -> None:
    tracker = LocalOrderTracker()
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "live",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "size_matched": "1",
            "status": "LIVE",
            "timestamp": "100",
        }
    )
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "filled",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "size_matched": "5",
            "status": "LIVE",
            "timestamp": "101",
        }
    )
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "canceled",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "status": "CANCELED",
            "timestamp": "102",
        }
    )

    assert tracker.live_order_ids() == ["live"]


def test_tracker_detects_open_exposure_from_owned_reserved_or_live_orders() -> None:
    tracker = LocalOrderTracker()

    assert tracker.has_open_exposure() is False
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "live-buy",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "status": "LIVE",
            "timestamp": "100",
        }
    )
    assert tracker.has_open_exposure() is True

    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "live-buy",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "size_matched": "5",
            "status": "MATCHED",
            "timestamp": "101",
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
            "status": "MATCHED",
            "timestamp": "102",
        }
    )
    assert tracker.has_open_exposure() is True

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-fill",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "price": "0.50",
            "status": "MATCHED",
            "timestamp": "103",
        }
    )
    assert tracker.has_open_exposure() is False


def test_strict_tracker_ignores_unregistered_old_trade_events() -> None:
    tracker = LocalOrderTracker(current_run_only=True)

    changed = tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "old-buy-fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
            "timestamp": "100",
        }
    )

    assert changed is None
    assert tracker.owned("yes") == Decimal("0")
    assert tracker.sellable("yes") == Decimal("0")


def test_strict_tracker_applies_trade_for_registered_current_run_order() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    submit_id = tracker.register_submit(
        "entry",
        "yes",
        "BUY",
        Decimal("5"),
        Decimal("0.40"),
        now_ts=100.0,
    )
    tracker.confirm_submit_order_id(submit_id, "buy-1", now_ts=100.1)

    changed = tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "taker_order_id": "buy-1",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
            "timestamp": "100.2",
        }
    )

    assert changed is not None
    assert tracker.owned("yes") == Decimal("5")
    assert tracker.sellable("yes") == Decimal("5")


def test_pending_submit_can_match_wss_order_by_exact_current_run_candidate() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    submit_id = tracker.register_submit(
        "exit",
        "yes",
        "SELL",
        Decimal("5"),
        Decimal("0.55"),
        now_ts=100.0,
    )

    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "sell-1",
            "asset_id": "yes",
            "side": "SELL",
            "size": "5",
            "price": "0.55",
            "status": "LIVE",
            "timestamp": "100.5",
        }
    )

    pending = tracker.pending_submit(submit_id)
    assert pending is not None
    assert pending.confirmed_order_id == "sell-1"


def test_pending_submit_ambiguous_wss_match_is_not_bound() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    first = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=100.0)
    second = tracker.register_submit("entry", "yes", "BUY", Decimal("5"), Decimal("0.40"), now_ts=100.0)

    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "buy-ambiguous",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "LIVE",
            "timestamp": "100.5",
        }
    )

    assert tracker.pending_submit(first).confirmed_order_id == ""
    assert tracker.pending_submit(second).confirmed_order_id == ""
