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


def test_exit_armory_uses_prepared_template_without_signing_inside_arm_exit() -> None:
    engine = _Engine()
    calls = []

    async def _tracked_build_template(**kwargs) -> FastOrderTemplate:
        calls.append(dict(kwargs))
        return await _build_template(**kwargs)

    armory = ExitArmory(
        engine=engine,
        build_template=_tracked_build_template,
        owner="owner",
        max_quote_age_ns=100_000_000,
    )
    decision = ExitDecision(
        "SELL",
        "take_profit",
        side="YES",
        token_id="yes",
        size=Decimal("5"),
        limit_price=Decimal("0.55"),
        bid=Decimal("0.55"),
        ask=Decimal("0.56"),
        order_type="FAK",
    )

    async def _run() -> bool:
        assert armory.prepare_exit(decision, quote_ts_ns=1_000) is True
        await asyncio.sleep(0)
        assert len(calls) == 1
        return await armory.arm_exit(decision, quote_ts_ns=2_000)

    assert asyncio.run(_run()) is True
    assert len(calls) == 1


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

    async def _run() -> bool:
        assert armory.prepare_exit(decision, quote_ts_ns=1_000) is True
        await asyncio.sleep(0)
        return await armory.arm_exit(decision, quote_ts_ns=2_000)

    assert asyncio.run(_run()) is True

    signal, template, guard = engine.armed[0]
    assert signal == "EXIT"
    assert template.side == "SELL"
    assert template.token_id == "yes"
    assert template.price == 0.56
    assert template.size == 7.5
    assert guard.min_bid == Decimal("0.56")
    assert guard.max_ask == Decimal("1")
    assert guard.max_age_ns == 100_000_000
    assert engine.quotes[-1] == ("yes", Decimal("0.56"), Decimal("0.57"), 2_000)


def test_arm_exit_awaits_inflight_prepare_and_returns_true_without_waiting_for_periodic_loop() -> None:
    """arm_exit must arm and return True on the same call that triggers preparation,
    not silently return False and defer to the next periodic loop iteration."""
    engine = _Engine()
    signed = []

    async def _slow_build_template(**kwargs) -> FastOrderTemplate:
        await asyncio.sleep(0)  # yield to let other tasks run before signing completes
        signed.append(dict(kwargs))
        return FastOrderTemplate(
            name=kwargs["name"],
            token_id=kwargs["token_id"],
            side=kwargs["side"],
            price=kwargs["price"],
            size=kwargs["size"],
            body_bytes=b'{"sell":1}',
        )

    armory = ExitArmory(
        engine=engine,
        build_template=_slow_build_template,
        owner="owner",
        max_quote_age_ns=100_000_000,
    )
    decision = ExitDecision(
        "SELL",
        "take_profit",
        side="YES",
        token_id="yes",
        size=Decimal("5"),
        limit_price=Decimal("0.55"),
        bid=Decimal("0.55"),
        ask=Decimal("0.56"),
        order_type="FAK",
    )

    async def _run() -> bool:
        # Simulate fill-driven path: prepare_exit starts signing, then
        # arm_exit is called immediately (before signing finishes).
        armory.prepare_exit(decision, quote_ts_ns=1_000)
        # Do NOT await sleep here — arm_exit must do the waiting itself.
        result = await armory.arm_exit(decision, quote_ts_ns=2_000)
        return result

    armed = asyncio.run(_run())
    assert armed is True, "arm_exit must return True after awaiting the in-flight prepare task"
    assert len(signed) == 1, "template must be signed exactly once"
    assert len(engine.armed) >= 1
