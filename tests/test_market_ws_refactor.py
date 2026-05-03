import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_ws import MarketContext, _discover_current_market, _extract_strike, _recv_loop


def test_extract_strike_does_not_use_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_STRIKE", "99999")
    monkeypatch.setenv("MINIMAL_STRIKE", "88888")

    assert _extract_strike({}, {}) == 0.0


def test_discovery_fails_closed_when_strike_is_missing() -> None:
    class _Http:
        async def get_clob_time(self) -> int:
            return 300

        async def gamma_get_event_by_slug(self, _slug: str) -> dict:
            return {
                "active": True,
                "closed": False,
                "markets": [
                    {
                        "active": True,
                        "acceptingOrders": True,
                        "closed": False,
                        "conditionId": "cond",
                        "clobTokenIds": '["yes","no"]',
                        "outcomes": '["Up","Down"]',
                        "endDate": "2999-01-01T00:00:00Z",
                    }
                ],
            }

    cfg = SimpleNamespace(market_window_s=300, market_slug_fmt="btc-updown-5m-{ts}")

    assert asyncio.run(_discover_current_market(_Http(), cfg)) is None


def test_new_market_frame_wakes_reconcile_loop_immediately() -> None:
    class _WS:
        def __init__(self) -> None:
            self.calls = 0

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return orjson.dumps({"event_type": "new_market", "slug": "btc-updown-5m-600"})
            raise asyncio.CancelledError

    async def _run() -> bool:
        event = asyncio.Event()
        state = {
            "current": MarketContext(slug="btc-updown-5m-300", condition_id="c1", asset_ids=["yes", "no"]),
            "subscribed": {"yes", "no"},
            "next_reconcile": 999999.0,
            "reconcile_event": event,
        }
        try:
            await _recv_loop(_WS(), SimpleNamespace(market_slug_fmt="btc-updown-5m-{ts}"), state, None)
        except asyncio.CancelledError:
            return event.is_set()
        return False

    assert asyncio.run(_run()) is True
