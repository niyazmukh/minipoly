import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binance_signal_engine import BinanceSignal
from signal_decision import MarketSignalContract, SignalDecisionConfig, decide_buy


def _signal(side: str = "YES", *, strength: float = 5.0, event_time_us: int = 1_000_000) -> BinanceSignal:
    return BinanceSignal(
        side=side,
        reason="microprice_momentum",
        event_time_us=event_time_us,
        update_id=1,
        microprice=100.50,
        move=0.50 if side == "YES" else -0.50,
        ofi=3.0 if side == "YES" else -3.0,
        imbalance=0.30 if side == "YES" else -0.30,
        spread=0.10,
        strength=strength,
    )


def test_contract_rejects_ambiguous_outcome_labels() -> None:
    contract = MarketSignalContract(
        yes_token_id="a",
        no_token_id="b",
        yes_label="Moon",
        no_label="Crash",
    )

    assert contract.is_valid is False
    assert contract.token_for_signal("YES") == ""


def test_contract_maps_yes_to_up_and_no_to_down() -> None:
    contract = MarketSignalContract(
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_label="Up",
        no_label="Down",
    )

    assert contract.is_valid is True
    assert contract.token_for_signal("YES") == "yes-token"
    assert contract.token_for_signal("NO") == "no-token"


def test_decision_refuses_buy_when_contract_is_invalid() -> None:
    decision = decide_buy(
        _signal("YES"),
        MarketSignalContract("yes", "no", "Up", "Up"),
        SignalDecisionConfig(max_ask=0.60),
        ask=0.40,
        quote_age_us=1_000,
        tte_us=60_000_000,
    )

    assert decision.action == "NO_BUY"
    assert decision.reason == "invalid_contract"


def test_decision_buys_only_when_signal_quote_time_and_edge_agree() -> None:
    decision = decide_buy(
        _signal("YES", strength=4.0),
        MarketSignalContract("yes-token", "no-token", "Up", "Down"),
        SignalDecisionConfig(max_ask=0.60, min_strength=3.0, min_edge=0.05),
        ask=0.50,
        quote_age_us=10_000,
        tte_us=60_000_000,
    )

    assert decision.action == "BUY"
    assert decision.side == "YES"
    assert decision.token_id == "yes-token"
    assert decision.edge > 0.05


def test_decision_blocks_stale_quote_expensive_ask_and_weak_signal() -> None:
    contract = MarketSignalContract("yes-token", "no-token", "Up", "Down")
    cfg = SignalDecisionConfig(max_ask=0.60, max_quote_age_us=100_000, min_strength=3.0)

    assert decide_buy(_signal("YES"), contract, cfg, ask=0.50, quote_age_us=200_000, tte_us=60_000_000).reason == "quote_stale"
    assert decide_buy(_signal("YES"), contract, cfg, ask=0.70, quote_age_us=1_000, tte_us=60_000_000).reason == "ask_above_limit"
    assert decide_buy(_signal("YES", strength=1.0), contract, cfg, ask=0.50, quote_age_us=1_000, tte_us=60_000_000).reason == "weak_signal"
