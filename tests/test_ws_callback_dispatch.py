import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_ws import (
    CONTEXT_EVENT_TYPE,
    MarketContext,
    _dispatch_event,
    _emit_context_event,
    _emit_status as _emit_market_status,
)
from user_channel_ws import _dispatch_user_event, _emit_status


def test_market_ws_dispatch_awaits_callback_without_print_path() -> None:
    seen: list[dict] = []

    async def on_event(ev: dict) -> None:
        seen.append(ev)

    event = {"event_type": "best_bid_ask", "asset_id": "yes"}
    asyncio.run(_dispatch_event(event, on_event))

    assert seen == [event]


def test_market_ws_callback_mode_routes_context_to_callback(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr("market_ws._safe_print", printed.append)
    seen: list[dict] = []

    async def on_event(ev: dict) -> None:
        seen.append(ev)

    asyncio.run(
        _emit_context_event(
            "bootstrap",
            MarketContext(
                slug="btc-updown-5m-1",
                condition_id="c",
                asset_ids=["yes", "no"],
                yes_token_id="yes",
                no_token_id="no",
                strike=42000.0,
                end_ts=300.0,
            ),
            on_event,
        )
    )

    assert printed == []
    assert len(seen) == 1
    assert seen[0]["event_type"] == CONTEXT_EVENT_TYPE
    assert seen[0]["strike"] == 42000.0
    assert seen[0]["yes_token_id"] == "yes"


def test_market_ws_callback_mode_suppresses_status_prints(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr("market_ws._safe_print", printed.append)

    _emit_market_status("no active market", on_event=lambda _ev: None)

    assert printed == []


def test_user_ws_dispatch_awaits_callback_without_print_path() -> None:
    seen: list[dict] = []

    async def on_event(ev: dict) -> None:
        seen.append(ev)

    event = {"event_type": "trade", "asset_id": "yes"}
    asyncio.run(_dispatch_user_event(event, on_event))

    assert seen == [event]


def test_user_ws_callback_mode_suppresses_status_prints(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr("user_channel_ws._safe_print", printed.append)

    _emit_status("user channel subscribed", on_event=lambda _ev: None)

    assert printed == []
