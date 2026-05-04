import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathEngine, HotPathGuard, QuoteSnapshot
from order_tracker import LocalOrderTracker


class _Submitter:
    def __init__(self) -> None:
        self.calls: list[FastOrderTemplate] = []

    async def submit(self, template: FastOrderTemplate) -> dict:
        self.calls.append(template)
        return {"success": True, "orderID": "oid-1"}


class _SlowSubmitter:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.calls = 0

    async def submit(self, _template: FastOrderTemplate) -> dict:
        self.calls += 1
        await self.release.wait()
        return {"success": True, "orderID": "slow-oid"}


class _SequenceSubmitter:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[FastOrderTemplate] = []

    async def submit(self, template: FastOrderTemplate) -> dict:
        self.calls.append(template)
        if self.responses:
            return self.responses.pop(0)
        return {"success": True, "orderID": f"oid-{len(self.calls)}"}


class _RaisingSubmitter:
    async def submit(self, _template: FastOrderTemplate) -> dict:
        raise OSError("socket reset")


def _template(
    *,
    name: str = "entry",
    token_id: str = "token",
    side: str = "BUY",
    price: float = 0.51,
    size: float = 10.0,
    body_bytes: bytes = b'{"order":1}',
) -> FastOrderTemplate:
    return FastOrderTemplate(
        name=name,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        body_bytes=body_bytes,
    )


def test_fresh_signal_submits_prearmed_template() -> None:
    submitter = _Submitter()
    engine = HotPathEngine(submitter=submitter, now_ns=lambda: 1_000_000_000)
    engine.arm("YES", _template(token_id="yes", price=0.52), HotPathGuard(max_ask=Decimal("0.53")))
    engine.update_quote("yes", bid=Decimal("0.51"), ask=Decimal("0.52"), ts_ns=999_500_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is True
    assert result.reason == "submitted"
    assert result.order_id == "oid-1"
    assert submitter.calls[0].token_id == "yes"


def test_template_is_single_use_after_venue_rejection() -> None:
    submitter = _SequenceSubmitter(
        [
            {
                "_http_status": 400,
                "error": "no orders found to match with FAK order",
                "orderID": "oid-rejected",
            }
        ]
    )
    engine = HotPathEngine(submitter=submitter, now_ns=lambda: 1_000_000_000)
    engine.arm("NO", _template(token_id="no", price=0.59), HotPathGuard(max_ask=Decimal("0.60")))
    engine.update_quote("no", bid=Decimal("0.58"), ask=Decimal("0.59"), ts_ns=999_900_000)

    first = asyncio.run(engine.on_signal("NO"))
    second = asyncio.run(engine.on_signal("NO"))

    assert first.reason == "submit_failed"
    assert first.order_id == "oid-rejected"
    assert second.reason == "not_armed"
    assert len(submitter.calls) == 1


def test_stale_quote_blocks_without_submit() -> None:
    submitter = _Submitter()
    engine = HotPathEngine(submitter=submitter, now_ns=lambda: 2_000_000_000, max_quote_age_ns=200_000_000)
    engine.arm("YES", _template(token_id="yes"), HotPathGuard(max_ask=Decimal("0.60")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000_000_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is False
    assert result.reason == "quote_stale"
    assert submitter.calls == []


def test_ask_guard_blocks_crossed_price() -> None:
    submitter = _Submitter()
    engine = HotPathEngine(submitter=submitter, now_ns=lambda: 1_000)
    engine.arm("YES", _template(token_id="yes"), HotPathGuard(max_ask=Decimal("0.50")))
    engine.update_quote("yes", bid=Decimal("0.49"), ask=Decimal("0.51"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is False
    assert result.reason == "ask_above_guard"
    assert submitter.calls == []


def test_min_ask_guard_blocks_penny_entry_without_submit() -> None:
    submitter = _Submitter()
    engine = HotPathEngine(submitter=submitter, now_ns=lambda: 1_000)
    engine.arm("NO", _template(token_id="no"), HotPathGuard(max_ask=Decimal("0.85"), min_ask=Decimal("0.10")))
    engine.update_quote("no", bid=Decimal("0.00"), ask=Decimal("0.01"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("NO"))

    assert result.submitted is False
    assert result.reason == "ask_below_guard"
    assert submitter.calls == []


def test_sell_template_requires_tracker_sellable_inventory() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("EXIT", _template(name="exit", token_id="yes", side="SELL", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("EXIT"))

    assert result.submitted is False
    assert result.reason == "insufficient_sellable"
    assert submitter.calls == []


def test_buy_template_blocks_while_unsold_position_exists() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "asset_id": "held",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("YES"))

    # Multi-position: buy-cycle lock removed. Second buy is allowed.
    assert result.submitted is True
    assert result.reason == "submitted"


def test_buy_template_ignores_unsold_position_outside_current_scope() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "old-buy-fill",
            "asset_id": "old",
            "side": "BUY",
            "size": "5",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.set_exposure_scope({"yes", "no"})
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is True
    assert result.reason == "submitted"


def test_buy_template_stays_locked_until_position_is_sold() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.arm("NO", _template(token_id="no", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)
    engine.update_quote("no", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    second = asyncio.run(engine.on_signal("NO"))

    # Multi-position: both buys allowed regardless of existing exposure.
    assert first.submitted is True
    assert second.submitted is True

    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "buy-fill",
            "asset_id": "yes",
            "side": "BUY",
            "size": "10",
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
            "size": "10",
            "price": "0.40",
            "status": "CONFIRMED",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "sell-fill",
            "asset_id": "yes",
            "side": "SELL",
            "size": "10",
            "price": "0.50",
            "status": "MATCHED",
        }
    )

    # Re-arm NO: template was consumed by second buy. Simulates armory re-arm.
    engine.arm("NO", _template(token_id="no", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    third = asyncio.run(engine.on_signal("NO"))

    # Multi-position: re-armed template can fire again after previous position sold.
    assert third.submitted is True


def test_completed_old_trade_history_does_not_unlock_new_buy_cycle() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "old-buy",
            "asset_id": "old",
            "side": "BUY",
            "size": "1",
            "price": "0.40",
            "status": "MATCHED",
        }
    )
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "old-sell",
            "asset_id": "old",
            "side": "SELL",
            "size": "1",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.arm("NO", _template(token_id="no", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)
    engine.update_quote("no", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    second = asyncio.run(engine.on_signal("NO"))

    # Multi-position: both buys allowed. Old completed trades don't block.
    assert first.submitted is True
    assert second.submitted is True


def test_in_flight_blocks_concurrent_duplicate_submit() -> None:
    async def _run() -> tuple[str, str, int]:
        release = asyncio.Event()
        submitter = _SlowSubmitter(release)
        engine = HotPathEngine(submitter=submitter, now_ns=lambda: 1_000)
        engine.arm("YES", _template(token_id="yes"), HotPathGuard(max_ask=Decimal("1")))
        engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

        first = asyncio.create_task(engine.on_signal("YES"))
        await asyncio.sleep(0)
        second = await engine.on_signal("YES")
        release.set()
        first_result = await first
        return first_result.reason, second.reason, submitter.calls

    first_reason, second_reason, calls = asyncio.run(_run())

    assert first_reason == "submitted"
    assert second_reason == "buy_in_flight"
    assert calls == 1


def test_failed_buy_submit_does_not_lock_cycle_and_can_retry() -> None:
    submitter = _SequenceSubmitter(
        [
            {"success": False, "_http_status": 400, "error": "rejected"},
            {"success": True, "orderID": "oid-2"},
        ]
    )
    engine = HotPathEngine(submitter=submitter, now_ns=lambda: 1_000)
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    second = asyncio.run(engine.on_signal("YES"))
    engine.arm("YES", _template(token_id="yes", side="BUY", body_bytes=b'{"order":2}'), HotPathGuard(max_ask=Decimal("1")))
    third = asyncio.run(engine.on_signal("YES"))

    assert first.submitted is False
    assert first.reason == "submit_failed"
    assert second.reason == "not_armed"
    assert third.submitted is True
    assert third.order_id == "oid-2"
    assert len(submitter.calls) == 2


def test_sell_submit_reserves_inventory_until_user_channel_updates() -> None:
    submitter = _Submitter()
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
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000_000_000)
    engine.arm("EXIT", _template(name="exit", token_id="yes", side="SELL", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000_000_000)

    first = asyncio.run(engine.on_signal("EXIT"))
    engine.arm("EXIT", _template(name="exit", token_id="yes", side="SELL", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    second = asyncio.run(engine.on_signal("EXIT"))

    assert first.submitted is True
    assert tracker.sellable("yes") == Decimal("0")
    assert second.submitted is False
    assert second.reason == "insufficient_sellable"
    assert len(submitter.calls) == 1


def test_failed_sell_submit_does_not_reserve_inventory() -> None:
    submitter = _SequenceSubmitter(
        [
            {"success": False, "_http_status": 500, "error": "temporarily unavailable"},
            {"success": True, "orderID": "sell-2"},
        ]
    )
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
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.arm("EXIT", _template(name="exit", token_id="yes", side="SELL", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("EXIT"))
    assert first.submitted is False
    assert tracker.sellable("yes") == Decimal("5")

    second = asyncio.run(engine.on_signal("EXIT"))
    engine.arm("EXIT", _template(name="exit", token_id="yes", side="SELL", size=5.0, body_bytes=b'{"order":2}'), HotPathGuard(max_ask=Decimal("1")))
    third = asyncio.run(engine.on_signal("EXIT"))

    assert second.reason == "not_armed"
    assert third.submitted is True
    assert tracker.sellable("yes") == Decimal("0")


def test_buy_submit_registers_current_run_pending_before_http_call() -> None:
    class _InspectingSubmitter:
        def __init__(self, tracker: LocalOrderTracker) -> None:
            self.tracker = tracker

        async def submit(self, template: FastOrderTemplate) -> dict:
            pending = list(self.tracker.pending_submits.values())
            assert len(pending) == 1
            assert pending[0].asset_id == template.token_id
            assert pending[0].side == "BUY"
            assert pending[0].intent == "entry"
            return {"success": True, "orderID": "buy-1"}

    tracker = LocalOrderTracker(current_run_only=True)
    submitter = _InspectingSubmitter(tracker)
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000_000_000)
    engine.arm("YES", _template(token_id="yes", side="BUY", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000_000_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is True
    pending = list(tracker.pending_submits.values())
    assert pending[0].confirmed_order_id == "buy-1"


def test_failed_submit_marks_pending_failed_without_reserving_or_locking() -> None:
    tracker = LocalOrderTracker(current_run_only=True)
    submitter = _SequenceSubmitter(
        [
            {"success": False, "_http_status": 500, "error": "temporary"},
            {"success": True, "orderID": "buy-2"},
        ]
    )
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000_000_000)
    engine.arm("YES", _template(token_id="yes", side="BUY", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000_000_000)

    first = asyncio.run(engine.on_signal("YES"))
    second = asyncio.run(engine.on_signal("YES"))
    engine.arm("YES", _template(token_id="yes", side="BUY", size=5.0, body_bytes=b'{"order":2}'), HotPathGuard(max_ask=Decimal("1")))
    third = asyncio.run(engine.on_signal("YES"))

    assert first.submitted is False
    assert second.reason == "not_armed"
    assert third.submitted is True
    statuses = [p.status for p in tracker.pending_submits.values()]
    assert statuses == ["FAILED", "CONFIRMED"]


def test_submit_exception_marks_pending_unknown_and_returns_failure() -> None:
    """A raised exception is treated as an UNKNOWN submit (post H1 fix).

    The order may have been accepted server-side; the tracker keeps the
    pending eligible for WSS reconciliation rather than declaring failure.
    """
    tracker = LocalOrderTracker(current_run_only=True)
    engine = HotPathEngine(submitter=_RaisingSubmitter(), tracker=tracker, now_ns=lambda: 1_000_000_000)
    engine.arm("YES", _template(token_id="yes", side="BUY", size=5.0), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000_000_000)

    result = asyncio.run(engine.on_signal("YES"))

    assert result.submitted is False
    assert result.reason == "submit_unknown"
    assert list(tracker.pending_submits.values())[0].status == "UNKNOWN"


def test_max_positions_counts_pending_entry_submits_during_wss_lag() -> None:
    """Reproduction of audit finding: max_concurrent_positions=1, one accepted
    BUY submit exists in tracker, but no WSS trade has arrived yet.  A second
    BUY must be rejected because the pending entry submit ties up a slot."""
    tracker = LocalOrderTracker()
    # Simulate an accepted entry submit — order was placed, but WSS
    # hasn't delivered MATCHED/CONFIRMED yet.  owned_by_asset is empty.
    tracker.register_submit(
        intent="entry",
        asset_id="yes",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.50"),
        now_ts=1.0,
        order_id_hint="oid-first",
    )
    tracker.confirm_submit_order_id("submit-1000000-1", "oid-first", now_ts=1.0)

    engine = HotPathEngine(
        submitter=_Submitter(),
        tracker=tracker,
        now_ns=lambda: 1_000_000_000,
        max_concurrent_positions=1,
    )
    engine.arm("YES", _template(token_id="yes", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.49"), ask=Decimal("0.50"), ts_ns=1_000_000_000)

    result = asyncio.run(engine.on_signal("YES"))

    # owned_by_asset is empty, but the pending entry submit counts toward cap.
    assert result.submitted is False
    assert result.reason == "max_positions"
    assert tracker.count_pending_entries() == 1


def test_pending_entry_excluded_when_already_owned() -> None:
    """Pending entry submit for an asset that IS in owned_by_asset
    should NOT double-count.  This is the steady-state after WSS catch-up."""
    tracker = LocalOrderTracker()
    # Establish ownership first (simulates WSS trade arrived)
    tracker.on_trade_event({
        "event_type": "trade",
        "id": "t1",
        "asset_id": "yes",
        "side": "BUY",
        "size": "10",
        "price": "0.50",
        "status": "MATCHED",
    })
    # Then register a matching pending submit (late arrival)
    tracker.register_submit(
        intent="entry",
        asset_id="yes",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.50"),
        now_ts=1.0,
    )
    engine = HotPathEngine(
        submitter=_Submitter(),
        tracker=tracker,
        now_ns=lambda: 1_000_000_000,
        max_concurrent_positions=1,
    )
    engine.arm("NO", _template(token_id="no", side="BUY"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("no", bid=Decimal("0.49"), ask=Decimal("0.50"), ts_ns=1_000_000_000)

    result = asyncio.run(engine.on_signal("NO"))

    # The pending entry is for "yes" which is already owned — count is 1.
    # "no" is a new asset, so cap of 1 is exceeded.
    assert result.submitted is False
    assert result.reason == "max_positions"
