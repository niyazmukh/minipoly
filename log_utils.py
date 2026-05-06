from __future__ import annotations

import logging
import os
import sys
import time


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


def print_log(line: str) -> None:
    try:
        print(f"ts_us={time.time_ns() // 1000} {line}", flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((f"ts_us={time.time_ns() // 1000} {line}\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass
