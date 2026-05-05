import asyncio
import base64
import hashlib
import hmac
import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, InvalidOperation
from typing import Any, Callable

import aiohttp
import orjson
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

try:
    from py_clob_client_v2.clob_types import OrderArgs as OrderArgsV2
    from py_clob_client_v2.order_utils.model.order_data_v2 import order_to_json_v2
except ImportError:  # pragma: no cover - exercised only when optional v2 SDK is absent.
    OrderArgsV2 = None
    order_to_json_v2 = None


ORDER_PATH = "/order"
CANCEL_ORDERS_PATH = "/orders"

# Per-request timeout for FAK order submission. eu-west-1 → Polymarket US
# RTT is ~300-400ms, so 2.0s gives headroom above p95 while still failing
# fast enough for FAK orders on 5-min markets.
_ORDER_POST_TIMEOUT = aiohttp.ClientTimeout(
    total=2.0,
    sock_connect=0.5,
    sock_read=2.0,
)

PRICE_TICK = Decimal("0.01")
SIZE_STEP = Decimal("0.01")
MAKER_AMOUNT_STEP = Decimal("0.01")
TAKER_AMOUNT_STEP = Decimal("0.0001")


def _dec(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def canonical_order_params(*, side: str, price, size, tick: Decimal = PRICE_TICK) -> tuple[Decimal, Decimal]:
    side_u = str(side or "").upper()
    price_d = _dec(price)
    size_d = _dec(size)

    if side_u == "BUY":
        q_price = _ceil_to_step(price_d, tick)
        q_size = _floor_to_step(size_d, TAKER_AMOUNT_STEP)
        q_size = _ceil_buy_size_for_amount_precision(q_price, q_size)
    elif side_u == "SELL":
        q_price = _floor_to_step(price_d, tick)
        q_size = _floor_to_step(size_d, SIZE_STEP)
    else:
        raise ValueError(f"invalid order side: {side!r}")

    if q_price <= 0 or q_size <= 0:
        raise ValueError(f"invalid canonical order params side={side_u} price={q_price} size={q_size}")

    return q_price, q_size


def _assert_step_aligned(value: Decimal, step: Decimal, label: str) -> None:
    aligned = _floor_to_step(value, step)
    if value != aligned:
        raise ValueError(
            f"signed_order_{label}_not_step_aligned value={value} step={step}"
        )


def _ceil_buy_size_for_amount_precision(
    price: Decimal,
    size: Decimal,
    *,
    price_step: Decimal = PRICE_TICK,
    maker_step: Decimal = MAKER_AMOUNT_STEP,
    taker_step: Decimal = TAKER_AMOUNT_STEP,
) -> Decimal:
    """Return the smallest size >= *size* such that price × size is
    aligned to *maker_step*, assuming price is aligned to *price_step*
    and size is aligned to *taker_step*.
    """
    price_units = int(price / price_step)
    taker_scale = int(Decimal("1") / taker_step)
    price_scale = int(Decimal("1") / price_step)
    maker_scale = int(Decimal("1") / maker_step)

    # price * size = (price_units/price_scale) * (taker_units/taker_scale)
    # For this to be aligned to maker_step = 1/maker_scale:
    #   price_units * taker_units * maker_scale  must be divisible by
    #   price_scale * taker_scale
    denominator = (price_scale * taker_scale) // maker_scale
    required_multiple = denominator // math.gcd(price_units, denominator)

    taker_units = int(size * taker_scale)
    remainder = taker_units % required_multiple
    if remainder > 0:
        taker_units += required_multiple - remainder

    return Decimal(taker_units) / Decimal(taker_scale)


# DUPLICATED: also in order_placer.py:56.  Consolidate if either file is refactored.
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
            "deferExec": False,
        }
    )


def _is_v2_signed_order(signed_order: Any) -> bool:
    return hasattr(signed_order, "timestamp") and hasattr(signed_order, "builder")


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
    price: Decimal
    size: Decimal
    body_bytes: bytes
    implied_price: Decimal | None = None

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


def _order_dict_from_body(body: bytes) -> dict[str, object]:
    obj = orjson.loads(body)
    if not isinstance(obj, dict):
        raise ValueError("signed_order_body_not_dict")
    order = obj.get("order")
    if isinstance(order, dict):
        return order
    return obj


def _extract_amounts(order: dict[str, object]) -> tuple[Decimal, Decimal]:
    maker_raw = order.get("makerAmount", order.get("maker_amount"))
    taker_raw = order.get("takerAmount", order.get("taker_amount"))
    maker = _dec(maker_raw)
    taker = _dec(taker_raw)
    if maker <= 0 or taker <= 0:
        raise ValueError(f"signed_order_non_positive_amounts maker={maker} taker={taker}")
    return maker, taker


def _implied_price_from_amounts(*, side: str, maker: Decimal, taker: Decimal) -> Decimal:
    side_u = side.upper()
    if side_u == "SELL":
        return taker / maker
    if side_u == "BUY":
        return maker / taker
    raise ValueError(f"invalid order side: {side!r}")


def _assert_tick_aligned(value: Decimal, tick: Decimal) -> None:
    aligned = _floor_to_step(value, tick)
    if value != aligned:
        raise ValueError(f"signed_order_price_not_tick_aligned implied={value} tick={tick}")


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
    side_u = str(side or "").upper()
    price_d, size_d = canonical_order_params(
        side=side_u,
        price=price,
        size=size,
        tick=PRICE_TICK,
    )

    order_args_cls = OrderArgsV2 if OrderArgsV2 is not None and _uses_v2_orders(clob) else OrderArgs

    signed = await asyncio.to_thread(
        clob.create_order,
        order_args_cls(
            token_id=token_id,
            price=float(price_d),
            size=float(size_d),
            side=side_u,
        ),
    )

    body = build_order_body(
        signed if _is_v2_signed_order(signed) else signed.dict(),
        owner=owner,
        order_type=order_type,
        post_only=post_only,
    )

    order = _order_dict_from_body(body)
    maker, taker = _extract_amounts(order)
    _assert_step_aligned(maker, MAKER_AMOUNT_STEP, "maker_amount")
    _assert_step_aligned(taker, TAKER_AMOUNT_STEP, "taker_amount")
    implied = _implied_price_from_amounts(side=side_u, maker=maker, taker=taker)
    _assert_tick_aligned(implied, PRICE_TICK)
    if implied != price_d:
        raise ValueError(
            f"signed_order_price_mismatch side={side_u} input={price_d} implied={implied}"
        )

    return FastOrderTemplate(
        name=name,
        token_id=token_id,
        side=side_u,
        price=price_d,
        size=size_d,
        body_bytes=body,
        implied_price=implied,
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
