import logging
import time
from decimal import Decimal
from typing import Any

import aiohttp
import orjson

from auth import L2Auth
from config import BotConfig
from utils import _DEC_ZERO, parse_decimal_amount


class CLOBHttpClient:
    __slots__ = ("_cfg", "_auth", "_log", "_session", "_gamma")

    def __init__(self, cfg: BotConfig, auth: L2Auth, logger: logging.Logger) -> None:
        self._cfg = cfg
        self._auth = auth
        self._log = logger
        self._session = aiohttp.ClientSession(
            base_url=cfg.clob_host,
            connector=aiohttp.TCPConnector(
                limit=0,
                ttl_dns_cache=cfg.http_dns_ttl_s,
                keepalive_timeout=cfg.http_keepalive_timeout_s,
                enable_cleanup_closed=True,
                force_close=False,
            ),
            timeout=aiohttp.ClientTimeout(
                total=cfg.clob_http_timeout_total_s,
                connect=cfg.http_connect_timeout_s,
                sock_connect=cfg.http_sock_connect_timeout_s,
            ),
            raise_for_status=False,
            skip_auto_headers={"User-Agent"},
        )
        self._gamma = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=cfg.gamma_http_timeout_total_s),
            raise_for_status=False,
        )

    async def close(self) -> None:
        await self._session.close()
        await self._gamma.close()

    async def get_clob_time(self) -> int:
        async with self._session.get("/time") as resp:
            raw = await resp.text()
            try:
                ts = int(raw.strip())
                return ts // 1000 if ts > 10_000_000_000 else ts
            except Exception:
                return int(time.time())

    async def gamma_get_event_by_slug(self, slug: str) -> dict[str, Any]:
        async with self._gamma.get(f"https://gamma-api.polymarket.com/events/slug/{slug}") as resp:
            if resp.status != 200:
                return {}
            try:
                data = await resp.json()
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    async def dataapi_positions_abs_sum(self, user_address: str) -> tuple[Decimal, int]:
        if not user_address:
            return _DEC_ZERO, 0
        url = "https://data-api.polymarket.com/positions"
        try:
            async with self._gamma.get(
                url,
                params={
                    "user": user_address,
                    "sizeThreshold": "0",
                    "limit": "500",
                },
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Data API positions check failed with HTTP {resp.status}")
                payload = await resp.read()
        except Exception:
            raise

        total_abs = _DEC_ZERO
        non_zero = 0
        try:
            data = orjson.loads(payload)
        except orjson.JSONDecodeError as exc:
            raise RuntimeError("Data API positions check returned invalid JSON") from exc
        if not isinstance(data, list):
            raise RuntimeError("Data API positions check returned a non-list payload")
        for row in data:
            if not isinstance(row, dict):
                continue
            value = parse_decimal_amount(row.get("size"))
            if value == 0:
                continue
            total_abs += abs(value)
            non_zero += 1
        return total_abs, non_zero

    async def clob_open_order_count(self) -> int:
        path = "/data/orders"
        headers = self._auth.headers("GET", path, b"")
        async with self._session.get(path, headers=headers) as resp:
            raw = await resp.read()
            if resp.status != 200:
                raise RuntimeError(f"CLOB open-order check failed with HTTP {resp.status}")
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError as exc:
            raise RuntimeError("CLOB open-order check returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("CLOB open-order check returned a non-object payload")
        data = payload.get("data")
        data_count = len(data) if isinstance(data, list) else 0
        try:
            count = int(payload.get("count", data_count))
        except (TypeError, ValueError):
            count = data_count
        return max(count, data_count)
