import asyncio
from decimal import Decimal
import sys
from pathlib import Path

import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from http_client import CLOBHttpClient


class _Response:
    def __init__(self, payload, *, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def read(self) -> bytes:
        return orjson.dumps(self._payload)


class _Session:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


class _Auth:
    def headers(self, method: str, path: str, body: bytes):
        assert method == "GET"
        assert path == "/data/orders"
        assert body == b""
        return {"POLY_API_KEY": "redacted"}


def _client(*, gamma_payload=None, clob_payload=None) -> CLOBHttpClient:
    client = object.__new__(CLOBHttpClient)
    client._cfg = None
    client._auth = _Auth()
    client._log = None
    client._gamma = _Session(gamma_payload)
    client._session = _Session(clob_payload)
    return client


def test_dataapi_positions_abs_sum_parses_position_payload() -> None:
    client = _client(
        gamma_payload=[
            {"size": "1.25"},
            {"size": "-0.50"},
            {"size": "0"},
            {"size": None},
        ]
    )

    total, count = asyncio.run(client.dataapi_positions_abs_sum("0xabc"))

    assert total == Decimal("1.75")
    assert count == 2
    assert client._gamma.calls[0][1]["params"]["limit"] == "500"


def test_clob_open_order_count_uses_total_count_when_available() -> None:
    client = _client(clob_payload={"count": 7, "data": [{"id": "a"}, {"id": "b"}]})

    count = asyncio.run(client.clob_open_order_count())

    assert count == 7
    assert client._session.calls[0][0] == "/data/orders"


def test_clob_open_order_count_falls_back_to_data_length() -> None:
    client = _client(clob_payload={"data": [{"id": "a"}, {"id": "b"}]})

    count = asyncio.run(client.clob_open_order_count())

    assert count == 2
