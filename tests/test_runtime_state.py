import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exit_policy import OpenPosition
from runtime_state import MinimalMarket, MinimalRuntimeState
from signal_decision import MarketSignalContract


def test_runtime_state_sets_market_contract_and_quotes() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000_000_000)
    market = MinimalMarket(
        slug="btc-updown-1",
        condition_id="c1",
        yes_token_id="yes",
        no_token_id="no",
        yes_label="Up",
        no_label="Down",
        start_ts=10.0,
        end_ts=310.0,
    )

    state.set_market(market)
    state.update_quote("yes", bid=Decimal("0.49"), ask=Decimal("0.51"), tick=Decimal("0.01"))

    quote = state.quote_for_side("YES")
    assert state.contract == MarketSignalContract("yes", "no", "Up", "Down")
    assert quote is not None
    assert quote.ask == Decimal("0.51")
    assert state.quote_age_us("yes", now_ns=1_250_000_000) == 250_000


def test_runtime_state_rejects_unknown_side_and_missing_quote() -> None:
    state = MinimalRuntimeState()
    state.set_market(
        MinimalMarket(
            slug="s",
            condition_id="c",
            yes_token_id="yes",
            no_token_id="no",
            yes_label="Up",
            no_label="Down",
            start_ts=0,
            end_ts=300,
        )
    )

    assert state.token_for_side("MAYBE") == ""
    assert state.quote_for_side("NO") is None


def test_runtime_state_tracks_single_open_position_for_exit_policy() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000)
    position = OpenPosition(
        side="YES",
        token_id="yes",
        entry_price=Decimal("0.50"),
        size=Decimal("10"),
        opened_ns=1_000,
    )

    state.set_position(position)
    assert state.position == position

    state.clear_position()
    assert state.position is None


def test_runtime_state_resolved_market_clears_tradable_state() -> None:
    state = MinimalRuntimeState(now_ns=lambda: 1_000)
    state.set_market(
        MinimalMarket(
            slug="s",
            condition_id="c",
            yes_token_id="yes",
            no_token_id="no",
            yes_label="Up",
            no_label="Down",
            start_ts=0,
            end_ts=300,
        )
    )
    state.update_quote("yes", bid=Decimal("0.49"), ask=Decimal("0.51"))
    state.set_position(
        OpenPosition(
            side="YES",
            token_id="yes",
            entry_price=Decimal("0.50"),
            size=Decimal("3"),
            opened_ns=1_000,
        )
    )

    state.mark_market_inactive("resolved")

    assert state.trading_active is False
    assert state.market_status == "resolved"
    assert state.quotes == {}
    assert state.position is None
