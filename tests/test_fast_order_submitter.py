import asyncio
import base64
import hmac
import hashlib
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_order_submitter import (
    DryRunOrderSubmitter,
    FastOrderTemplate,
    FastOrderSubmitter,
    HeaderSigner,
    PRICE_TICK,
    _assert_tick_aligned,
    _floor_to_step,
    build_order_body,
    extract_order_id,
    prepare_template,
)


def _secret() -> str:
    return base64.urlsafe_b64encode(b"super-secret").decode("ascii")


def test_header_signer_reuses_signature_for_same_second_and_body() -> None:
    signer = HeaderSigner(
        address="0xabc",
        api_key="api-key",
        api_passphrase="pass",
        api_secret=_secret(),
        now_s=lambda: 1234,
    )
    body = b'{"x":1}'

    first = signer.headers("POST", "/order", body)
    second = signer.headers("POST", "/order", body)

    expected_payload = b"1234POST/order" + body
    expected = base64.urlsafe_b64encode(
        hmac.new(b"super-secret", expected_payload, hashlib.sha256).digest()
    ).decode("utf-8")
    assert first == second
    assert first["POLY_SIGNATURE"] == expected
    assert first["POLY_TIMESTAMP"] == "1234"


def test_build_order_body_is_stable_and_compact() -> None:
    body = build_order_body(
        {"tokenId": "token", "side": "BUY", "salt": 7},
        owner="api-key",
        order_type="FAK",
        post_only=False,
    )

    assert body == b'{"order":{"tokenId":"token","side":"BUY","salt":7},"owner":"api-key","orderType":"FAK","postOnly":false,"deferExec":false}'


def test_build_order_body_serializes_v2_signed_order_for_buy_and_sell() -> None:
    from py_clob_client_v2.order_utils.model.order_data_v2 import SignedOrderV2
    from py_clob_client_v2.order_utils.model.side import Side
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

    for side, side_text in ((Side.BUY, "BUY"), (Side.SELL, "SELL")):
        body = build_order_body(
            SignedOrderV2(
                salt="1",
                maker="0x0000000000000000000000000000000000000001",
                signer="0x0000000000000000000000000000000000000002",
                tokenId="123",
                makerAmount="100",
                takerAmount="200",
                side=side,
                signatureType=SignatureTypeV2.EOA,
                timestamp="1770000000000",
                metadata="0x" + "0" * 64,
                builder="0x" + "0" * 64,
                expiration="0",
                signature="0xsig",
            ),
            owner="api-key",
            order_type="FAK",
            post_only=False,
        )

        assert b'"timestamp":"1770000000000"' in body
        assert b'"metadata":"0x' in body
        assert b'"builder":"0x' in body
        assert b'"deferExec":false' in body
        assert f'"side":"{side_text}"'.encode("ascii") in body
        assert b'"feeRateBps"' not in body
        assert b'"nonce"' not in body
        assert b'"taker"' not in body


def test_extract_order_id_accepts_common_clob_response_shapes() -> None:
    assert extract_order_id({"orderID": "a"}) == "a"
    assert extract_order_id({"orderId": "b"}) == "b"
    assert extract_order_id({"order": {"id": "c"}}) == "c"
    assert extract_order_id({"data": {"order_id": "d"}}) == "d"


def test_template_requires_body_bytes() -> None:
    with pytest.raises(ValueError, match="body_bytes"):
        FastOrderTemplate(name="entry", token_id="t", side="BUY", price=0.5, size=1.0, body_bytes=b"")


def test_cancel_orders_uses_delete_orders_with_compact_body() -> None:
    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def read(self) -> bytes:
            return b'[{"canceled":"oid-1"}]'

    class _Session:
        def __init__(self) -> None:
            self.calls = []

        def delete(self, path, *, data, headers):
            self.calls.append((path, data, headers))
            return _Resp()

    session = _Session()
    signer = HeaderSigner(
        address="0xabc",
        api_key="api-key",
        api_passphrase="pass",
        api_secret=_secret(),
        now_s=lambda: 1234,
    )
    submitter = FastOrderSubmitter(session, signer)  # type: ignore[arg-type]

    import asyncio

    result = asyncio.run(submitter.cancel_orders(["oid-1", "oid-2"]))

    assert result == [{"canceled": "oid-1"}]
    assert session.calls[0][0] == "/orders"
    assert session.calls[0][1] == b'["oid-1","oid-2"]'
    assert session.calls[0][2]["POLY_SIGNATURE"]


def test_submit_transport_error_returns_structured_failure() -> None:
    class _Session:
        def post(self, *_args, **_kwargs):
            raise OSError("network down")

    signer = HeaderSigner(
        address="0xabc",
        api_key="api-key",
        api_passphrase="pass",
        api_secret=_secret(),
        now_s=lambda: 1234,
    )
    submitter = FastOrderSubmitter(_Session(), signer)  # type: ignore[arg-type]

    import asyncio

    result = asyncio.run(
        submitter.submit(
            FastOrderTemplate(name="entry", token_id="yes", side="BUY", price=0.5, size=1.0, body_bytes=b"{}")
        )
    )

    assert result["success"] is False
    assert result["_http_status"] == 0
    assert result["error"] == "transport_error"


def test_cancel_transport_error_returns_structured_failure() -> None:
    class _Session:
        def delete(self, *_args, **_kwargs):
            raise OSError("network down")

    signer = HeaderSigner(
        address="0xabc",
        api_key="api-key",
        api_passphrase="pass",
        api_secret=_secret(),
        now_s=lambda: 1234,
    )
    submitter = FastOrderSubmitter(_Session(), signer)  # type: ignore[arg-type]

    import asyncio

    result = asyncio.run(submitter.cancel_orders(["oid-1"]))

    assert result["success"] is False
    assert result["_http_status"] == 0
    assert result["error"] == "transport_error"


def test_dry_run_submitter_never_uses_http_session() -> None:
    class _Session:
        def post(self, *_args, **_kwargs):
            raise AssertionError("dry run must not POST /order")

        def delete(self, *_args, **_kwargs):
            raise AssertionError("dry run must not DELETE /orders")

    import asyncio

    submitter = DryRunOrderSubmitter(_Session())  # type: ignore[arg-type]
    template = FastOrderTemplate(name="entry", token_id="yes", side="BUY", price=0.5, size=1.0, body_bytes=b"{}")

    submitted = asyncio.run(submitter.submit(template))
    cancelled = asyncio.run(submitter.cancel_orders(["oid-1"]))

    assert submitted == {
        "success": False,
        "_http_status": 0,
        "error": "dry_run",
        "side": "BUY",
        "token_id": "yes",
        "price": 0.5,
        "size": 1.0,
    }
    assert cancelled == {"success": True, "_http_status": 0, "dry_run": True, "cancelled": ["oid-1"]}


# ── prepare_template signed-body validation ───────────────────────────────


class _MockClobClient:
    """Returns V1-style signed orders via .dict() with configurable amounts."""

    def __init__(self, maker_amount: str, taker_amount: str) -> None:
        self._maker = maker_amount
        self._taker = taker_amount
        self._calls: list[dict[str, object]] = []

    def create_order(self, order_args: object) -> "_MockSignedOrder":
        self._calls.append(
            {
                "token_id": getattr(order_args, "token_id", ""),
                "price": getattr(order_args, "price", 0),
                "size": getattr(order_args, "size", 0),
                "side": getattr(order_args, "side", ""),
            }
        )
        return _MockSignedOrder(self._maker, self._taker)


class _MockSignedOrder:
    def __init__(self, maker_amount: str, taker_amount: str) -> None:
        self._maker = maker_amount
        self._taker = taker_amount

    def dict(self) -> dict[str, str]:
        return {
            "makerAmount": self._maker,
            "takerAmount": self._taker,
        }


def test_prepare_template_rejects_non_tick_implied_price_locally() -> None:
    """The exact failing combination from live log: maker=1.57, taker=1.13.

    Implied SELL price = 1.13 / 1.57 ≈ 0.719745..., not aligned to 0.01 tick.
    Must raise before HTTP submit.
    """

    async def _run() -> None:
        clob = _MockClobClient(maker_amount="1.57", taker_amount="1.13")
        with pytest.raises(ValueError, match="signed_order_price_not_tick_aligned"):
            await prepare_template(
                clob,
                name="exit-take_profit",
                token_id="yes",
                side="SELL",
                price=0.72,
                size=1.57,
                owner="owner",
                order_type="GTC",
                post_only=False,
            )

    asyncio.run(_run())


@pytest.mark.parametrize(
    "maker,taker,expected_price",
    [
        ("1.57", "1.1304", "0.72"),
        ("1.57", "1.1461", "0.73"),
        ("1.57", "1.1618", "0.74"),
    ],
)
def test_valid_sell_bodies_pass(maker: str, taker: str, expected_price: str) -> None:
    async def _run() -> None:
        clob = _MockClobClient(maker_amount=maker, taker_amount=taker)
        template = await prepare_template(
            clob,
            name="exit-take_profit",
            token_id="yes",
            side="SELL",
            price=float(expected_price),
            size=1.57,
            owner="owner",
            order_type="GTC",
            post_only=False,
        )
        assert template.implied_price == template.price, (
            f"implied={template.implied_price} != price={template.price}"
        )
        assert template.implied_price == _floor_to_step(
            template.implied_price, Decimal("0.01")
        ), f"implied={template.implied_price} not tick-aligned"
        assert template.price == Decimal(expected_price), (
            f"price={template.price} != expected={expected_price}"
        )
        assert template.size == Decimal("1.57")

    asyncio.run(_run())


def test_prepare_template_raises_on_price_mismatch() -> None:
    """Implied price is tick-aligned but differs from canonicalized input."""

    async def _run() -> None:
        # Input price canonicalizes to 0.72. The SDK returns amounts that imply
        # 0.73 (which IS tick-aligned but mismatches the input).
        # maker=1.57, taker=1.1461 → implied = 1.1461 / 1.57 = 0.73
        clob = _MockClobClient(maker_amount="1.57", taker_amount="1.1461")
        with pytest.raises(ValueError, match="signed_order_price_mismatch"):
            await prepare_template(
                clob,
                name="exit-take_profit",
                token_id="yes",
                side="SELL",
                price=0.72,
                size=1.57,
                owner="owner",
                order_type="GTC",
                post_only=False,
            )

    asyncio.run(_run())
