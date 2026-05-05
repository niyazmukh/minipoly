import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

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
    assert builder.calls[0]["size"] == Decimal("18.0000")
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


def test_one_point_zero_one_dollar_entry_size_rejected_when_notional_jump_too_large() -> None:
    """0.67 ask, $1.01 target: ceil-valid size 2.0000 makes $1.34, >$0.01 overrun."""
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

    assert changed is False, "price=0.67, target=$1.01 must reject under default max_notional_overrun=0.01"
    assert builder.calls == []
    assert engine.armed == []


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


@dataclass
class _CanonicalizingBuilder:
    """Returns templates with post-canonical size, not raw armory size."""

    expected_size: Decimal

    def __post_init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> FastOrderTemplate:
        self.calls.append(dict(kwargs))
        return FastOrderTemplate(
            name=str(kwargs["name"]),
            token_id=str(kwargs["token_id"]),
            side=str(kwargs["side"]),
            price=Decimal(str(kwargs["price"])),
            size=self.expected_size,
            body_bytes=b"x",
        )


def test_identical_quote_does_not_rearm_when_builder_returns_canonical_size() -> None:
    """Regression: identical quotes must not rearm just because
    canonical size differs from raw ceil_to_2dp target."""
    async def _run() -> None:
        engine = _Engine()
        # ask=0.48 → buy_limit=0.48 → raw=ceil_to_2dp(10/0.48)=20.84
        # Under default max_notional_overrun=0.01: ceil=20.8750 maker=10.02 > 10.01
        # → floor=20.8125 maker=9.99 chosen
        builder = _CanonicalizingBuilder(expected_size=Decimal("20.8125"))
        armory = TemplateArmory(
            cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), entry_slippage=Decimal("0")),
            engine=engine,
            build_template=builder,
            now_ns=lambda: 1,
        )

        first = await armory.on_quote(
            signal="YES", token_id="yes",
            bid=Decimal("0.47"), ask=Decimal("0.48"), tick=Decimal("0.01"),
        )
        # Drain background sign task.
        for _ in range(5):
            await asyncio.sleep(0)

        second = await armory.on_quote(
            signal="YES", token_id="yes",
            bid=Decimal("0.47"), ask=Decimal("0.48"), tick=Decimal("0.01"),
        )

        assert first is True
        assert second is False, "identical quote must not rearm"
        assert len(builder.calls) == 1
        assert engine.armed[0][1].size == Decimal("20.8125")

    asyncio.run(_run())


@pytest.mark.parametrize(
    "ask,expected_size",
    [
        # Under max_notional_overrun=0.01: 0.48 ceil=20.8750 maker=10.02 > 10.01 → floor=20.8125
        (Decimal("0.48"), Decimal("20.8125")),
        # 0.51 ceil=20.0000 maker=10.20 > 10.01 → floor=19.0000
        (Decimal("0.51"), Decimal("19.0000")),
        # 0.50 floor=ceil=20.0000 maker=10.00
        (Decimal("0.50"), Decimal("20.0000")),
    ],
)
def test_armory_passes_canonical_size_to_builder(ask: Decimal, expected_size: Decimal) -> None:
    """Armory must pass canonical BUY size (not raw ceil_to_2dp) to builder."""
    async def _run() -> None:
        builder = _CanonicalizingBuilder(expected_size=expected_size)
        armory = TemplateArmory(
            cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), entry_slippage=Decimal("0")),
            engine=_Engine(),
            build_template=builder,
            now_ns=lambda: 1,
        )
        await armory.on_quote(
            signal="YES", token_id="yes",
            bid=ask - Decimal("0.01"), ask=ask, tick=Decimal("0.01"),
        )
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(builder.calls) == 1
        assert builder.calls[0]["price"] == ask
        assert builder.calls[0]["size"] == expected_size, (
            f"armory sent raw size {builder.calls[0]['size']!r} instead of canonical {expected_size!r}"
        )

    asyncio.run(_run())


def test_armory_min_size_checked_before_canonicalization() -> None:
    """min_size gate applies to raw ceil_to_2dp, not post-canonical size."""
    engine = _Engine()
    builder = _Builder()
    # For ask=0.50: raw_size=20.00, which is >= 5.0. Should arm.
    # For ask=0.99: raw_size=10.11... ceil=0.11? No: 10/0.99 = 10.10...
    # Actually need: raw_size < 5. 10/0.99=10.10 > 5. OK not useful.
    # Just test that min_size blocks: ask=0.99, usdc=1.01 → raw=ceil(1.01/0.99)=2
    # But min_size=5 → blocked.
    armory = TemplateArmory(
        cfg=ArmoryConfig(usdc_per_trade=Decimal("1.01"), min_size=Decimal("5")),
        engine=engine,
        build_template=builder,
    )
    result = asyncio.run(
        armory.on_quote(
            signal="YES", token_id="yes",
            bid=Decimal("0.50"), ask=Decimal("0.50"), tick=Decimal("0.01"),
        )
    )
    assert result is False
    assert builder.calls == []


def test_armory_rejects_precision_lattice_jump_above_notional_cap() -> None:
    """price=0.67, target=$1.01: ceil=2.0000 maker=$1.34 > $1.02 max → reject."""
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(
            usdc_per_trade=Decimal("1.01"),
            max_notional_overrun=Decimal("0.01"),
        ),
        engine=engine,
        build_template=builder,
    )

    result = asyncio.run(
        armory.on_quote(
            signal="YES", token_id="yes",
            bid=Decimal("0.66"), ask=Decimal("0.67"), tick=Decimal("0.01"),
        )
    )

    assert result is False
    assert builder.calls == []
    assert engine.armed == []


def test_armory_accepts_valid_near_min_when_within_cap() -> None:
    """price=0.51, target=$1.01: ceil=2.0000 maker=$1.02 ≤ $1.02 max → accept."""
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(
            usdc_per_trade=Decimal("1.01"),
            max_notional_overrun=Decimal("0.01"),
        ),
        engine=engine,
        build_template=builder,
    )

    changed = asyncio.run(
        armory.on_quote(
            signal="YES", token_id="yes",
            bid=Decimal("0.50"), ask=Decimal("0.51"), tick=Decimal("0.01"),
        )
    )
    # Drain background sign task.
    import asyncio as _asyncio
    for _ in range(5):
        _asyncio.run(asyncio.sleep(0))

    assert changed is True
    assert len(builder.calls) == 1
    assert builder.calls[0]["price"] == Decimal("0.51")
    assert builder.calls[0]["size"] == Decimal("2.0000")
    assert builder.calls[0]["price"] * builder.calls[0]["size"] == Decimal("1.02")


def test_armory_uses_floor_when_ceil_exceeds_notional_cap() -> None:
    """price=0.48, target=$10: ceil maker=$10.02 > $10.01 → floor=$9.99 chosen."""
    engine = _Engine()
    builder = _Builder()
    armory = TemplateArmory(
        cfg=ArmoryConfig(
            usdc_per_trade=Decimal("10"),
            max_notional_overrun=Decimal("0.01"),
        ),
        engine=engine,
        build_template=builder,
    )

    changed = asyncio.run(
        armory.on_quote(
            signal="YES", token_id="yes",
            bid=Decimal("0.47"), ask=Decimal("0.48"), tick=Decimal("0.01"),
        )
    )

    assert changed is True
    assert builder.calls[0]["price"] == Decimal("0.48")
    assert builder.calls[0]["size"] == Decimal("20.8125")
    assert builder.calls[0]["price"] * builder.calls[0]["size"] == Decimal("9.99")
