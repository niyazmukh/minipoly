import base64
import hmac
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fast_order_submitter import (
    DryRunOrderSubmitter,
    FastOrderTemplate,
    FastOrderSubmitter,
    HeaderSigner,
    build_order_body,
    extract_order_id,
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
