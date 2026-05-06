import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

import orjson
import websockets
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

from log_utils import configure_logging, env_flag, full_trace_enabled, log_level, print_log

# Official reference:
# https://docs.polymarket.com/api-reference/wss/user
# https://docs.polymarket.com/api-reference/authentication#python
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"
EventCallback = Callable[[dict[str, Any]], object]

APP_PING_INTERVAL_S = 10.0
_LOG = logging.getLogger(__name__)


def _redact_api_key(api_key: str) -> str:
    key = str(api_key or "")
    if len(key) <= 10:
        return "<set>" if key else "<missing>"
    return f"{key[:6]}...{key[-4:]}"


def _to_events(msg: Any) -> list[dict[str, Any]]:
    if isinstance(msg, dict):
        return [msg]
    if isinstance(msg, list):
        return [x for x in msg if isinstance(x, dict)]
    return []


async def _dispatch_user_event(ev: dict[str, Any], on_event: EventCallback | None) -> None:
    if on_event is None:
        et = str(ev.get("event_type") or ev.get("eventType") or ev.get("type") or "").lower()
        print_log(f"\n[event_type={et}]")
        print_log(orjson.dumps(ev, option=orjson.OPT_INDENT_2).decode("utf-8"))
        return
    result = on_event(ev)
    if asyncio.iscoroutine(result):
        await result


def _emit_status(line: str, *, on_event: EventCallback | None = None) -> None:
    _LOG.warning("user_ws_status %s", line)
    if on_event is None:
        print_log(line)


async def _app_ping_loop(ws) -> None:
    while True:
        await asyncio.sleep(APP_PING_INTERVAL_S)
        await ws.send("PING")


async def _resolve_api_creds() -> tuple[str, str, str]:
    api_key = os.getenv("POLY_API_KEY", "").strip()
    api_secret = os.getenv("POLY_API_SECRET", "").strip()
    api_passphrase = os.getenv("POLY_API_PASSPHRASE", "").strip()
    if api_key and api_secret and api_passphrase:
        return api_key, api_secret, api_passphrase

    private_key = os.getenv("POLY_PK", "").strip() or os.getenv("PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError(
            "Missing credentials: set POLY_API_KEY/POLY_API_SECRET/POLY_API_PASSPHRASE "
            "or set POLY_PK (or PRIVATE_KEY) to derive API creds."
        )
    # Normalize and validate private key before passing to py_clob_client.
    # Expected: 64 hex chars, optionally prefixed with 0x.
    private_key = private_key.strip().strip('"').strip("'")
    if private_key.lower().startswith("0x"):
        private_key = private_key[2:]
    if len(private_key) != 64 or re.fullmatch(r"[0-9a-fA-F]{64}", private_key) is None:
        raise RuntimeError(
            "POLY_PK/PRIVATE_KEY format is invalid. Expected 64 hex chars "
            "(optionally with 0x prefix), with no spaces or comments."
        )
    private_key = "0x" + private_key

    clob_host = os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com").strip() or "https://clob.polymarket.com"
    chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))
    signature_type = int(os.getenv("POLY_SIG_TYPE", "0"))
    funder = os.getenv("POLY_FUNDER", "").strip()

    if funder:
        client = ClobClient(
            host=clob_host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )
    else:
        client = ClobClient(
            host=clob_host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
        )
    creds = await asyncio.to_thread(client.create_or_derive_api_creds)
    if not creds.api_key or not creds.api_secret or not creds.api_passphrase:
        raise RuntimeError("Failed to derive API credentials from POLY_PK.")
    return str(creds.api_key), str(creds.api_secret), str(creds.api_passphrase)


async def listen_forever(
    *,
    on_event: EventCallback | None = None,
    api_key: str = "",
    api_secret: str = "",
    api_passphrase: str = "",
) -> None:
    load_dotenv(SCRIPT_ENV_FILE, override=True)

    if not (api_key and api_secret and api_passphrase):
        api_key, api_secret, api_passphrase = await _resolve_api_creds()
    ws_url = os.getenv("POLY_WS_USER", WS_USER_URL).strip() or WS_USER_URL
    log_raw = full_trace_enabled() or env_flag("MINIMAL_LOG_EVERY_USER_WS_MESSAGE", False)

    backoff = 0.25
    while True:
        try:
            api_key_label = _redact_api_key(api_key)
            _LOG.warning("user_ws_connecting url=%s api_key=%s", ws_url, api_key_label)

            async with websockets.connect(
                ws_url,
                ping_interval=None,
                compression=None,
                open_timeout=5,
                close_timeout=2,
                max_queue=8192,
            ) as ws:
                _LOG.warning("user_ws_connected url=%s api_key=%s", ws_url, api_key_label)
                auth_msg = {
                    "auth": {
                        "apiKey": api_key,
                        "secret": api_secret,
                        "passphrase": api_passphrase,
                    },
                    "type": "user",
                }
                await ws.send(orjson.dumps(auth_msg).decode("utf-8"))
                _LOG.warning("user_ws_auth_sent type=user api_key=%s", api_key_label)
                _emit_status("user channel subscribed", on_event=on_event)

                backoff = 0.25
                ping_task = asyncio.create_task(_app_ping_loop(ws), name="user-ws-app-ping")
                try:
                    while True:
                        raw = await ws.recv()
                        msg = raw
                        if isinstance(raw, bytes):
                            msg = raw.decode("utf-8", errors="ignore")
                        if isinstance(msg, str):
                            msg = msg.strip()
                        if log_raw:
                            print_log(f"user_ws_raw recv_us={time.time_ns() // 1000} bytes={len(str(msg))} raw={msg}")
                        if msg in ("PONG", "pong", "PING", "ping", ""):
                            continue
                        try:
                            payload = orjson.loads(msg) if isinstance(msg, (str, bytes)) else msg
                        except orjson.JSONDecodeError:
                            continue
                        for ev in _to_events(payload):
                            et = str(ev.get("event_type") or ev.get("eventType") or ev.get("type") or "").lower()
                            if et in {"order", "trade"}:
                                await _dispatch_user_event(ev, on_event)
                            else:
                                _LOG.warning("user_ws_control_payload event_type=%s", et or "<missing>")
                finally:
                    ping_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await ping_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.exception("user_ws_disconnected reconnecting_in=%.2fs", backoff)
            print_log(f"user ws disconnected: {exc!r}; reconnecting in {backoff:.2f}s")
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 1.7)


if __name__ == "__main__":
    configure_logging(log_level("INFO"))
    try:
        asyncio.run(listen_forever())
    except RuntimeError as exc:
        print_log(f"config error: {exc}")
        raise SystemExit(2) from exc
