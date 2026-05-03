import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exit_policy import ExitPolicyConfig, OpenPosition, decide_exit
from runtime_state import QuoteState


def _position(**overrides) -> OpenPosition:
    data = {
        "side": "YES",
        "token_id": "yes",
        "entry_price": Decimal("0.50"),
        "size": Decimal("10"),
        "opened_ns": 1_000_000_000,
    }
    data.update(overrides)
    return OpenPosition(**data)


def _quote(*, bid: str = "0.56", ask: str = "0.57", ts_ns: int = 1_100_000_000) -> QuoteState:
    return QuoteState("yes", Decimal(bid), Decimal(ask), Decimal("0.01"), ts_ns)


def test_take_profit_sells_only_when_fresh_bid_crosses_target() -> None:
    decision = decide_exit(
        _position(),
        _quote(bid="0.56"),
        ExitPolicyConfig(take_profit_bps=1000, stop_loss_bps=1500),
        now_ns=1_100_000_000,
        tte_us=120_000_000,
        sellable_size=Decimal("10"),
    )

    assert decision.action == "SELL"
    assert decision.reason == "take_profit"
    assert decision.limit_price == Decimal("0.56")
    assert decision.size == Decimal("10")
    assert decision.order_type == "FAK"


def test_stop_loss_sells_when_bid_breaks_risk_floor() -> None:
    decision = decide_exit(
        _position(entry_price=Decimal("0.60")),
        _quote(bid="0.50", ask="0.51"),
        ExitPolicyConfig(take_profit_bps=2000, stop_loss_bps=1500),
        now_ns=1_100_000_000,
        tte_us=120_000_000,
        sellable_size=Decimal("8"),
    )

    assert decision.action == "SELL"
    assert decision.reason == "stop_loss"
    assert decision.limit_price == Decimal("0.50")
    assert decision.size == Decimal("8")


def test_expiry_ripcord_sells_near_market_end() -> None:
    decision = decide_exit(
        _position(),
        _quote(bid="0.47", ask="0.48"),
        ExitPolicyConfig(take_profit_bps=2000, stop_loss_bps=3000, force_exit_tte_us=5_000_000),
        now_ns=1_100_000_000,
        tte_us=4_000_000,
        sellable_size=Decimal("10"),
    )

    assert decision.action == "SELL"
    assert decision.reason == "expiry_ripcord"
    assert decision.limit_price == Decimal("0.47")


def test_stale_quote_blocks_sell_even_if_profit_target_would_have_triggered() -> None:
    decision = decide_exit(
        _position(),
        _quote(bid="0.70", ts_ns=1_000_000_000),
        ExitPolicyConfig(take_profit_bps=1000, max_quote_age_us=50_000),
        now_ns=1_200_000_000,
        tte_us=120_000_000,
        sellable_size=Decimal("10"),
    )

    assert decision.action == "HOLD"
    assert decision.reason == "quote_stale"


def test_no_sellable_inventory_blocks_exit_order() -> None:
    decision = decide_exit(
        _position(),
        _quote(bid="0.70"),
        ExitPolicyConfig(take_profit_bps=1000),
        now_ns=1_100_000_000,
        tte_us=120_000_000,
        sellable_size=Decimal("0"),
    )

    assert decision.action == "HOLD"
    assert decision.reason == "no_sellable_inventory"
