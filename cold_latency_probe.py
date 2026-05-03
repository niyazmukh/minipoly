from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from decimal import Decimal

from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathEngine, HotPathGuard
from order_tracker import LocalOrderTracker


@dataclass(slots=True)
class ProbeResult:
    buy_ns: int
    sell_ns: int
    gap_ns: int


class _StubSubmitter:
    async def submit(self, template: FastOrderTemplate) -> dict:
        return {"success": True, "orderID": f"stub-{template.side.lower()}"}


def _template(name: str, token_id: str, side: str, size: Decimal) -> FastOrderTemplate:
    return FastOrderTemplate(
        name=name,
        token_id=token_id,
        side=side,
        price=0.50,
        size=float(size),
        body_bytes=b'{"stub":true}',
    )


async def _run_once(size: Decimal) -> ProbeResult:
    tracker = LocalOrderTracker()
    engine = HotPathEngine(submitter=_StubSubmitter(), tracker=tracker, now_ns=time.perf_counter_ns)
    engine.set_exposure_scope({"yes", "no"})
    engine.arm("YES", _template("entry", "yes", "BUY", size), HotPathGuard(max_ask=Decimal("1")))
    engine.update_quote("yes", bid=Decimal("0.49"), ask=Decimal("0.50"), ts_ns=time.perf_counter_ns())

    buy_start = time.perf_counter_ns()
    buy = await engine.on_signal("YES")
    buy_end = time.perf_counter_ns()
    if not buy.submitted:
        raise RuntimeError(f"buy probe blocked: {buy.reason}")

    with redirect_stdout(io.StringIO()):
        tracker.on_trade_event(
            {
                "event_type": "trade",
                "id": f"probe-buy-{buy_end}",
                "asset_id": "yes",
                "side": "BUY",
                "size": str(size),
                "price": "0.50",
                "status": "MATCHED",
            }
        )
    engine.arm("EXIT", _template("exit", "yes", "SELL", size), HotPathGuard(max_ask=Decimal("1"), min_bid=Decimal("0.01")))
    engine.update_quote("yes", bid=Decimal("0.51"), ask=Decimal("0.52"), ts_ns=time.perf_counter_ns())

    sell_start = time.perf_counter_ns()
    sell = await engine.on_signal("EXIT")
    sell_end = time.perf_counter_ns()
    if not sell.submitted:
        raise RuntimeError(f"sell probe blocked: {sell.reason}")

    return ProbeResult(buy_ns=buy_end - buy_start, sell_ns=sell_end - sell_start, gap_ns=sell_start - buy_end)


def _pct(values: list[int], pct: float) -> int:
    if not values:
        return 0
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * pct))))
    return sorted(values)[idx]


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Cold local latency probe for buy and sell hot-path placement.")
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--size", default="1")
    args = parser.parse_args()

    runs = max(1, args.runs)
    size = Decimal(str(args.size))
    results = [await _run_once(size) for _ in range(runs)]

    for label, values in (
        ("buy_submit_path_ns", [r.buy_ns for r in results]),
        ("sell_submit_path_ns", [r.sell_ns for r in results]),
        ("buy_to_sell_probe_gap_ns", [r.gap_ns for r in results]),
    ):
        print(
            f"{label} min={min(values)} p50={int(statistics.median(values))} "
            f"p95={_pct(values, 0.95)} max={max(values)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
