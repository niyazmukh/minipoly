import os
from datetime import datetime, timezone
from typing import Any

import orjson


_TRUE = {"1", "true", "yes", "y", "on", "t"}
_FALSE = {"0", "false", "no", "n", "off", "f"}


def env_str(name: str, default: str = "", *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return str(value)


def env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else int(default)
    if min_value is not None and value < min_value:
        raise RuntimeError(f"{name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise RuntimeError(f"{name} must be <= {max_value}")
    return value


def env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.getenv(name, "").strip()
    value = float(raw) if raw else float(default)
    if min_value is not None and value < min_value:
        raise RuntimeError(f"{name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise RuntimeError(f"{name} must be <= {max_value}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise RuntimeError(f"{name} must be a boolean (got {raw!r})")


def maybe_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = orjson.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def parse_gamma_iso8601_to_unix(value: str) -> float:
    value = (value or "").strip()
    if not value:
        return 0.0
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
