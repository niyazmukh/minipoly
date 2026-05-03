import asyncio
import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp
import orjson
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

try:
    from py_clob_client_v2.clob_types import OrderArgs as OrderArgsV2
    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG as V2_ROUNDING_CONFIG
    from py_clob_client_v2.order_utils.model.order_data_v2 import order_to_json_v2
except ImportError:  # pragma: no cover - exercised only when optional v2 SDK is absent.
    OrderArgsV2 = None
    V2_ROUNDING_CONFIG = None
    order_to_json_v2 = None


ORDER_PATH = "/order"
CANCEL_ORDERS_PATH = "/orders"

# Tight per-request timeout for FAK order submission. Beats the session-wide
# default which is friendlier to long-poll endpoints. Values target the
# observed Polymarket /order p95 of ~850ms so a stuck socket fails fast.
_ORDER_POST_TIMEOUT = aiohttp.ClientTimeout(
    total=1.0,
    sock_connect=0.3,
    sock_read=1.0,
)


def _decode_secret(secret: str) -> bytes:
    pad = "=" * (-len(secret) % 4)
    return base64.urlsafe_b64decode(secret + pad)


def build_order_body(
    signed_order: Any,
    *,
    owner: str,
    order_type: str,
    post_only: bool,
) -> bytes:
    if _is_v2_signed_order(signed_order):
        if order_to_json_v2 is None:
            raise RuntimeError("py_clob_client_v2 is required to serialize V2 signed orders")
        return orjson.dumps(order_to_json_v2(signed_order, owner, order_type, bool(post_only), False))
    return orjson.dumps(
        {
            "order": signed_order,
            "owner": owner,
            "orderType": order_type,
            "postOnly": bool(post_only),
        }
    )


def _is_v2_signed_order(signed_order: Any) -> bool:
    return hasattr(signed_order, "timestamp") and hasattr(signed_order, "builder")


def _patch_v2_rounding_for_venue() -> None:
    if V2_ROUNDING_CONFIG is None:
        return
    for round_config in V2_ROUNDING_CONFIG.values():
        if getattr(round_config, "amount", 0) > 2:
            round_config.amount = 2


def extract_order_id(obj: Any) -> str:
    # Fast path: Polymarket /order responses surface orderID at the top level
    # on success. Avoid recursion in the steady-state submit path.
    if isinstance(obj, dict):
        for key in ("orderID", "orderId", "order_id"):
            val = obj.get(key)
            if val:
                return str(val)
        nested = obj.get("order")
        if isinstance(nested, dict):
            val = nested.get("id")
            if val:
                return str(val)
        # Slow path fallback only for non-canonical envelopes.
        return _extract_order_id_deep(obj)
    if isinstance(obj, list):
        return _extract_order_id_deep(obj)
    return ""


def _extract_order_id_deep(obj: Any) -> str:
    if isinstance(obj, dict):
        for key in ("data", "result", "response", "payload"):
            oid = _extract_order_id_deep(obj.get(key))
            if oid:
                return oid
        for key in ("orderID", "orderId", "order_id"):
            val = obj.get(key)
            if val:
                return str(val)
    if isinstance(obj, list):
        for item in obj:
            oid = _extract_order_id_deep(item)
            if oid:
                return oid
    return ""


@dataclass(frozen=True, slots=True)
class FastOrderTemplate:
    name: str
    token_id: str
    side: str
    price: float
    size: float
    body_bytes: bytes

    def __post_init__(self) -> None:
        if not self.body_bytes:
            raise ValueError("body_bytes is required")


class HeaderSigner:
    __slots__ = (
        "_address",
        "_api_key",
        "_api_passphrase",
        "_secret",
        "_now_s",
        "_last_key",
        "_last_headers",
    )

    def __init__(
        self,
        *,
        address: str,
        api_key: str,
        api_passphrase: str,
        api_secret: str,
        now_s: Callable[[], float] = time.time,
    ) -> None:
        if not address or not api_key or not api_passphrase or not api_secret:
            raise RuntimeError("address, api_key, api_passphrase, and api_secret are required")
        self._address = address
        self._api_key = api_key
        self._api_passphrase = api_passphrase
        self._secret = _decode_secret(api_secret)
        self._now_s = now_s
        self._last_key: tuple[str, str, bytes, int] | None = None
        self._last_headers: dict[str, str] | None = None

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        ts_int = int(self._now_s())
        key = (method, path, body, ts_int)
        if key == self._last_key and self._last_headers is not None:
            # aiohttp accepts the dict by-iter; callers must treat the result
            # as read-only. Returning the cached reference avoids a per-submit
            # dict copy in the hot path.
            return self._last_headers

        ts = str(ts_int)
        sig_payload = ts.encode("utf-8") + method.encode("utf-8") + path.encode("utf-8") + body
        digest = hmac.new(self._secret, sig_payload, hashlib.sha256).digest()
        sig = base64.urlsafe_b64encode(digest).decode("utf-8")
        headers = {
            "POLY_ADDRESS": self._address,
            "POLY_API_KEY": self._api_key,
            "POLY_PASSPHRASE": self._api_passphrase,
            "POLY_TIMESTAMP": ts,
            "POLY_SIGNATURE": sig,
            "Content-Type": "application/json",
        }
        self._last_key = key
        self._last_headers = headers
        return headers


async def prepare_template(
    clob: ClobClient,
    *,
    name: str,
    token_id: str,
    side: str,
    price: float,
    size: float,
    owner: str,
    order_type: str,
    post_only: bool,
) -> FastOrderTemplate:
    order_args_cls = OrderArgsV2 if OrderArgsV2 is not None and _uses_v2_orders(clob) else OrderArgs
    if order_args_cls is OrderArgsV2:
        _patch_v2_rounding_for_venue()
    signed = await asyncio.to_thread(
        clob.create_order,
        order_args_cls(token_id=token_id, price=float(price), size=float(size), side=side),
    )
    body = build_order_body(
        signed if _is_v2_signed_order(signed) else signed.dict(),
        owner=owner,
        order_type=order_type,
        post_only=post_only,
    )
    return FastOrderTemplate(
        name=name,
        token_id=token_id,
        side=side,
        price=float(price),
        size=float(size),
        body_bytes=body,
    )


def _uses_v2_orders(clob: Any) -> bool:
    return clob.__class__.__module__.split(".", 1)[0] == "py_clob_client_v2"


class FastOrderSubmitter:
    __slots__ = ("_session", "_signer", "_path")

    def __init__(self, session: aiohttp.ClientSession, signer: HeaderSigner, path: str = ORDER_PATH) -> None:
        self._session = session
        self._signer = signer
        self._path = path

    async def submit(self, template: FastOrderTemplate) -> dict[str, Any]:
        body = template.body_bytes
        headers = self._signer.headers("POST", self._path, body)
        try:
            async with self._session.post(
                self._path,
                data=body,
                headers=headers,
                timeout=_ORDER_POST_TIMEOUT,
            ) as resp:
                raw = await resp.read()
                try:
                    data = orjson.loads(raw)
                except Exception:
                    data = {"success": False, "raw": raw.decode("utf-8", errors="replace")}
                data["_http_status"] = resp.status
                oid = extract_order_id(data)
                if oid:
                    data["_order_id"] = oid
                return data
        except Exception as exc:
            return {"success": False, "_http_status": 0, "error": "transport_error", "detail": repr(exc)}

    async def cancel_orders(self, order_ids: list[str]) -> Any:
        if not order_ids:
            return []
        body = orjson.dumps(order_ids)
        headers = self._signer.headers("DELETE", CANCEL_ORDERS_PATH, body)
        try:
            async with self._session.delete(CANCEL_ORDERS_PATH, data=body, headers=headers) as resp:
                raw = await resp.read()
                try:
                    data = orjson.loads(raw)
                except Exception:
                    data = {"success": False, "raw": raw.decode("utf-8", errors="replace")}
                if isinstance(data, dict):
                    data["_http_status"] = resp.status
                return data
        except Exception as exc:
            return {"success": False, "_http_status": 0, "error": "transport_error", "detail": repr(exc)}


class DryRunOrderSubmitter:
    __slots__ = ("_session",)

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def submit(self, template: FastOrderTemplate) -> dict[str, Any]:
        return {
            "success": False,
            "_http_status": 0,
            "error": "dry_run",
            "side": template.side,
            "token_id": template.token_id,
            "price": template.price,
            "size": template.size,
        }

    async def cancel_orders(self, order_ids: list[str]) -> Any:
        return {"success": True, "_http_status": 0, "dry_run": True, "cancelled": list(order_ids)}
