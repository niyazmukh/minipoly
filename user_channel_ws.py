import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable

import orjson
import websockets
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

# Official reference:
# https://docs.polymarket.com/api-reference/wss/user
# https://docs.polymarket.com/api-reference/authentication#python
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"
EventCallback = Callable[[dict[str, Any]], object]


def _safe_print(line: str) -> None:
    try:
        print(line)
    except Exception:
        pass


def _to_events(msg: Any) -> list[dict[str, Any]]:
    if isinstance(msg, dict):
        return [msg]
    if isinstance(msg, list):
        return [x for x in msg if isinstance(x, dict)]
    return []


async def _dispatch_user_event(ev: dict[str, Any], on_event: EventCallback | None) -> None:
    if on_event is None:
        et = str(ev.get("event_type") or ev.get("eventType") or ev.get("type") or "").lower()
        _safe_print(f"\n[event_type={et}]")
        _safe_print(orjson.dumps(ev, option=orjson.OPT_INDENT_2).decode("utf-8"))
        return
    result = on_event(ev)
    if asyncio.iscoroutine(result):
        await result


def _emit_status(line: str, *, on_event: EventCallback | None = None) -> None:
    if on_event is None:
        _safe_print(line)


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


async def listen_forever(*, on_event: EventCallback | None = None) -> None:
    load_dotenv(SCRIPT_ENV_FILE, override=True)

    api_key, api_secret, api_passphrase = await _resolve_api_creds()
    ws_url = os.getenv("POLY_WS_USER", WS_USER_URL).strip() or WS_USER_URL

    backoff = 0.25
    while True:
        try:
            async with websockets.connect(
                ws_url,
                # Use protocol-level ping/pong. Do not send app-level heartbeat payloads.
                ping_interval=20,
                ping_timeout=10,
                compression=None,
                open_timeout=5,
                close_timeout=2,
                max_queue=8192,
            ) as ws:
                auth_msg = {
                    "auth": {
                        "apiKey": api_key,
                        "secret": api_secret,
                        "passphrase": api_passphrase,
                    },
                    "type": "user",
                }
                await ws.send(orjson.dumps(auth_msg).decode("utf-8"))
                _emit_status("user channel subscribed", on_event=on_event)

                backoff = 0.25
                while True:
                    raw = await ws.recv()
                    msg = raw
                    if isinstance(raw, bytes):
                        msg = raw.decode("utf-8", errors="ignore")
                    if isinstance(msg, str):
                        msg = msg.strip()
                    if msg in ("PONG", "pong", "PING", "ping", ""):
                        continue
                    try:
                        payload = orjson.loads(msg) if isinstance(msg, (str, bytes)) else msg
                    except orjson.JSONDecodeError:
                        # Ignore control/non-JSON frames without dropping connection.
                        continue
                    for ev in _to_events(payload):
                        et = str(ev.get("event_type") or ev.get("eventType") or ev.get("type") or "").lower()
                        if et in {"order", "trade"}:
                            await _dispatch_user_event(ev, on_event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _safe_print(f"user ws disconnected: {exc!r}; reconnecting in {backoff:.2f}s")
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 1.7)


if __name__ == "__main__":
    try:
        asyncio.run(listen_forever())
    except RuntimeError as exc:
        _safe_print(f"config error: {exc}")
        raise SystemExit(2) from exc
