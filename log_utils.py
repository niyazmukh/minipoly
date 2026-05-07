from __future__ import annotations

import logging
import os
import shlex
import sys
import time
from typing import Any


class EpochUsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts_us = int(record.created * 1_000_000)
        message = record.getMessage()
        if record.exc_info:
            message = message + "\n" + self.formatException(record.exc_info)
        return f"ts_us={ts_us} level={record.levelname} logger={record.name} {message}"


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(EpochUsFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(str(level or "WARNING").upper())


def log_level(default: str = "WARNING") -> str:
    return os.getenv("MINIMAL_LOG_LEVEL", default).strip() or default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def full_trace_enabled() -> bool:
    return env_flag("MINIMAL_FULL_TRACE_LOG", False)


def _field_text(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return shlex.quote(str(value))


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    parts = [str(event)]
    for key, value in fields.items():
        parts.append(f"{key}={_field_text(value)}")
    logger.log(level, "%s", " ".join(parts))


def _result_value(result: Any, key: str, default: Any = "") -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _response_value(response: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = response.get(key)
        if value not in (None, ""):
            return value
    return ""


def log_hot_path_result(
    logger: logging.Logger,
    event: str,
    *,
    side: str,
    token_id: str,
    result: Any,
) -> None:
    response = _result_value(result, "response", None)
    if not isinstance(response, dict):
        response = {}
    order_id = _result_value(result, "order_id", "") or _response_value(
        response, "_order_id", "orderID", "order_id"
    )
    submitted = bool(_result_value(result, "submitted", False))
    reason = str(_result_value(result, "reason", "") or "")
    error = _response_value(response, "error", "errorMsg")
    error_text = str(error or "").lower()
    # Keep warning-level output for events that change live state or indicate
    # a real visibility/risk issue. Harmless local gates and expected FAK
    # rejections stay at INFO so WARNING runs do not drown in control flow.
    level = logging.INFO
    if submitted or reason in {"submit_unknown", "not_armed", "quote_stale"}:
        level = logging.WARNING
    elif "transport" in error_text or "duplicated" in error_text:
        level = logging.WARNING

    log_event(
        logger,
        level,
        event,
        side=side,
        token_id=token_id,
        submitted=submitted,
        reason=reason,
        order_id=order_id,
        latency_ns=_result_value(result, "latency_ns", 0),
        http_status=response.get("_http_status", ""),
        response_status=response.get("status", ""),
        taking_amount=response.get("takingAmount", ""),
        making_amount=response.get("makingAmount", ""),
        error=error,
    )


def print_log(line: str) -> None:
    try:
        print(f"ts_us={time.time_ns() // 1000} {line}", flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((f"ts_us={time.time_ns() // 1000} {line}\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass
