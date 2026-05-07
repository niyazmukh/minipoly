import asyncio
import base64
import hmac
import hashlib
import sys
from decimal import Decimal, ROUND_CEILING
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
    _buy_size_multiple_for_amount_precision,
    _ceil_buy_size_for_amount_precision,
    _floor_buy_size_for_amount_precision,
    _floor_to_step,
    build_order_body,
    canonical_buy_target_for_notional,
    canonical_order_params,
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
    pytest.importorskip("py_clob_client_v2")
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


def test_dry_run_submitter_never_uses_http_session() -> None:
    class _Session:
        def post(self, *_args, **_kwargs):
            raise AssertionError("dry run must not POST /order")

    import asyncio

    submitter = DryRunOrderSubmitter(_Session())  # type: ignore[arg-type]
    template = FastOrderTemplate(name="entry", token_id="yes", side="BUY", price=0.5, size=1.0, body_bytes=b"{}")

    submitted = asyncio.run(submitter.submit(template))

    assert submitted == {
        "success": False,
        "_http_status": 0,
        "error": "dry_run",
        "side": "BUY",
        "token_id": "yes",
        "price": 0.5,
        "size": 1.0,
    }


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


# ── BUY amount precision ──────────────────────────────────────────────────


def test_prepare_template_uses_market_tick_instead_of_global_cent_tick() -> None:
    """A sub-cent market tick must survive the final signing canonicalization."""

    async def _run() -> None:
        clob = _MockClobClient(maker_amount="10.01", taker_amount="910.0000")
        template = await prepare_template(
            clob,
            name="entry-yes",
            token_id="yes",
            side="BUY",
            price=Decimal("0.011"),
            size=Decimal("910.0000"),
            tick=Decimal("0.001"),
            owner="owner",
            order_type="FAK",
            post_only=False,
        )

        assert template.price == Decimal("0.011")
        assert template.size == Decimal("910.0000")
        assert clob._calls[0]["price"] == 0.011
        assert clob._calls[0]["size"] == Decimal("910.0000")

    asyncio.run(_run())


def test_prepare_template_passes_price_as_float_and_size_as_decimal() -> None:
    async def _run() -> None:
        clob = _MockClobClient(maker_amount="1.29", taker_amount="0.0129")
        template = await prepare_template(
            clob,
            name="exit-expiry_ripcord",
            token_id="yes",
            side="SELL",
            price=Decimal("0.01"),
            size=Decimal("1.29"),
            owner="owner",
            order_type="FAK",
            post_only=False,
        )

        assert template.price == Decimal("0.01")
        assert clob._calls[0]["price"] == 0.01
        assert clob._calls[0]["size"] == Decimal("1.29")

    asyncio.run(_run())


def test_prepare_template_rejects_buy_body_with_maker_over_2dp() -> None:
    """BUY with maker=10.0032 (>2dp) must be rejected locally."""

    async def _run() -> None:
        # implied price = 10.0032 / 20.84 ≈ 0.48 (tick-aligned)
        # but maker amount has 4dp, violating 2dp constraint
        clob = _MockClobClient(maker_amount="10.0032", taker_amount="20.84")
        with pytest.raises(ValueError, match="signed_order_maker_amount_too_many_decimals"):
            await prepare_template(
                clob,
                name="entry-yes",
                token_id="yes",
                side="BUY",
                price=0.48,
                size=20.84,
                owner="owner",
                order_type="FAK",
                post_only=False,
            )

    asyncio.run(_run())


def test_prepare_template_rejects_buy_body_with_serialized_maker_over_2dp() -> None:
    """BUY maker=1.020000 is numerically cent-aligned but serialized over 2dp."""

    async def _run() -> None:
        clob = _MockClobClient(maker_amount="1.020000", taker_amount="2.5500")
        with pytest.raises(ValueError, match="signed_order_maker_amount_too_many_decimals"):
            await prepare_template(
                clob,
                name="entry-no",
                token_id="no",
                side="BUY",
                price=Decimal("0.40"),
                size=Decimal("2.5500"),
                owner="owner",
                order_type="FAK",
                post_only=False,
            )

    asyncio.run(_run())


def test_prepare_template_rejects_atomic_buy_body_with_scaled_maker_over_2dp() -> None:
    """V2 signed bodies use 1e6 atom strings; 1016000 means 1.016000 USDC."""

    async def _run() -> None:
        clob = _MockClobClient(maker_amount="1016000", taker_amount="1270000")
        with pytest.raises(ValueError, match="signed_order_maker_amount_too_many_decimals"):
            await prepare_template(
                clob,
                name="entry-no",
                token_id="no",
                side="BUY",
                price=Decimal("0.80"),
                size=Decimal("1.27"),
                owner="owner",
                order_type="FAK",
                post_only=False,
            )

    asyncio.run(_run())


@pytest.mark.parametrize(
    "price,maker,taker",
    [
        ("0.48", "9.96", "20.75"),
        ("0.51", "9.69", "19.0000"),
        ("0.50", "10.00", "20.0000"),
    ],
)
def test_valid_buy_bodies_pass(price: str, maker: str, taker: str) -> None:
    async def _run() -> None:
        clob = _MockClobClient(maker_amount=maker, taker_amount=taker)
        template = await prepare_template(
            clob,
            name="entry-yes",
            token_id="yes",
            side="BUY",
            price=float(price),
            size=float(taker),
            owner="owner",
            order_type="FAK",
            post_only=False,
        )
        assert template.price == _floor_to_step(template.price, Decimal("0.01"))
        # maker amount must be step-aligned
        maker_d = Decimal(maker)
        assert _floor_to_step(maker_d, Decimal("0.01")) == maker_d
        # taker amount must be step-aligned
        taker_d = Decimal(taker)
        assert _floor_to_step(taker_d, Decimal("0.0001")) == taker_d

    asyncio.run(_run())


def test_prepare_template_rejects_buy_body_with_taker_over_4dp() -> None:
    """BUY with taker=0.015625 (>4dp) must be rejected locally."""

    async def _run() -> None:
        # maker=0.01 (2dp-aligned), taker=0.015625 (5dp)
        # implied price = 0.01 / 0.015625 = 0.64 (tick-aligned)
        clob = _MockClobClient(maker_amount="0.01", taker_amount="0.015625")
        with pytest.raises(ValueError, match="signed_order_taker_amount_too_many_decimals"):
            await prepare_template(
                clob,
                name="entry-yes",
                token_id="yes",
                side="BUY",
                price=0.64,
                size=0.015625,
                owner="owner",
                order_type="FAK",
                post_only=False,
            )

    asyncio.run(_run())


def test_prepare_template_rejects_buy_body_with_serialized_taker_over_4dp() -> None:
    """BUY taker=2.550000 is numerically 4dp-aligned but serialized over 4dp."""

    async def _run() -> None:
        clob = _MockClobClient(maker_amount="1.02", taker_amount="2.550000")
        with pytest.raises(ValueError, match="signed_order_taker_amount_too_many_decimals"):
            await prepare_template(
                clob,
                name="entry-no",
                token_id="no",
                side="BUY",
                price=Decimal("0.40"),
                size=Decimal("2.5500"),
                owner="owner",
                order_type="FAK",
                post_only=False,
            )

    asyncio.run(_run())


def test_prepare_template_rejects_sell_body_with_serialized_amount_precision_escape() -> None:
    """SELL must use the same serialized signed-body precision gate."""

    async def _run() -> None:
        clob = _MockClobClient(maker_amount="1.570000", taker_amount="1.130400")
        with pytest.raises(ValueError, match="signed_order_maker_amount_too_many_decimals"):
            await prepare_template(
                clob,
                name="exit-take_profit",
                token_id="yes",
                side="SELL",
                price=Decimal("0.72"),
                size=Decimal("1.57"),
                owner="owner",
                order_type="FAK",
                post_only=False,
            )

    asyncio.run(_run())


def test_canonical_buy_size_is_amount_precision_aligned() -> None:
    """Property: for BUY, canonical size*price always has ≤2dp.

    Simulates the real TemplateArmory flow: ceil_to_2dp(usdc_per_trade / price)
    to produce the raw target, then canonical_order_params(...) adjusts for
    amount precision.
    """
    from fast_order_submitter import canonical_order_params

    usdc_per_trade = Decimal("10")
    prices = [Decimal(p) / Decimal("100") for p in range(1, 100)]
    for price_d in prices:
        raw_size = (usdc_per_trade / price_d).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
        try:
            q_price, q_size = canonical_order_params(
                side="BUY", price=price_d, size=raw_size
            )
        except ValueError:
            continue
        maker = q_price * q_size
        assert _floor_to_step(maker, Decimal("0.01")) == maker, (
            f"BUY price={price_d} size={q_size} maker={maker} not 2dp-aligned"
        )
        assert q_size == _floor_to_step(q_size, Decimal("0.01")), (
            f"BUY size={q_size} not SDK limit-order 2dp-aligned"
        )
        assert q_price == _floor_to_step(q_price, Decimal("0.01")), (
            f"BUY price={q_price} not tick-aligned"
        )
        assert maker / q_size == q_price, (
            f"BUY implied price mismatch: maker={maker}/size={q_size} != price={q_price}"
        )
        # Current ceil-to-valid-size policy: canonical size >= target
        assert q_size >= raw_size, (
            f"BUY canonical size={q_size} < target size={raw_size}"
        )


# ── BUY sizing helpers ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "price,expected_multiple",
    [
        (Decimal("0.67"), Decimal("1.0000")),
        (Decimal("0.51"), Decimal("1.0000")),
        (Decimal("0.48"), Decimal("0.25")),
        (Decimal("0.50"), Decimal("0.0200")),
    ],
)
def test_buy_size_multiple_for_amount_precision(price: Decimal, expected_multiple: Decimal) -> None:
    from fast_order_submitter import _buy_size_multiple_for_amount_precision
    result = _buy_size_multiple_for_amount_precision(price)
    assert result == expected_multiple, (
        f"price={price}: expected multiple={expected_multiple}, got {result}"
    )


@pytest.mark.parametrize(
    "price,raw,expected_floor,expected_ceil",
    [
        (Decimal("0.48"), Decimal("20.84"), Decimal("20.75"), Decimal("21.00")),
        (Decimal("0.51"), Decimal("19.61"), Decimal("19.0000"), Decimal("20.0000")),
        (Decimal("0.50"), Decimal("20.00"), Decimal("20.0000"), Decimal("20.0000")),
        (Decimal("0.67"), Decimal("1.51"), Decimal("1.0000"), Decimal("2.0000")),
    ],
)
def test_buy_floor_ceil_for_amount_precision(
    price: Decimal, raw: Decimal, expected_floor: Decimal, expected_ceil: Decimal
) -> None:
    from fast_order_submitter import _floor_buy_size_for_amount_precision, _ceil_buy_size_for_amount_precision
    floor_size = _floor_buy_size_for_amount_precision(price, raw, price_step=Decimal("0.01"))
    ceil_size = _ceil_buy_size_for_amount_precision(price, raw, price_step=Decimal("0.01"))
    assert floor_size == expected_floor, f"price={price} raw={raw}: floor={floor_size}, expected={expected_floor}"
    assert ceil_size == expected_ceil, f"price={price} raw={raw}: ceil={ceil_size}, expected={expected_ceil}"


# ── Notional-aware policy helper ──────────────────────────────────────────


@pytest.mark.parametrize(
    "price,target_usdc,expected_size,expected_maker,expected_policy",
    [
        (Decimal("0.48"), Decimal("10"), Decimal("20.75"), Decimal("9.9600"), "floor"),
        (Decimal("0.51"), Decimal("10"), Decimal("19.0000"), Decimal("9.69"), "floor"),
        (Decimal("0.50"), Decimal("10"), Decimal("20.0000"), Decimal("10.00"), "ceil"),
        (Decimal("0.51"), Decimal("1.01"), Decimal("2.0000"), Decimal("1.02"), "ceil"),
    ],
)
def test_canonical_buy_target_for_notional_chooses_correct_policy(
    price: Decimal, target_usdc: Decimal, expected_size: Decimal, expected_maker: Decimal, expected_policy: str
) -> None:
    from fast_order_submitter import canonical_buy_target_for_notional
    target = canonical_buy_target_for_notional(
        price=price,
        target_usdc=target_usdc,
        tick=Decimal("0.01"),
        min_size=Decimal("0.01"),
        min_maker_amount=Decimal("1.01"),
        max_notional_overrun=Decimal("0.01"),
    )
    assert target.price == price
    assert target.size == expected_size
    assert target.maker_amount == expected_maker
    assert target.policy == expected_policy


def test_canonical_buy_target_for_notional_rejects_when_no_valid_size() -> None:
    """price=0.67, target=$1.01: ceil=$1.34 > max, floor=$0.67 < min → reject."""
    from fast_order_submitter import canonical_buy_target_for_notional
    with pytest.raises(ValueError, match="no_valid_buy_size_within_notional_bounds"):
        canonical_buy_target_for_notional(
            price=Decimal("0.67"),
            target_usdc=Decimal("1.01"),
            tick=Decimal("0.01"),
            min_size=Decimal("0.01"),
            min_maker_amount=Decimal("1.01"),
            max_notional_overrun=Decimal("0.01"),
        )


def test_canonical_buy_target_for_notional_rejects_floor_above_zero_overrun_cap() -> None:
    """Floor is still invalid when a fractional target allows no overrun."""
    from fast_order_submitter import canonical_buy_target_for_notional
    with pytest.raises(ValueError, match="no_valid_buy_size_within_notional_bounds"):
        canonical_buy_target_for_notional(
            price=Decimal("0.11"),
            target_usdc=Decimal("10.009"),
            tick=Decimal("0.01"),
            min_size=Decimal("0.01"),
            min_maker_amount=Decimal("1.01"),
            max_notional_overrun=Decimal("0"),
            max_notional_overrun_bps=0,
        )


def test_canonical_buy_target_for_notional_rejects_below_min_size() -> None:
    """raw_size < min_size must raise before any lattice math."""
    from fast_order_submitter import canonical_buy_target_for_notional
    with pytest.raises(ValueError, match="buy_raw_size_below_min_size"):
        canonical_buy_target_for_notional(
            price=Decimal("0.50"),
            target_usdc=Decimal("0.10"),
            tick=Decimal("0.01"),
            min_size=Decimal("5.00"),
            min_maker_amount=Decimal("1.01"),
            max_notional_overrun=Decimal("0.01"),
        )
