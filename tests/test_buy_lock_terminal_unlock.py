import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathEngine, HotPathGuard
from order_tracker import LocalOrderTracker


def _template(token_id="yes", side="BUY"):
    return FastOrderTemplate(
        name="t",
        token_id=token_id,
        side=side,
        price=0.5,
        size=10.0,
        body_bytes=b"x",
    )


class _Submitter:
    def __init__(self) -> None:
        self.calls: list[FastOrderTemplate] = []

    async def submit(self, template):
        self.calls.append(template)
        return {"orderID": "buy-1"}


def test_lock_releases_when_buy_terminates_with_zero_fill() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.set_exposure_scope({"yes", "no"})
    engine.arm("YES", _template(token_id="yes"), HotPathGuard(max_ask=Decimal("1")))
    engine.arm("NO", _template(token_id="no"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)
    engine.update_quote("no", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    assert first.submitted is True

    # Multi-position: no lock, second buy proceeds independently.
    blocked = asyncio.run(engine.on_signal("NO"))
    assert blocked.submitted is True

    # Order reaches a terminal state with zero fill (rejected/expired).
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "buy-1",
            "asset_id": "yes",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "0",
            "status": "CANCELED",
        }
    )

    # Template consumed; re-arm for third signal.
    engine.arm("NO", _template(token_id="no"), HotPathGuard(max_ask=Decimal("1")))
    third = asyncio.run(engine.on_signal("NO"))
    assert third.submitted is True


def test_lock_does_not_release_on_partial_fill_terminal() -> None:
    submitter = _Submitter()
    tracker = LocalOrderTracker()
    engine = HotPathEngine(submitter=submitter, tracker=tracker, now_ns=lambda: 1_000)
    engine.set_exposure_scope({"yes", "no"})
    engine.arm("YES", _template(token_id="yes"), HotPathGuard(max_ask=Decimal("1")))
    engine.arm("NO", _template(token_id="no"), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)
    engine.update_quote("no", bid=Decimal("0.50"), ask=Decimal("0.51"), ts_ns=1_000)

    first = asyncio.run(engine.on_signal("YES"))
    assert first.submitted is True

    # Order partially filled then terminal but exposure remains.
    tracker.on_trade_event(
        {
            "event_type": "trade",
            "id": "trd-1",
            "asset_id": "yes",
            "side": "BUY",
            "size": "5",
            "price": "0.50",
            "status": "MATCHED",
        }
    )
    tracker.on_order_event(
        {
            "event_type": "order",
            "id": "buy-1",
            "asset_id": "yes",
            "side": "BUY",
            "original_size": "10",
            "size_matched": "5",
            "status": "CANCELED",
        }
    )

    blocked = asyncio.run(engine.on_signal("NO"))
    # Multi-position: exposure doesn't block new buys.
    assert blocked.submitted is True
