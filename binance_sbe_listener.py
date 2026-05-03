import argparse
import asyncio
import gc
import inspect
import os
import struct
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import websockets
from dotenv import load_dotenv

from binance_signal_engine import BinanceTick

TickFieldsCallback = Callable[[int, int, float, float, float, float], object]

# Official references:
# - https://developers.binance.com/docs/binance-spot-api-docs/sbe-market-data-streams
# - https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/sbe/schemas/stream_1_0.xml
# - https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/sbe/schemas/spot-fixsbe-1_0.xml

SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"
DEFAULT_SCHEMA_URL = (
    "https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/sbe/schemas/stream_1_0.xml"
)
DEFAULT_MESSAGE_NAME = "BestBidAskStreamEvent"

_PRIMITIVE_TO_STRUCT: dict[str, str] = {
    "char": "c",
    "int8": "b",
    "uint8": "B",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
    "float": "f",
    "double": "d",
}

_PRIMITIVE_SIZES: dict[str, int] = {
    "char": 1,
    "int8": 1,
    "uint8": 1,
    "int16": 2,
    "uint16": 2,
    "int32": 4,
    "uint32": 4,
    "int64": 8,
    "uint64": 8,
    "float": 4,
    "double": 8,
}


@dataclass(frozen=True, slots=True)
class CompiledMessage:
    schema_url: str
    schema_id: int
    schema_version: int
    message_name: str
    template_id: int
    header_struct: struct.Struct
    header_index: dict[str, int]
    root_struct: struct.Struct
    root_index: dict[str, int]
    symbol_len_bytes: int


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    ws_url: str
    api_key: str
    schema_url: str
    message_name: str
    decode_symbol: bool
    status_interval_ms: int
    max_queue: int
    open_timeout_s: float
    close_timeout_s: float
    reconnect_min_s: float
    reconnect_max_s: float
    reconnect_factor: float
    disable_gc: bool


def _safe_print(line: str) -> None:
    try:
        print(line)
    except Exception:
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int, min_value: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except Exception:
        return default
    return max(min_value, value)


def _env_float(name: str, default: float, min_value: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except Exception:
        return default
    return max(min_value, value)


def _field_value_count(fmt: str) -> int:
    idx = 0
    while idx < len(fmt) and fmt[idx].isdigit():
        idx += 1
    if idx == 0:
        return 1
    if idx >= len(fmt):
        return 1
    code = fmt[idx:]
    if code in ("s", "p"):
        return 1
    return int(fmt[:idx])


def _local_name(tag: str) -> str:
    pos = tag.rfind("}")
    return tag[pos + 1 :] if pos >= 0 else tag


def _fetch_schema_xml(schema_ref: str) -> tuple[str, str]:
    req = urllib.request.Request(schema_ref, headers={"User-Agent": "poly-buy-sell-minimal-sbe-listener/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = resp.read()
    if payload.lstrip().startswith(b"<"):
        return schema_ref, payload.decode("utf-8")

    ptr = payload.decode("utf-8", errors="replace").strip()
    if not ptr.lower().endswith(".xml"):
        raise RuntimeError(f"Schema resolver at {schema_ref!r} returned a non-XML pointer: {ptr!r}")
    resolved = urllib.parse.urljoin(schema_ref, ptr)
    req2 = urllib.request.Request(resolved, headers={"User-Agent": "poly-buy-sell-minimal-sbe-listener/1.0"})
    with urllib.request.urlopen(req2, timeout=10) as resp2:
        payload2 = resp2.read()
    if not payload2.lstrip().startswith(b"<"):
        raise RuntimeError(f"Resolved schema {resolved!r} is not XML.")
    return resolved, payload2.decode("utf-8")


def _resolve_fixed_type(
    type_name: str,
    fixed_types: dict[str, tuple[str, int]],
    composites: dict[str, list[tuple[str, str]]],
    stack: tuple[str, ...] = (),
) -> tuple[str, int]:
    direct = fixed_types.get(type_name)
    if direct is not None:
        return direct
    members = composites.get(type_name)
    if members is None:
        raise RuntimeError(f"Unknown SBE type: {type_name!r}")
    if type_name in stack:
        cycle = " -> ".join((*stack, type_name))
        raise RuntimeError(f"Recursive SBE type definition detected: {cycle}")

    fmts: list[str] = []
    total_size = 0
    next_stack = (*stack, type_name)
    for _, member_type in members:
        m_fmt, m_size = _resolve_fixed_type(member_type, fixed_types, composites, next_stack)
        fmts.append(m_fmt)
        total_size += m_size
    resolved = ("".join(fmts), total_size)
    fixed_types[type_name] = resolved
    return resolved


def _compile_schema(schema_url: str, message_name: str) -> CompiledMessage:
    resolved_schema_url, xml_text = _fetch_schema_xml(schema_url)
    root = ET.fromstring(xml_text)
    ns = {"sbe": "http://fixprotocol.io/2016/sbe"}

    schema_id = int(root.get("id") or "0")
    schema_version = int(root.get("version") or "0")

    fixed_types: dict[str, tuple[str, int]] = {
        primitive: (_PRIMITIVE_TO_STRUCT[primitive], _PRIMITIVE_SIZES[primitive]) for primitive in _PRIMITIVE_TO_STRUCT
    }
    composites: dict[str, list[tuple[str, str]]] = {}

    types_node = None
    for child in root:
        if _local_name(child.tag) == "types":
            types_node = child
            break
    if types_node is None:
        raise RuntimeError("Schema is missing <types> definition.")

    inline_counter = 0
    for node in types_node:
        ln = _local_name(node.tag)
        name = str(node.get("name") or "")
        if not name:
            continue
        if ln == "type":
            primitive = node.get("primitiveType")
            if primitive not in _PRIMITIVE_TO_STRUCT:
                continue
            length = int(node.get("length") or "1")
            base_fmt = _PRIMITIVE_TO_STRUCT[primitive]
            base_size = _PRIMITIVE_SIZES[primitive]
            if length == 1:
                fixed_types[name] = (base_fmt, base_size)
            else:
                if primitive == "char":
                    fixed_types[name] = (f"{length}s", length)
                else:
                    fixed_types[name] = (f"{length}{base_fmt}", length * base_size)
        elif ln in ("enum", "set"):
            encoding = node.get("encodingType")
            if encoding in fixed_types:
                fixed_types[name] = fixed_types[encoding]
        elif ln == "composite":
            members: list[tuple[str, str]] = []
            for member in node:
                mln = _local_name(member.tag)
                member_name = str(member.get("name") or "")
                if mln == "type":
                    primitive = member.get("primitiveType")
                    if primitive not in _PRIMITIVE_TO_STRUCT:
                        continue
                    length = int(member.get("length") or "1")
                    inline_name = f"__inline_{inline_counter}"
                    inline_counter += 1
                    base_fmt = _PRIMITIVE_TO_STRUCT[primitive]
                    base_size = _PRIMITIVE_SIZES[primitive]
                    if length == 1:
                        fixed_types[inline_name] = (base_fmt, base_size)
                    else:
                        if primitive == "char":
                            fixed_types[inline_name] = (f"{length}s", length)
                        else:
                            fixed_types[inline_name] = (f"{length}{base_fmt}", length * base_size)
                    members.append((member_name, inline_name))
                elif mln == "ref":
                    ref_type = str(member.get("type") or "")
                    if ref_type:
                        members.append((member_name, ref_type))
            composites[name] = members

    for composite_name in list(composites.keys()):
        _resolve_fixed_type(composite_name, fixed_types, composites)

    header_members = composites.get("messageHeader")
    if not header_members:
        raise RuntimeError("Schema does not define composite type 'messageHeader'.")

    header_fmt_parts: list[str] = []
    header_index: dict[str, int] = {}
    value_index = 0
    for member_name, member_type in header_members:
        member_fmt, _ = _resolve_fixed_type(member_type, fixed_types, composites)
        if _field_value_count(member_fmt) != 1:
            raise RuntimeError(f"Unsupported messageHeader field format for {member_name!r}: {member_fmt!r}")
        header_fmt_parts.append(member_fmt)
        header_index[member_name] = value_index
        value_index += 1
    header_struct = struct.Struct("<" + "".join(header_fmt_parts))

    msg_node = root.find(f".//sbe:message[@name='{message_name}']", ns)
    if msg_node is None:
        raise RuntimeError(
            f"Message {message_name!r} not found in schema {resolved_schema_url!r}. "
            "Choose a valid message name for this schema."
        )

    template_id = int(msg_node.get("id") or "0")
    root_fmt_parts: list[str] = []
    root_index: dict[str, int] = {}
    symbol_len_bytes = 0
    value_index = 0

    for child in msg_node:
        ln = _local_name(child.tag)
        if ln == "field":
            field_name = str(child.get("name") or "")
            field_type = str(child.get("type") or "")
            if not field_name or not field_type:
                continue
            field_fmt, _ = _resolve_fixed_type(field_type, fixed_types, composites)
            if _field_value_count(field_fmt) != 1:
                raise RuntimeError(f"Unsupported fixed field format for {field_name!r}: {field_fmt!r}")
            root_fmt_parts.append(field_fmt)
            root_index[field_name] = value_index
            value_index += 1
        elif ln == "data":
            data_name = str(child.get("name") or "")
            data_type = str(child.get("type") or "")
            if data_name.lower() == "symbol":
                members = composites.get(data_type, [])
                if members:
                    length_member_name, length_member_type = members[0]
                    if length_member_name.lower() == "length":
                        _, length_size = _resolve_fixed_type(length_member_type, fixed_types, composites)
                        if length_size in (1, 2, 4):
                            symbol_len_bytes = length_size

    if not root_fmt_parts:
        raise RuntimeError(f"Message {message_name!r} has no fixed root fields to decode.")
    root_struct = struct.Struct("<" + "".join(root_fmt_parts))

    return CompiledMessage(
        schema_url=resolved_schema_url,
        schema_id=schema_id,
        schema_version=schema_version,
        message_name=message_name,
        template_id=template_id,
        header_struct=header_struct,
        header_index=header_index,
        root_struct=root_struct,
        root_index=root_index,
        symbol_len_bytes=symbol_len_bytes,
    )


def _decode_symbol(buf: memoryview, offset: int, symbol_len_bytes: int) -> str:
    if symbol_len_bytes <= 0 or offset >= len(buf):
        return ""
    if symbol_len_bytes == 1:
        ln = int(buf[offset])
        start = offset + 1
    elif symbol_len_bytes == 2:
        if offset + 2 > len(buf):
            return ""
        ln = int.from_bytes(buf[offset : offset + 2], "little", signed=False)
        start = offset + 2
    elif symbol_len_bytes == 4:
        if offset + 4 > len(buf):
            return ""
        ln = int.from_bytes(buf[offset : offset + 4], "little", signed=False)
        start = offset + 4
    else:
        return ""
    end = start + ln
    if end > len(buf):
        return ""
    try:
        return bytes(buf[start:end]).decode("ascii", errors="ignore")
    except Exception:
        return ""


def _scaled_to_float(mantissa: int, exponent: int) -> float:
    if exponent >= 0:
        return float(mantissa * (10**exponent))
    return float(mantissa) / float(10 ** (-exponent))


def _consume_callback_task(task: asyncio.Task, tasks: set[asyncio.Task]) -> None:
    tasks.discard(task)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        _safe_print(f"callback_error={exc!r}")


def _dispatch_callback_result(result: object, tasks: set[asyncio.Task]) -> None:
    if not inspect.isawaitable(result):
        return
    task = asyncio.create_task(result)  # type: ignore[arg-type]
    tasks.add(task)
    task.add_done_callback(lambda done: _consume_callback_task(done, tasks))


def _resolve_config(args: argparse.Namespace) -> RuntimeConfig:
    ws_url = (args.ws_url or os.getenv("BINANCE_SBE_WS") or "").strip()
    symbol = (args.symbol or os.getenv("BINANCE_SBE_SYMBOL") or "btcusdt").strip().lower()
    stream = (args.stream or os.getenv("BINANCE_SBE_STREAM") or "bestBidAsk").strip()
    ws_base = (os.getenv("BINANCE_SBE_WS_BASE") or "wss://stream-sbe.binance.com/ws").strip()
    if not ws_url:
        ws_url = f"{ws_base.rstrip('/')}/{symbol}@{stream}"

    api_key = (args.api_key or os.getenv("BINANCE_SBE_API_KEY") or os.getenv("BINANCE_API_KEY") or "").strip()
    schema_url = (args.schema_url or os.getenv("BINANCE_SBE_SCHEMA_URL") or DEFAULT_SCHEMA_URL).strip()
    message_name = (args.message_name or os.getenv("BINANCE_SBE_MESSAGE_NAME") or DEFAULT_MESSAGE_NAME).strip()

    return RuntimeConfig(
        ws_url=ws_url,
        api_key=api_key,
        schema_url=schema_url,
        message_name=message_name,
        decode_symbol=bool(args.decode_symbol or _env_bool("BINANCE_SBE_DECODE_SYMBOL", False)),
        status_interval_ms=max(1, _env_int("BINANCE_SBE_STATUS_INTERVAL_MS", 20, min_value=1)),
        max_queue=max(1, _env_int("BINANCE_SBE_MAX_QUEUE", 1, min_value=1)),
        open_timeout_s=_env_float("BINANCE_SBE_OPEN_TIMEOUT_S", 3.0, min_value=0.1),
        close_timeout_s=_env_float("BINANCE_SBE_CLOSE_TIMEOUT_S", 1.0, min_value=0.1),
        reconnect_min_s=_env_float("BINANCE_SBE_RECONNECT_MIN_S", 0.25, min_value=0.0),
        reconnect_max_s=_env_float("BINANCE_SBE_RECONNECT_MAX_S", 8.0, min_value=0.1),
        reconnect_factor=_env_float("BINANCE_SBE_RECONNECT_FACTOR", 1.7, min_value=1.0),
        disable_gc=_env_bool("BINANCE_SBE_DISABLE_GC", True),
    )


def _required_index(idx: dict[str, int], key: str) -> int:
    if key not in idx:
        raise RuntimeError(f"Schema message is missing required field {key!r}.")
    return idx[key]


async def _consume_best_bid_ask(
    ws,
    cfg: RuntimeConfig,
    spec: CompiledMessage,
    *,
    on_tick: Callable[[BinanceTick], object] | None = None,
    on_tick_fields: TickFieldsCallback | None = None,
) -> None:
    recv = ws.recv
    header_unpack = spec.header_struct.unpack_from
    body_unpack = spec.root_struct.unpack_from
    header_size = spec.header_struct.size
    body_size = spec.root_struct.size

    block_length_idx = _required_index(spec.header_index, "blockLength")
    template_id_idx = _required_index(spec.header_index, "templateId")

    event_time_idx = _required_index(spec.root_index, "eventTime")
    update_id_idx = _required_index(spec.root_index, "bookUpdateId")
    price_exp_idx = _required_index(spec.root_index, "priceExponent")
    qty_exp_idx = _required_index(spec.root_index, "qtyExponent")
    bid_price_idx = _required_index(spec.root_index, "bidPrice")
    bid_qty_idx = _required_index(spec.root_index, "bidQty")
    ask_price_idx = _required_index(spec.root_index, "askPrice")
    ask_qty_idx = _required_index(spec.root_index, "askQty")

    status_enabled = on_tick is None and on_tick_fields is None and int(cfg.status_interval_ms) > 0
    status_interval_ns = int(cfg.status_interval_ms) * 1_000_000 if status_enabled else 0
    next_status_ns = time.monotonic_ns() + status_interval_ns
    ticks = 0
    last_update_id = 0
    callback_tasks: set[asyncio.Task] = set()

    try:
        while True:
            frame = await recv()
            if type(frame) is not bytes:
                continue

            view = memoryview(frame)
            if len(view) < header_size:
                continue

            header = header_unpack(view, 0)
            template_id = int(header[template_id_idx])
            if template_id != spec.template_id:
                continue

            block_length = int(header[block_length_idx])
            payload_offset = header_size
            if block_length < body_size:
                continue
            if len(view) < payload_offset + block_length:
                continue

            body = body_unpack(view, payload_offset)
            ticks += 1

            event_time_us = int(body[event_time_idx])
            update_id = int(body[update_id_idx])
            price_exp = int(body[price_exp_idx])
            qty_exp = int(body[qty_exp_idx])
            bid_price = int(body[bid_price_idx])
            bid_qty = int(body[bid_qty_idx])
            ask_price = int(body[ask_price_idx])
            ask_qty = int(body[ask_qty_idx])

            if on_tick_fields is not None:
                bid_f = _scaled_to_float(bid_price, price_exp)
                ask_f = _scaled_to_float(ask_price, price_exp)
                bid_qty_f = _scaled_to_float(bid_qty, qty_exp)
                ask_qty_f = _scaled_to_float(ask_qty, qty_exp)
                tick_result = on_tick_fields(event_time_us, update_id, bid_f, ask_f, bid_qty_f, ask_qty_f)
                _dispatch_callback_result(tick_result, callback_tasks)
            elif on_tick is not None:
                tick_result = on_tick(
                    BinanceTick(
                        event_time_us=event_time_us,
                        update_id=update_id,
                        bid=_scaled_to_float(bid_price, price_exp),
                        ask=_scaled_to_float(ask_price, price_exp),
                        bid_qty=_scaled_to_float(bid_qty, qty_exp),
                        ask_qty=_scaled_to_float(ask_qty, qty_exp),
                    )
                )
                _dispatch_callback_result(tick_result, callback_tasks)

            if not status_enabled:
                last_update_id = update_id
                continue

            now_ns = time.monotonic_ns()
            if now_ns < next_status_ns:
                last_update_id = update_id
                continue
            next_status_ns = now_ns + status_interval_ns

            now_us = time.time_ns() // 1000
            lag_us = now_us - event_time_us
            spread_m = ask_price - bid_price
            spread_f = _scaled_to_float(spread_m, price_exp)
            bid_f = _scaled_to_float(bid_price, price_exp)
            ask_f = _scaled_to_float(ask_price, price_exp)
            bid_qty_f = _scaled_to_float(bid_qty, qty_exp)
            ask_qty_f = _scaled_to_float(ask_qty, qty_exp)

            symbol = ""
            if cfg.decode_symbol and spec.symbol_len_bytes > 0:
                symbol = _decode_symbol(view, payload_offset + block_length, spec.symbol_len_bytes)
            symbol_txt = f" symbol={symbol}" if symbol else ""

            _safe_print(
                "tick="
                + str(ticks)
                + f" lag_us={lag_us}"
                + f" upd={update_id}"
                + f" bid={bid_f:.8f}"
                + f" ask={ask_f:.8f}"
                + f" spread={spread_f:.8f}"
                + f" bid_qty={bid_qty_f:.8f}"
                + f" ask_qty={ask_qty_f:.8f}"
                + symbol_txt
            )
            last_update_id = update_id
    finally:
        if callback_tasks:
            pending = list(callback_tasks)
            for _t in pending:
                _t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            callback_tasks.clear()


async def listen_forever(
    cfg: RuntimeConfig,
    spec: CompiledMessage,
    *,
    on_tick: Callable[[BinanceTick], object] | None = None,
    on_tick_fields: TickFieldsCallback | None = None,
) -> None:
    backoff = cfg.reconnect_min_s
    while True:
        try:
            headers = [("X-MBX-APIKEY", cfg.api_key)] if cfg.api_key else None
            async with websockets.connect(
                cfg.ws_url,
                additional_headers=headers,
                ping_interval=None,
                compression=None,
                max_queue=cfg.max_queue,
                open_timeout=cfg.open_timeout_s,
                close_timeout=cfg.close_timeout_s,
            ) as ws:
                _safe_print(
                    f"connected ws={cfg.ws_url} template_id={spec.template_id} "
                    f"schema_id={spec.schema_id} schema_version={spec.schema_version}"
                )
                backoff = cfg.reconnect_min_s
                await _consume_best_bid_ask(ws, cfg, spec, on_tick=on_tick, on_tick_fields=on_tick_fields)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _safe_print(f"listener_error={exc!r}; reconnecting_in={backoff:.2f}s")
            await asyncio.sleep(backoff)
            backoff = min(cfg.reconnect_max_s, max(cfg.reconnect_min_s, backoff * cfg.reconnect_factor))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal low-latency Binance SBE bestBidAsk listener.")
    parser.add_argument("--ws-url", default="", help="Override full websocket URL. Default builds from symbol+stream.")
    parser.add_argument("--symbol", default="", help="Spot symbol, e.g. btcusdt (used when --ws-url is omitted).")
    parser.add_argument("--stream", default="", help="Stream suffix, e.g. bestBidAsk (used when --ws-url is omitted).")
    parser.add_argument("--api-key", default="", help="Binance API key. Defaults to BINANCE_SBE_API_KEY/BINANCE_API_KEY.")
    parser.add_argument("--schema-url", default="", help="Schema XML URL or resolver URL.")
    parser.add_argument("--message-name", default="", help="SBE message name to decode.")
    parser.add_argument("--decode-symbol", action="store_true", help="Decode trailing symbol varString from payload.")
    parser.add_argument("--dry-run", action="store_true", help="Compile schema and print layout, then exit.")
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> None:
    cfg = _resolve_config(args)
    spec = _compile_schema(cfg.schema_url, cfg.message_name)

    _safe_print(
        f"schema_url={spec.schema_url} message={spec.message_name} template_id={spec.template_id} "
        f"root_size={spec.root_struct.size} header_size={spec.header_struct.size}"
    )
    _safe_print("root_fields=" + ", ".join(spec.root_index.keys()))

    if args.dry_run:
        return
    if not cfg.api_key:
        raise RuntimeError("Missing API key: set BINANCE_SBE_API_KEY (Ed25519 key) or pass --api-key.")

    if cfg.disable_gc:
        gc.disable()
    await listen_forever(cfg, spec)


if __name__ == "__main__":
    load_dotenv(SCRIPT_ENV_FILE, override=True)
    parsed_args = _parse_args()
    try:
        asyncio.run(_async_main(parsed_args))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        _safe_print(f"config_error: {exc}")
        raise SystemExit(2) from exc
