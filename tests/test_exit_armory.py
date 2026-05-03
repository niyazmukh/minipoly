import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exit_armory import ExitArmory
from exit_policy import ExitDecision
from fast_order_submitter import FastOrderTemplate


class _Engine:
    def __init__(self) -> None:
        self.armed: list[tuple[str, FastOrderTemplate, object]] = []
        self.quotes: list[tuple[str, Decimal, Decimal, int]] = []

    def arm(self, signal: str, template: FastOrderTemplate, guard: object) -> None:
        self.armed.append((signal, template, guard))

    def update_quote(self, token_id: str, *, bid: Decimal, ask: Decimal, ts_ns: int) -> None:
        self.quotes.append((token_id, bid, ask, ts_ns))


async def _build_template(**kwargs) -> FastOrderTemplate:
    return FastOrderTemplate(
        name=kwargs["name"],
        token_id=kwargs["token_id"],
        side=kwargs["side"],
        price=kwargs["price"],
        size=kwargs["size"],
        body_bytes=b'{"sell":1}',
    )


def test_exit_armory_arms_sell_template_with_bid_guard_and_fresh_quote() -> None:
    engine = _Engine()
    armory = ExitArmory(
        engine=engine,
        build_template=_build_template,
        owner="owner",
        max_quote_age_ns=100_000_000,
    )
    decision = ExitDecision(
        action="SELL",
        reason="take_profit",
        side="YES",
        token_id="yes",
        size=Decimal("7.5"),
        limit_price=Decimal("0.56"),
        bid=Decimal("0.56"),
        ask=Decimal("0.57"),
        order_type="FAK",
        signal="EXIT",
    )

    assert asyncio.run(armory.arm_exit(decision, quote_ts_ns=2_000)) is True

    signal, template, guard = engine.armed[0]
    assert signal == "EXIT"
    assert template.side == "SELL"
    assert template.token_id == "yes"
    assert template.price == 0.56
    assert template.size == 7.5
    assert guard.min_bid == Decimal("0.56")
    assert guard.max_ask == Decimal("1")
    assert guard.max_age_ns == 100_000_000
    assert engine.quotes == [("yes", Decimal("0.56"), Decimal("0.57"), 2_000)]
