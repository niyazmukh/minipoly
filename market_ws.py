import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import orjson
import websockets
from dotenv import load_dotenv
from py_clob_client.clob_types import ApiCreds

from auth import L2Auth
from config import BotConfig
from http_client import CLOBHttpClient
from log_utils import configure_logging, env_flag, full_trace_enabled, log_level, print_log
from utils import maybe_json_list, parse_gamma_iso8601_to_unix

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONCILE_INTERVAL_S = 10.0
PING_INTERVAL_S = 10.0
MINIMAL_ROOT = Path(__file__).resolve().parent
SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"

WATCHED_EVENT_TYPES = {
    "book",
    "price_change",
    "last_trade_price",
    "tick_size_change",
    "best_bid_ask",
    "new_market",
    "market_resolved",
}

CONTEXT_EVENT_TYPE = "minimal_market_context"
INACTIVE_EVENT_TYPE = "minimal_market_inactive"


@dataclass(slots=True)
class MarketContext:
    slug: str
    condition_id: str
    asset_ids: list[str]
    yes_token_id: str = ""
    no_token_id: str = ""
    yes_label: str = "Up"
    no_label: str = "Down"
    start_ts: float = 0.0
    end_ts: float = 0.0
    strike: float = 0.0
    slug_ts: int = 0


WebSocketConn = Any
EventCallback = Callable[[dict[str, Any]], object]


def _to_events(msg: Any) -> list[dict[str, Any]]:
    if isinstance(msg, dict):
        return [msg]
    if isinstance(msg, list):
        return [x for x in msg if isinstance(x, dict)]
    return []


def _event_type(ev: dict[str, Any]) -> str:
    return str(ev.get("event_type") or ev.get("eventType") or "")


def _event_targets_subscribed(ev: dict[str, Any], subscribed: set[str]) -> bool:
    aid = ev.get("asset_id") or ev.get("assetId")
    if aid is not None and str(aid) in subscribed:
        return True
    for key in ("assets_ids", "asset_ids"):
        arr = ev.get(key)
        if isinstance(arr, list):
            for x in arr:
                if str(x) in subscribed:
                    return True
    pcs = ev.get("price_changes") or ev.get("priceChanges")
    if isinstance(pcs, list):
        for pc in pcs:
            if isinstance(pc, dict):
                paid = pc.get("asset_id") or pc.get("assetId")
                if paid is not None and str(paid) in subscribed:
                    return True
    return False


# Strike values in btc-updown markets often appear in structured Gamma fields,
# question text, or description text. Live discovery fails closed when no
# unambiguous strike can be derived from market metadata.
_STRIKE_PATTERNS = (
    re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)"),
    re.compile(r"\$\s*([0-9]{4,7}(?:\.[0-9]+)?)"),
    re.compile(r"\b([0-9]{4,7}(?:\.[0-9]+)?)\s*(?:USD|USDT)?\b"),
)


def _coerce_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _extract_strike(market: dict[str, Any], event: dict[str, Any]) -> float:
    for source in (market, event):
        for key in ("strike", "strike_price", "strikePrice", "targetPrice", "target_price"):
            v = _coerce_float(source.get(key))
            if v > 0:
                return v
    text_sources: list[str] = []
    for src in (market, event):
        for key in ("question", "title", "description", "groupItemTitle", "slug"):
            v = src.get(key)
            if isinstance(v, str):
                text_sources.append(v)
    for text in text_sources:
        for pat in _STRIKE_PATTERNS:
            m = pat.search(text)
            if m:
                v = _coerce_float(m.group(1))
                if v > 1000.0:  # BTC-scale guard
                    return v
    return 0.0


def _select_yes_no(token_ids: list[str], outcomes_raw: Any) -> tuple[str, str, str, str]:
    outcomes = [str(x).strip() for x in maybe_json_list(outcomes_raw)]
    yes_idx = no_idx = None
    for idx, label in enumerate(outcomes):
        low = label.lower()
        if yes_idx is None and ("yes" in low or "up" in low or "above" in low or "higher" in low):
            yes_idx = idx
        elif no_idx is None and ("no" in low or "down" in low or "below" in low or "lower" in low):
            no_idx = idx
    if (
        yes_idx is not None
        and no_idx is not None
        and yes_idx != no_idx
        and yes_idx < len(token_ids)
        and no_idx < len(token_ids)
        and yes_idx < len(outcomes)
        and no_idx < len(outcomes)
    ):
        return token_ids[yes_idx], token_ids[no_idx], outcomes[yes_idx], outcomes[no_idx]
    yes_token = token_ids[0] if len(token_ids) > 0 else ""
    no_token = token_ids[1] if len(token_ids) > 1 else ""
    yes_label = outcomes[0] if len(outcomes) > 0 else "Up"
    no_label = outcomes[1] if len(outcomes) > 1 else "Down"
    return yes_token, no_token, yes_label, no_label


async def _discover_current_market(http: CLOBHttpClient, cfg: BotConfig) -> MarketContext | None:
    server_ts = await http.get_clob_time()
    window_s = int(cfg.market_window_s)
    current_window = server_ts - (server_ts % window_s)
    now = time.time()

    for ts in (current_window, current_window + window_s):
        slug = cfg.market_slug_fmt.format(ts=ts)
        event = await http.gamma_get_event_by_slug(slug)
        if not event or not event.get("active") or event.get("closed"):
            continue
        markets = event.get("markets")
        if not isinstance(markets, list):
            continue
        m = next(
            (
                mk
                for mk in markets
                if isinstance(mk, dict)
                and mk.get("active")
                and mk.get("acceptingOrders")
                and not mk.get("closed")
            ),
            None,
        )
        if not m:
            continue

        end_ts = parse_gamma_iso8601_to_unix(str(m.get("endDate") or ""))
        if end_ts > 0 and end_ts <= now:
            continue
        start_ts = parse_gamma_iso8601_to_unix(str(m.get("startDate") or ""))

        token_ids = [str(x) for x in maybe_json_list(m.get("clobTokenIds")) if x is not None]
        if len(token_ids) < 2:
            continue

        yes_token, no_token, yes_label, no_label = _select_yes_no(token_ids, m.get("outcomes"))
        if not yes_token or not no_token:
            continue
        condition_id = str(m.get("conditionId") or "")
        strike = _extract_strike(m, event)
        # strike=0 is valid for direction-only ("Up or Down") markets; the
        # signal engine auto-bootstraps the strike from the first Binance tick.
        return MarketContext(
            slug=slug,
            condition_id=condition_id,
            asset_ids=[yes_token, no_token],
            yes_token_id=yes_token,
            no_token_id=no_token,
            yes_label=yes_label,
            no_label=no_label,
            start_ts=start_ts,
            end_ts=end_ts,
            strike=strike,
            slug_ts=int(ts),
        )
    return None


async def _send_initial_subscribe(ws: WebSocketConn, asset_ids: list[str]) -> None:
    msg = {
        "assets_ids": asset_ids,
        "type": "market",
        "custom_feature_enabled": True,
    }
    await ws.send(orjson.dumps(msg).decode("utf-8"))


async def _send_incremental_subscribe(ws: WebSocketConn, asset_ids: list[str]) -> None:
    if not asset_ids:
        return
    msg = {
        "operation": "subscribe",
        "assets_ids": asset_ids,
        "custom_feature_enabled": True,
    }
    await ws.send(orjson.dumps(msg).decode("utf-8"))


def _make_context_event(reason: str, ctx: MarketContext) -> dict[str, Any]:
    return {
        "event_type": CONTEXT_EVENT_TYPE,
        "reason": reason,
        "slug": ctx.slug,
        "condition_id": ctx.condition_id,
        "yes_token_id": ctx.yes_token_id,
        "no_token_id": ctx.no_token_id,
        "yes_label": ctx.yes_label,
        "no_label": ctx.no_label,
        "start_ts": ctx.start_ts,
        "end_ts": ctx.end_ts,
        "strike": ctx.strike,
        "slug_ts": ctx.slug_ts,
    }


def _make_inactive_event(reason: str) -> dict[str, Any]:
    return {"event_type": INACTIVE_EVENT_TYPE, "reason": reason}


async def _emit_context_event(reason: str, ctx: MarketContext, on_event: EventCallback | None) -> None:
    if on_event is None:
        print_log(
            f"[context:{reason}] slug={ctx.slug} conditionId={ctx.condition_id} "
            f"yes={ctx.yes_token_id} no={ctx.no_token_id} strike={ctx.strike} end_ts={ctx.end_ts}"
        )
        return
    result = on_event(_make_context_event(reason, ctx))
    if asyncio.iscoroutine(result):
        await result


async def _emit_inactive_event(reason: str, on_event: EventCallback | None) -> None:
    if on_event is None:
        print_log(f"[market_inactive:{reason}]")
        return
    result = on_event(_make_inactive_event(reason))
    if asyncio.iscoroutine(result):
        await result


def _emit_status(line: str, *, on_event: EventCallback | None = None) -> None:
    if on_event is None:
        print_log(line)


async def _dispatch_event(ev: dict[str, Any], on_event: EventCallback | None) -> None:
    if on_event is None:
        print_log(f"\n[event_type={_event_type(ev)}]")
        print_log(orjson.dumps(ev, option=orjson.OPT_INDENT_2).decode("utf-8"))
        return
    result = on_event(ev)
    if asyncio.iscoroutine(result):
        await result


async def _maintenance_loop(
    ws: WebSocketConn,
    http: CLOBHttpClient,
    cfg: BotConfig,
    state: dict[str, Any],
    on_event: EventCallback | None,
) -> None:
    """Periodic reconcile + ping. Runs in parallel with the recv loop so the
    recv path never blocks on time-based work."""
    reconcile_event: asyncio.Event | None = state.get("reconcile_event")
    while True:
        now_mono = time.monotonic()
        next_due = min(state["next_ping"], state["next_reconcile"])
        delay = max(0.0, next_due - now_mono)
        if reconcile_event is None:
            if delay > 0:
                await asyncio.sleep(delay)
        else:
            try:
                await asyncio.wait_for(reconcile_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            reconcile_event.clear()
        now_mono = time.monotonic()

        if now_mono >= state["next_ping"]:
            state["next_ping"] = now_mono + PING_INTERVAL_S
            try:
                await ws.send("PING")
            except Exception:
                return

        if now_mono >= state["next_reconcile"]:
            state["next_reconcile"] = now_mono + RECONCILE_INTERVAL_S
            try:
                discovered = await _discover_current_market(http, cfg)
            except Exception:
                discovered = None
            if discovered is None:
                continue
            current: MarketContext = state["current"]
            if discovered.condition_id != current.condition_id or discovered.slug != current.slug:
                missing = [aid for aid in discovered.asset_ids if aid not in state["subscribed"]]
                if missing:
                    try:
                        await _send_incremental_subscribe(ws, missing)
                    except Exception:
                        return
                    state["subscribed"].update(missing)
                state["current"] = discovered
                await _emit_context_event("reconciled", discovered, on_event)


async def _recv_loop(
    ws: WebSocketConn,
    cfg: BotConfig,
    state: dict[str, Any],
    on_event: EventCallback | None,
) -> None:
    slug_prefix = cfg.market_slug_fmt.split("{ts}")[0]
    log_raw = full_trace_enabled() or env_flag("MINIMAL_LOG_EVERY_MARKET_WS_MESSAGE", False)
    while True:
        raw = await ws.recv()
        if log_raw:
            if isinstance(raw, bytes):
                raw_text = raw.decode("utf-8", errors="replace")
            else:
                raw_text = str(raw)
            print_log(f"market_ws_raw recv_us={time.time_ns() // 1000} bytes={len(raw_text)} raw={raw_text}")
        if isinstance(raw, bytes):
            if raw == b"PONG":
                continue
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
        else:
            if raw == "PONG":
                continue
            try:
                payload = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue

        for ev in _to_events(payload):
            et = _event_type(ev)
            if et not in WATCHED_EVENT_TYPES:
                continue
            if et == "new_market":
                if not str(ev.get("slug") or "").startswith(slug_prefix):
                    continue
                # Re-fetch full gamma metadata to populate strike/labels.
                # The WS new_market frame alone does not include strike.
                state["next_reconcile"] = 0.0
                reconcile_event = state.get("reconcile_event")
                if reconcile_event is not None:
                    reconcile_event.set()
                continue
            if et == "market_resolved":
                current: MarketContext = state["current"]
                ev_market = str(ev.get("market") or ev.get("condition_id") or ev.get("conditionId") or "").lower()
                if ev_market and current.condition_id and ev_market != current.condition_id.lower():
                    continue
                await _dispatch_event(ev, on_event)
                continue
            if not _event_targets_subscribed(ev, state["subscribed"]):
                # Fall back to slug/condition_id match for events that omit asset ids.
                ev_market = str(ev.get("market") or ev.get("condition_id") or ev.get("conditionId") or "").lower()
                ev_slug = str(ev.get("slug") or "")
                current = state["current"]
                if not (
                    (current.condition_id and ev_market == current.condition_id.lower())
                    or (current.slug and ev_slug == current.slug)
                ):
                    continue
            await _dispatch_event(ev, on_event)


async def listen_forever(
    cfg: BotConfig,
    http: CLOBHttpClient,
    *,
    on_event: EventCallback | None = None,
) -> None:
    backoff = 0.5
    while True:
        try:
            current = await _discover_current_market(http, cfg)
            if current is None:
                _emit_status("no active btc-updown-5m market found; retrying...", on_event=on_event)
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.8)
                continue

            async with websockets.connect(
                WS_URL,
                ping_interval=None,
                compression=None,
                open_timeout=5,
                close_timeout=2,
                max_queue=2048,
            ) as ws:
                await _send_initial_subscribe(ws, current.asset_ids)
                await _emit_context_event("bootstrap", current, on_event)
                backoff = 0.5

                state: dict[str, Any] = {
                    "current": current,
                    "subscribed": set(current.asset_ids),
                    "next_reconcile": time.monotonic() + RECONCILE_INTERVAL_S,
                    "next_ping": time.monotonic() + PING_INTERVAL_S,
                    "reconcile_event": asyncio.Event(),
                }
                recv_task = asyncio.create_task(_recv_loop(ws, cfg, state, on_event), name="market-ws-recv")
                maint_task = asyncio.create_task(
                    _maintenance_loop(ws, http, cfg, state, on_event), name="market-ws-maint"
                )
                try:
                    done, pending = await asyncio.wait(
                        {recv_task, maint_task}, return_when=asyncio.FIRST_EXCEPTION
                    )
                    for t in pending:
                        t.cancel()
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    for t in done:
                        exc = t.exception()
                        if exc is not None:
                            raise exc
                finally:
                    if not recv_task.done():
                        recv_task.cancel()
                    if not maint_task.done():
                        maint_task.cancel()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print_log(f"ws disconnected: {exc!r}; reconnecting in {backoff:.1f}s")
            await _emit_inactive_event("disconnected", on_event)
            await asyncio.sleep(backoff)
            backoff = min(10.0, backoff * 1.8)


async def _async_main() -> None:
    configure_logging(log_level("INFO"))
    os.chdir(MINIMAL_ROOT)
    load_dotenv(SCRIPT_ENV_FILE, override=True)
    try:
        cfg = BotConfig.from_env()
    except RuntimeError as exc:
        print_log(f"config error: {exc}")
        raise SystemExit(2) from exc

    dummy_creds = ApiCreds(api_key="DUMMY", api_secret="AA", api_passphrase="DUMMY")
    auth = L2Auth(
        dummy_creds,
        poly_address="0x0000000000000000000000000000000000000000",
        cache_max_entries=cfg.auth_cache_max_entries,
    )
    http = CLOBHttpClient(cfg, auth, logging.getLogger("minimal_market_ws"))
    try:
        await listen_forever(cfg, http)
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(_async_main())
