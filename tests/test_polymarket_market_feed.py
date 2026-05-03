import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_market_feed import apply_market_event
from runtime_state import MinimalMarket, MinimalRuntimeState


class _Armory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Decimal, Decimal, Decimal]] = []

    async def on_quote(self, *, signal: str, token_id: str, bid: Decimal, ask: Decimal, tick: Decimal) -> bool:
        self.calls.append((signal, token_id, bid, ask, tick))
        return True


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
            start_ts=0,
            end_ts=300,
        )
    )
    return state


def test_apply_best_bid_ask_updates_quote_and_arms_side() -> None:
    state = _state()
    armory = _Armory()

    changed = asyncio.run(
        apply_market_event(
            {
                "event_type": "best_bid_ask",
                "asset_id": "yes",
                "best_bid": "0.49",
                "best_ask": "0.51",
            },
            state,
            armory,
        )
    )

    assert changed is True
    assert state.quote_for_side("YES").ask == Decimal("0.51")  # type: ignore[union-attr]
    assert armory.calls == [("YES", "yes", Decimal("0.49"), Decimal("0.51"), Decimal("0.01"))]


def test_apply_price_changes_handles_multiple_assets_and_tick_size() -> None:
    state = _state()
    armory = _Armory()

    changed = asyncio.run(
        apply_market_event(
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": "yes", "best_bid": "0.50", "best_ask": "0.52", "tick_size": "0.001"},
                    {"asset_id": "no", "bid": "0.47", "ask": "0.49", "tick_size": "0.01"},
                ],
            },
            state,
            armory,
        )
    )

    assert changed is True
    assert state.quote_for_side("YES").tick == Decimal("0.001")  # type: ignore[union-attr]
    assert state.quote_for_side("NO").ask == Decimal("0.49")  # type: ignore[union-attr]
    assert [call[0] for call in armory.calls] == ["YES", "NO"]


def test_apply_market_event_ignores_unknown_assets() -> None:
    state = _state()
    armory = _Armory()

    changed = asyncio.run(
        apply_market_event(
            {"event_type": "best_bid_ask", "asset_id": "other", "best_bid": "1", "best_ask": "1"},
            state,
            armory,
        )
    )

    assert changed is False
    assert armory.calls == []


def test_market_resolved_event_deactivates_market_and_blocks_later_quotes() -> None:
    state = _state()
    armory = _Armory()
    state.update_quote("yes", bid=Decimal("0.49"), ask=Decimal("0.51"))

    resolved = asyncio.run(
        apply_market_event(
            {"event_type": "market_resolved", "market": "c"},
            state,
            armory,
        )
    )
    late_quote = asyncio.run(
        apply_market_event(
            {"event_type": "best_bid_ask", "asset_id": "yes", "best_bid": "0.60", "best_ask": "0.61"},
            state,
            armory,
        )
    )

    assert resolved is True
    assert late_quote is False
    assert state.trading_active is False
    assert state.quote_for_side("YES") is None
    assert armory.calls == []
