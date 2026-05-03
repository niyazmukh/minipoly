import base64
import hashlib
import hmac
import time
from collections import OrderedDict

from py_clob_client.clob_types import ApiCreds


def _b64_urlsafe_decode_padded(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


class L2Auth:
    __slots__ = ("api_key", "passphrase", "poly_address", "_secret_bytes", "_cache", "_cache_max_entries")

    def __init__(self, creds: ApiCreds, poly_address: str, cache_max_entries: int = 10) -> None:
        self.api_key = creds.api_key
        self.passphrase = creds.api_passphrase
        self.poly_address = poly_address
        self._secret_bytes = _b64_urlsafe_decode_padded(creds.api_secret)
        self._cache_max_entries = max(1, int(cache_max_entries))
        self._cache: OrderedDict[tuple[str, str, bytes], tuple[int, dict[str, str]]] = OrderedDict()

    def headers(self, method: str, path: str, body_bytes: bytes = b"") -> dict[str, str]:
        method = method.upper()
        now = int(time.time())
        key = (method, path, body_bytes)
        cached = self._cache.get(key)
        if cached and cached[0] == now:
            return cached[1]

        ts = str(now)
        payload = ts.encode("utf-8") + method.encode("utf-8") + path.encode("utf-8") + body_bytes
        sig = base64.urlsafe_b64encode(hmac.new(self._secret_bytes, payload, hashlib.sha256).digest()).decode("utf-8")
        headers = {
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.passphrase,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_ADDRESS": self.poly_address,
            "Content-Type": "application/json",
        }
        if key in self._cache:
            self._cache.pop(key, None)
        elif len(self._cache) >= self._cache_max_entries:
            self._cache.popitem(last=False)
        self._cache[key] = (now, headers)
        return headers
