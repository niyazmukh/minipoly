import asyncio
import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import orjson
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

# Official references:
# - https://docs.polymarket.com/api-reference/authentication.md
# - https://docs.polymarket.com/api-reference/trade/post-a-new-order.md
# - https://github.com/Polymarket/py-clob-client
SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"


def _safe_print(line: str) -> None:
    try:
        print(line)
    except Exception:
        pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _decode_secret(secret: str) -> bytes:
    pad = "=" * (-len(secret) % 4)
    return base64.urlsafe_b64decode(secret + pad)


_RESTING_ORDER_TYPES = frozenset({"GTC", "GTD"})


def _resolve_order_type(env_name: str, default: str = "FAK") -> str:
    raw = os.getenv(env_name, "").strip().upper()
    order_type = raw or default
    if order_type in _RESTING_ORDER_TYPES and not _env_bool("MINIMAL_ALLOW_RESTING_ORDERS", False):
        raise RuntimeError(
            f"{env_name}={order_type} is a resting order type. Set "
            f"MINIMAL_ALLOW_RESTING_ORDERS=true intentionally before using "
            f"GTC/GTD; the bot does not implement the heartbeat required to "
            f"keep resting orders alive."
        )
    return order_type


@dataclass(slots=True)
class MinimalOrderConfig:
    host: str
    chain_id: int
    private_key: str
    signature_type: int
    funder: str
    order_type: str
    post_only: bool
    allow_untracked_sell: bool

    @staticmethod
    def from_env() -> "MinimalOrderConfig":
        if not _env_bool("POLY_ALLOW_LIVE_ORDERS", False) and not _env_bool("MINIMAL_DRY_RUN_ORDERS", False):
            raise RuntimeError(
                "Refusing to initialize live order placer. Set POLY_ALLOW_LIVE_ORDERS=true "
                "only when you intentionally want this probe to submit real CLOB orders, "
                "or MINIMAL_DRY_RUN_ORDERS=true for non-transactional smoke tests."
            )
        # POLY_ORDER_TYPE was historically the entry default for the manual
        # probe. The autonomous runtime uses MINIMAL_ENTRY_ORDER_TYPE/
        # MINIMAL_EXIT_ORDER_TYPE explicitly. Default to FAK and refuse
        # GTC/GTD unless MINIMAL_ALLOW_RESTING_ORDERS=true.
        return MinimalOrderConfig(
            host=os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com").strip() or "https://clob.polymarket.com",
            chain_id=_env_int("POLY_CHAIN_ID", 137),
            private_key=_required_env("POLY_PK"),
            signature_type=_env_int("POLY_SIG_TYPE", 0),
            funder=os.getenv("POLY_FUNDER", "").strip(),
            order_type=_resolve_order_type("POLY_ORDER_TYPE", "FAK"),
            post_only=(os.getenv("POLY_POST_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}),
            allow_untracked_sell=_env_bool("POLY_ALLOW_UNTRACKED_SELL", False),
        )

    def require_manual_sell_allowed(self) -> None:
        if not self.allow_untracked_sell:
            raise RuntimeError(
                "Refusing untracked manual SELL. Automated exits must use LocalOrderTracker sellable inventory. "
                "Set POLY_ALLOW_UNTRACKED_SELL=true only for an intentional manual probe."
            )


class MinimalOrderPlacer:
    __slots__ = (
        "_cfg",
        "_clob",
        "_api_key",
        "_api_passphrase",
        "_secret_bytes",
        "_address",
        "_session",
    )

    def __init__(self, cfg: MinimalOrderConfig) -> None:
        self._cfg = cfg
        if cfg.funder:
            self._clob = ClobClient(
                host=cfg.host,
                key=cfg.private_key,
                chain_id=cfg.chain_id,
                signature_type=cfg.signature_type,
                funder=cfg.funder,
            )
        else:
            self._clob = ClobClient(
                host=cfg.host,
                key=cfg.private_key,
                chain_id=cfg.chain_id,
                signature_type=cfg.signature_type,
            )
        self._api_key = ""
        self._api_passphrase = ""
        self._secret_bytes = b""
        self._address = ""
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        creds = await asyncio.to_thread(self._clob.create_or_derive_api_creds)
        self._clob.set_api_creds(creds)
        self._init_auth(creds)
        self._session = aiohttp.ClientSession(
            base_url=self._cfg.host,
            timeout=aiohttp.ClientTimeout(total=_env_float("POLY_HTTP_TIMEOUT_S", 3.0)),
            raise_for_status=False,
            skip_auto_headers={"User-Agent"},
            connector=aiohttp.TCPConnector(
                limit=0,
                ttl_dns_cache=_env_int("POLY_HTTP_DNS_TTL_S", 600),
                keepalive_timeout=_env_float("POLY_HTTP_KEEPALIVE_S", 60.0),
                enable_cleanup_closed=True,
                force_close=False,
            ),
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _init_auth(self, creds: ApiCreds) -> None:
        self._api_key = str(creds.api_key or "")
        self._api_passphrase = str(creds.api_passphrase or "")
        secret = str(creds.api_secret or "")
        if not self._api_key or not self._api_passphrase or not secret:
            raise RuntimeError("Failed to initialize API credentials.")
        self._secret_bytes = _decode_secret(secret)
        self._address = str(self._clob.get_address() or "")
        if not self._address:
            raise RuntimeError("Failed to derive POLY_ADDRESS from POLY_PK.")

    def _l2_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        sig_payload = ts.encode("utf-8") + method.encode("utf-8") + path.encode("utf-8") + body
        digest = hmac.new(self._secret_bytes, sig_payload, hashlib.sha256).digest()
        sig = base64.urlsafe_b64encode(digest).decode("utf-8")
        return {
            "POLY_ADDRESS": self._address,
            "POLY_API_KEY": self._api_key,
            "POLY_PASSPHRASE": self._api_passphrase,
            "POLY_TIMESTAMP": ts,
            "POLY_SIGNATURE": sig,
            "Content-Type": "application/json",
        }

    async def place_buy(self, token_id: str, price: float, size: float) -> dict[str, Any]:
        return await self._place_limit(side=BUY, token_id=token_id, price=price, size=size)

    async def place_sell(self, token_id: str, price: float, size: float) -> dict[str, Any]:
        self._cfg.require_manual_sell_allowed()
        return await self._place_limit(side=SELL, token_id=token_id, price=price, size=size)

    async def _place_limit(self, side: str, token_id: str, price: float, size: float) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Order placer not started. Call start() first.")

        signed = await asyncio.to_thread(
            self._clob.create_order,
            OrderArgs(token_id=token_id, price=float(price), size=float(size), side=side),
        )
        order_payload = signed.dict()

        # SDK behavior: CLOB owner field should be API key.
        body_obj = {
            "order": order_payload,
            "owner": self._api_key,
            "orderType": self._cfg.order_type,
            "postOnly": self._cfg.post_only,
        }
        body = orjson.dumps(body_obj)
        path = "/order"
        headers = self._l2_headers("POST", path, body)

        async with self._session.post(path, data=body, headers=headers) as resp:
            raw = await resp.read()
            try:
                data = orjson.loads(raw)
            except Exception:
                data = {"success": False, "status": resp.status, "raw": raw.decode("utf-8", errors="replace")}
            data["_http_status"] = resp.status
            return data


async def _async_main() -> None:
    load_dotenv(SCRIPT_ENV_FILE, override=True)

    if len(os.sys.argv) != 5:
        _safe_print("usage: python minimal/order_placer.py <buy|sell> <token_id> <price> <size>")
        raise SystemExit(2)

    side_arg = os.sys.argv[1].strip().lower()
    token_id = os.sys.argv[2].strip()
    price = float(os.sys.argv[3])
    size = float(os.sys.argv[4])

    cfg = MinimalOrderConfig.from_env()
    placer = MinimalOrderPlacer(cfg)
    await placer.start()
    try:
        if side_arg == "buy":
            result = await placer.place_buy(token_id=token_id, price=price, size=size)
        elif side_arg == "sell":
            result = await placer.place_sell(token_id=token_id, price=price, size=size)
        else:
            raise RuntimeError("side must be 'buy' or 'sell'")
        _safe_print(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode("utf-8"))
    finally:
        await placer.close()


if __name__ == "__main__":
    try:
        asyncio.run(_async_main())
    except RuntimeError as exc:
        _safe_print(f"config error: {exc}")
        raise SystemExit(2) from exc
