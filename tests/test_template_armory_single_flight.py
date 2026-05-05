import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathGuard
from template_armory import ArmoryConfig, TemplateArmory


class _Engine:
    def __init__(self) -> None:
        self.armed: list[tuple[str, FastOrderTemplate, HotPathGuard]] = []
        self.quotes: list[tuple[str, Decimal, Decimal, int | None]] = []

    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        self.armed.append((signal, template, guard))

    def update_quote(self, token_id: str, *, bid: Decimal, ask: Decimal, ts_ns: int | None = None) -> None:
        self.quotes.append((token_id, bid, ask, ts_ns))


def _build_factory(latch: asyncio.Event, signed_log: list[str]):
    async def _build(**kwargs) -> FastOrderTemplate:
        signed_log.append(str(kwargs.get("price")))
        await latch.wait()
        return FastOrderTemplate(
            name=str(kwargs.get("name")),
            token_id=str(kwargs.get("token_id")),
            side=str(kwargs.get("side")),
            price=kwargs.get("price"),
            size=kwargs.get("size"),
            body_bytes=b"x",
        )

    return _build


def test_on_quote_does_not_block_on_signing() -> None:
    async def _run() -> None:
        engine = _Engine()
        latch = asyncio.Event()
        signed: list[str] = []
        armory = TemplateArmory(
            cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), reprice_hysteresis_pct=Decimal("0.0")),
            engine=engine,
            build_template=_build_factory(latch, signed),
            now_ns=lambda: 1,
        )

        # Issue many quote updates without releasing the latch.
        for ask_cents in range(50, 60):
            ask = Decimal(ask_cents) / Decimal("100")
            await armory.on_quote(
                signal="YES",
                token_id="yes",
                bid=ask - Decimal("0.01"),
                ask=ask,
                tick=Decimal("0.01"),
            )

        # Engine quote was updated for every event, but no template has been
        # armed yet because signing is blocked on the latch.
        assert len(engine.quotes) == 10
        assert engine.armed == []

        # Release: the single-flight task should pick the latest target.
        latch.set()
        await asyncio.sleep(0)
        # Drain background tasks.
        for _ in range(20):
            await asyncio.sleep(0)

        assert len(engine.armed) >= 1
        last_signal, last_template, _ = engine.armed[-1]
        assert last_signal == "YES"
        # Last armed price should reflect the most recent ask (0.59).
        assert last_template.price == Decimal("0.59")

    asyncio.run(_run())


def test_reset_cancels_inflight_tasks() -> None:
    async def _run() -> None:
        engine = _Engine()
        latch = asyncio.Event()
        signed: list[str] = []
        armory = TemplateArmory(
            cfg=ArmoryConfig(usdc_per_trade=Decimal("10"), reprice_hysteresis_pct=Decimal("0.0")),
            engine=engine,
            build_template=_build_factory(latch, signed),
            now_ns=lambda: 1,
        )

        await armory.on_quote(
            signal="YES",
            token_id="yes",
            bid=Decimal("0.49"),
            ask=Decimal("0.50"),
            tick=Decimal("0.01"),
        )
        armory.reset()
        # The pending task is cancelled, even though signing is still blocked.
        latch.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert engine.armed == []

    asyncio.run(_run())
