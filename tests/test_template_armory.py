import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathGuard
from template_armory import ArmoryConfig, TemplateArmory, ceil_to_2dp, ceil_to_tick, floor_to_2dp


class _Engine:
    def __init__(self) -> None:
        self.armed: list[tuple[str, FastOrderTemplate, HotPathGuard]] = []
        self.quotes: list[tuple[str, Decimal, Decimal, int]] = []

    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        self.armed.append((signal, template, guard))

    def update_quote(self, token_id: str, *, bid: Decimal, ask: Decimal, ts_ns: int | None = None) -> None:
        self.quotes.append((token_id, bid, ask, int(ts_ns or 0)))


class _Builder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> FastOrderTemplate:
        self.calls.append(kwargs)
        return FastOrderTemplate(
            name=kwargs["name"],
            token_id=kwargs["token_id"],
            side=kwargs["side"],
            price=Decimal(str(kwargs["price"])),
            size=Decimal(str(kwargs["size"])),
            body_bytes=f"{kwargs['name']}:{kwargs['price']}:{kwargs['size']}".encode("ascii"),
        )


def test_price_helpers_align_to_tick_and_2dp() -> None:
    assert ceil_to_tick(Decimal("0.521"), Decimal("0.01")) == Decimal("0.53")
    assert ceil_to_tick(Decimal("0.520"), Decimal("0.01")) == Decimal("0.52")
    assert floor_to_2dp(Decimal("19.999")) == Decimal("19.99")
    assert ceil_to_2dp(Decimal("1.49254")) == Decimal("1.50")


def test_quote_update_prepares_and_arms_template() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), entry_slippage=Decimal("0.002")),
        engine=engine,
        build_template=builder,
        now_ns=lambda: 1_000_000,
    )

    changed = asyncio.run(
        armory.on_quote(
            signal="YES",
            token_id="yes-token",
            bid=Decimal("0.50"),
            ask=Decimal("0.521"),
            tick=Decimal("0.01"),
        )
    )

    assert changed is True
    assert builder.calls[0]["price"] == Decimal("0.53")
    assert builder.calls[0]["size"] == Decimal("18.87")
    assert engine.armed[0][0] == "YES"
    assert engine.armed[0][1].token_id == "yes-token"
    assert engine.armed[0][2].max_ask == Decimal("0.521")
    assert engine.quotes == [("yes-token", Decimal("0.50"), Decimal("0.521"), 1_000_000)]


def test_marketable_buy_budget_below_venue_floor_is_rejected() -> None:
    engine = _Engine()
    builder = _Builder()

    try:
        TemplateArmory(
            cfg=ArmoryConfig(usdc_per_trade=Decimal("1.00"), min_buy_limit=Decimal("0.10")),
            engine=engine,
            build_template=builder,
        )
    except RuntimeError as exc:
        assert "MINIMAL_USDC_PER_TRADE" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for sub-floor live buy budget")


def test_one_point_zero_one_dollar_entry_size_rounds_up_to_venue_min_cash() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(usdc_per_trade=Decimal("1.01"), min_buy_limit=Decimal("0.10")),
        engine=engine,
        build_template=builder,
    )

    changed = asyncio.run(
        armory.on_quote(
            signal="YES",
            token_id="yes-token",
            bid=Decimal("0.66"),
            ask=Decimal("0.67"),
            tick=Decimal("0.01"),
        )
    )

    assert changed is True
    assert builder.calls[0]["price"] == Decimal("0.67")
    assert builder.calls[0]["size"] == Decimal("1.51")
    assert builder.calls[0]["price"] * builder.calls[0]["size"] >= Decimal("1.01")


def test_rearm_hysteresis_skips_tiny_price_move() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(
            usdc_per_trade=Decimal("10"),
            entry_slippage=Decimal("0.001"),
            reprice_hysteresis_pct=Decimal("0.01"),
        ),
        engine=engine,
        build_template=builder,
        now_ns=lambda: 1_000_000,
    )

    first = asyncio.run(armory.on_quote(signal="YES", token_id="yes", bid=Decimal("0.50"), ask=Decimal("0.50"), tick=Decimal("0.01")))
    second = asyncio.run(armory.on_quote(signal="YES", token_id="yes", bid=Decimal("0.50"), ask=Decimal("0.501"), tick=Decimal("0.01")))

    assert first is True
    assert second is False
    assert len(builder.calls) == 1


def test_tick_change_forces_rearm_even_same_price() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), entry_slippage=Decimal("0")),
        engine=engine,
        build_template=builder,
        now_ns=lambda: 1_000_000,
    )

    first = asyncio.run(armory.on_quote(signal="YES", token_id="yes", bid=Decimal("0.50"), ask=Decimal("0.52"), tick=Decimal("0.01")))
    second = asyncio.run(armory.on_quote(signal="YES", token_id="yes", bid=Decimal("0.50"), ask=Decimal("0.52"), tick=Decimal("0.001")))

    assert first is True
    assert second is True
    assert len(builder.calls) == 2


def test_invalid_quote_or_size_does_not_arm() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(usdc_per_trade=Decimal("1.01"), min_size=Decimal("5")),
        engine=engine,
        build_template=builder,
    )

    changed = asyncio.run(armory.on_quote(signal="YES", token_id="yes", bid=Decimal("0"), ask=Decimal("0"), tick=Decimal("0.01")))
    too_small = asyncio.run(armory.on_quote(signal="NO", token_id="no", bid=Decimal("0.50"), ask=Decimal("0.80"), tick=Decimal("0.01")))

    assert changed is False
    assert too_small is False
    assert builder.calls == []
    assert engine.armed == []


def test_unfillable_one_dollar_price_does_not_start_signing() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), entry_slippage=Decimal("0")),
        engine=engine,
        build_template=builder,
    )

    changed = asyncio.run(
        armory.on_quote(
            signal="YES",
            token_id="yes",
            bid=Decimal("0.99"),
            ask=Decimal("1.00"),
            tick=Decimal("0.01"),
        )
    )

    assert changed is False
    assert builder.calls == []
    assert engine.armed == []


def test_configured_buy_limit_bounds_skip_out_of_band_quotes() -> None:
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(
            usdc_per_trade=Decimal("10"),
            min_buy_limit=Decimal("0.10"),
            max_buy_limit=Decimal("0.60"),
        ),
        engine=engine,
        build_template=builder,
    )

    low = asyncio.run(
        armory.on_quote(
            signal="YES",
            token_id="yes",
            bid=Decimal("0.01"),
            ask=Decimal("0.05"),
            tick=Decimal("0.01"),
        )
    )
    high = asyncio.run(
        armory.on_quote(
            signal="NO",
            token_id="no",
            bid=Decimal("0.70"),
            ask=Decimal("0.70"),
            tick=Decimal("0.01"),
        )
    )

    assert low is False
    assert high is False
    assert builder.calls == []
    assert engine.armed == []


def test_signing_failure_is_swallowed_so_next_quote_can_retry() -> None:
    async def _run() -> None:
        engine = _Engine()
        calls = 0

        async def _flaky_builder(**kwargs) -> FastOrderTemplate:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient signing failure")
            return FastOrderTemplate(
                name=kwargs["name"],
                token_id=kwargs["token_id"],
                side=kwargs["side"],
                price=Decimal(str(kwargs["price"])),
                size=Decimal(str(kwargs["size"])),
                body_bytes=b"x",
            )

        armory = TemplateArmory(
            cfg=ArmoryConfig(usdc_per_trade=Decimal("10")),
            engine=engine,
            build_template=_flaky_builder,
            now_ns=lambda: 1,
        )

        first = await armory.on_quote(
            signal="YES",
            token_id="yes",
            bid=Decimal("0.49"),
            ask=Decimal("0.50"),
            tick=Decimal("0.01"),
        )
        await asyncio.sleep(0)
        second = await armory.on_quote(
            signal="YES",
            token_id="yes",
            bid=Decimal("0.50"),
            ask=Decimal("0.51"),
            tick=Decimal("0.01"),
        )
        for _ in range(5):
            await asyncio.sleep(0)

        assert first is True
        assert second is True
        assert calls == 2
        assert engine.armed[0][0] == "YES"
        assert engine.armed[0][1].price == Decimal("0.51")

    asyncio.run(_run())
