import argparse
import asyncio
import dataclasses
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterator

import orjson
import websockets
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

# Official references (discoverable from minimal/docs/llms.md):
# - https://docs.polymarket.com/api-reference/wss/user.md
# - https://docs.polymarket.com/market-data/websocket/user-channel.md
# - https://docs.polymarket.com/concepts/order-lifecycle.md
# - https://docs.polymarket.com/concepts/positions-tokens.md

WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"
DEFAULT_REPLAY_LOG = Path(__file__).resolve().parent / "docs" / "userchannel.log"
_DEC_ZERO = Decimal("0")
_SIZE_QUANTUM = Decimal("0.01")

_TERMINAL_ORDER_STATUSES = {
    "MATCHED",
    "FILLED",
    "CANCELED",
    "CANCELLED",
    "CANCELED_MARKET_RESOLVED",
    "EXPIRED",
    "INVALID",
    "FAILED",
}
_TERMINAL_TRADE_STATUSES = {"CONFIRMED", "FAILED"}
_VALID_TRADE_TRANSITIONS: dict[str, set[str]] = {
    "": {"MATCHED", "MINED", "RETRYING", "CONFIRMED", "FAILED"},
    "MATCHED": {"MATCHED", "MINED", "RETRYING", "CONFIRMED", "FAILED"},
    "MINED": {"MINED", "RETRYING", "CONFIRMED", "FAILED"},
    "RETRYING": {"MATCHED", "MINED", "RETRYING", "CONFIRMED", "FAILED"},
    "CONFIRMED": {"CONFIRMED"},
    "FAILED": {"FAILED"},
}


_DEBUG_USER_CHANNEL = os.getenv("MINIMAL_DEBUG_USER_CHANNEL", "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_print(line: str) -> None:
    try:
        print(line)
    except Exception:
        pass


def _hot_print(line: str) -> None:
    # Per-event hot-path observability is opt-in only. By default the user
    # channel does not write to stdout while a trade is being processed.
    if not _DEBUG_USER_CHANNEL:
        return
    try:
        print(line)
    except Exception:
        pass


def _parse_dec(raw: Any) -> Decimal:
    if raw is None:
        return _DEC_ZERO
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return _DEC_ZERO


def _floor_size_to_quantum(size: Decimal) -> Decimal:
    size = _parse_dec(size)
    if size <= 0:
        return _DEC_ZERO
    return (size / _SIZE_QUANTUM).to_integral_value(rounding=ROUND_DOWN) * _SIZE_QUANTUM


def _parse_ts(raw: Any) -> float:
    d = _parse_dec(raw)
    if d <= 0:
        return 0.0
    ts = float(d)
    if ts > 10_000_000_000:
        ts = ts / 1000.0
    return ts


def _event_ts(msg: dict[str, Any]) -> float:
    for key in ("timestamp", "last_update", "match_time", "matchtime", "created_at"):
        if key in msg:
            ts = _parse_ts(msg.get(key))
            if ts > 0:
                return ts
    return 0.0


def _norm_status(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip().upper()
    if s.startswith("ORDER_STATUS_"):
        s = s[len("ORDER_STATUS_") :]
    return s


def _order_status(msg: dict[str, Any]) -> str:
    status = _norm_status(msg.get("status"))
    if status:
        return status
    typ = _norm_status(msg.get("type") or msg.get("event_type"))
    if typ in {"PLACEMENT", "UPDATE"}:
        return "LIVE"
    if typ == "CANCELLATION":
        return "CANCELED"
    return typ


def _is_invalid_trade_transition(prev_status: str, next_status: str) -> bool:
    if not next_status:
        return False
    allowed = _VALID_TRADE_TRANSITIONS.get(prev_status)
    if allowed is None:
        return False
    return next_status not in allowed


def _to_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _event_kind(ev: dict[str, Any]) -> str:
    et = str(ev.get("event_type") or ev.get("eventType") or ev.get("type") or "").strip().lower()
    if et == "trade":
        return "trade"
    if et == "order" or et in {"placement", "update", "cancellation"}:
        return "order"
    return ""


@dataclass(frozen=True, slots=True)
class OrderState:
    order_id: str
    asset_id: str
    side: str
    original_size: Decimal
    size_matched: Decimal
    remaining: Decimal
    status: str
    updated_ts: float


@dataclass(frozen=True, slots=True)
class TradeState:
    trade_id: str
    taker_order_id: str
    asset_id: str
    side: str
    size: Decimal
    price: Decimal
    status: str
    applied: bool
    finalized: bool
    updated_ts: float


# Pending-submit lifecycle:
#   PENDING        - registered before HTTP submit; awaiting response.
#   CONFIRMED      - HTTP returned a usable order_id (or WSS bound the order).
#   FAILED         - server returned a definitive client/server rejection
#                    (4xx with a body that we could parse, or 5xx with explicit
#                    success=False and no order_id).
#   UNKNOWN        - the submit could not be definitively classified: transport
#                    failure, timeout, malformed body, or a 5xx without an
#                    informative payload. The order MAY have been accepted on
#                    the server. Strict mode keeps these candidates eligible
#                    for WSS matching (with size/price tolerance) so a server-
#                    accepted order can still be reconciled into the tracker.
_PENDING_MATCH_STATUSES = frozenset({"PENDING", "UNKNOWN"})
_EXPIRED_UNKNOWN_STATUS = "EXPIRED_UNKNOWN"
_MATCHABLE_SUBMIT_STATUSES = _PENDING_MATCH_STATUSES | frozenset({_EXPIRED_UNKNOWN_STATUS})


@dataclass(frozen=True, slots=True)
class PendingSubmit:
    submit_id: str
    intent: str
    asset_id: str
    side: str
    size: Decimal
    price: Decimal
    created_ts: float
    order_id_hint: str = ""
    confirmed_order_id: str = ""
    status: str = "PENDING"
    last_error: str = ""


class LocalOrderTracker:
    __slots__ = (
        "orders",
        "trades",
        "pending_submits",
        "owned_by_asset",
        "settled_by_asset",
        "cost_by_asset",
        "reserved_sell_by_asset",
        "_reserved_by_order",
        "_underflow_assets",
        "_current_run_only",
        "_submit_seq",
        "_order_to_submit",
        "_submit_hints",
    )

    def __init__(self, *, current_run_only: bool = False) -> None:
        self.orders: dict[str, OrderState] = {}
        self.trades: dict[str, TradeState] = {}
        self.pending_submits: dict[str, PendingSubmit] = {}
        self.owned_by_asset: dict[str, Decimal] = {}
        self.settled_by_asset: dict[str, Decimal] = {}
        self.cost_by_asset: dict[str, Decimal] = {}
        self.reserved_sell_by_asset: dict[str, Decimal] = {}
        self._reserved_by_order: dict[str, tuple[str, Decimal]] = {}
        self._underflow_assets: set[str] = set()
        self._current_run_only = bool(current_run_only)
        self._submit_seq = 0
        self._order_to_submit: dict[str, str] = {}
        self._submit_hints: dict[str, str] = {}

    def register_submit(
        self,
        intent: str,
        asset_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        *,
        now_ts: float,
        order_id_hint: str = "",
    ) -> str:
        self._submit_seq += 1
        submit_id = f"submit-{int(float(now_ts) * 1_000_000)}-{self._submit_seq}"
        hint = str(order_id_hint or "").strip()
        record = PendingSubmit(
            submit_id=submit_id,
            intent=str(intent or "").strip(),
            asset_id=str(asset_id or "").strip(),
            side=str(side or "").upper().strip(),
            size=_parse_dec(size),
            price=_parse_dec(price),
            created_ts=float(now_ts),
            order_id_hint=hint,
        )
        self.pending_submits[submit_id] = record
        if hint:
            self._submit_hints[hint] = submit_id
        return submit_id

    def pending_submit(self, submit_id: str) -> PendingSubmit | None:
        return self.pending_submits.get(str(submit_id or ""))

    def confirm_submit_order_id(self, submit_id: str, order_id: str, *, now_ts: float) -> PendingSubmit | None:
        pending = self.pending_submits.get(str(submit_id or ""))
        oid = str(order_id or "").strip()
        if pending is None or not oid:
            return None
        if pending.confirmed_order_id:
            return pending
        updated = dataclasses.replace(
            pending,
            confirmed_order_id=oid,
            status="CONFIRMED",
        )
        self.pending_submits[pending.submit_id] = updated
        self._order_to_submit[oid] = pending.submit_id
        # If SELL inventory was provisionally reserved under unknown:{submit_id},
        # move the accounting key to the real order id. An order event will
        # replace it with venue-reported remaining size; a trade-first event
        # can immediately reduce it by taker_order_id.
        self._move_provisional_reservation(pending.submit_id, oid)
        return updated

    def release_market_inventory(self, asset_ids: set[str] | frozenset[str]) -> None:
        """Drop tracker state for assets whose market has resolved.

        Polymarket settles binary markets at resolution; the position is
        liquidated by the protocol. The bot must not attempt to sell, must
        not consider the asset as inventory, and must not let the asset's
        accumulated owned balance leak across market rotations.
        """
        if not asset_ids:
            return
        scope = frozenset(a for a in asset_ids if a)
        for asset_id in scope:
            self.owned_by_asset.pop(asset_id, None)
            self.settled_by_asset.pop(asset_id, None)
            self.cost_by_asset.pop(asset_id, None)
            self.reserved_sell_by_asset.pop(asset_id, None)
            self._underflow_assets.discard(asset_id)
        # Clear any per-order reservations whose asset has resolved.
        for order_id, slot in list(self._reserved_by_order.items()):
            slot_asset, _ = slot
            if slot_asset in scope:
                self._reserved_by_order.pop(order_id, None)
        # Mark non-terminal orders for resolved assets as canceled-by-resolution.
        for order_id, order in list(self.orders.items()):
            if order.asset_id not in scope:
                continue
            if order.status in _TERMINAL_ORDER_STATUSES:
                continue
            self.orders[order_id] = dataclasses.replace(
                order,
                remaining=_DEC_ZERO,
                status="CANCELED_MARKET_RESOLVED",
            )
        # Drop pending submits for resolved assets so a stale UNKNOWN can't
        # bind a future event to a market that no longer exists.
        for submit_id, pending in list(self.pending_submits.items()):
            if pending.asset_id in scope and pending.status in _MATCHABLE_SUBMIT_STATUSES:
                self.pending_submits[submit_id] = dataclasses.replace(
                    pending, status="FAILED", last_error="market_resolved"
                )

    def release_provisional_reservation(self, submit_id: str) -> None:
        """Release a provisional SELL reservation tied to an UNKNOWN submit.

        Called when expire_unknown_submits decides the unknown was unaccepted.
        """
        self._release_reservation_key(f"unknown:{str(submit_id or '')}")

    def mark_submit_failed(self, submit_id: str, *, error: str = "") -> PendingSubmit | None:
        pending = self.pending_submits.get(str(submit_id or ""))
        if pending is None:
            return None
        updated = dataclasses.replace(pending, status="FAILED", last_error=str(error or ""))
        self.pending_submits[pending.submit_id] = updated
        return updated

    def mark_submit_unknown(self, submit_id: str, *, error: str = "") -> PendingSubmit | None:
        """Mark a submit as ambiguous — outcome could not be classified.

        The order MAY have been accepted on the server. The tracker keeps the
        submit eligible for WSS matching (with size/price tolerance) until
        either a binding order/trade event arrives or `expire_unknown_submits`
        prunes it as definitively unaccepted.
        """
        pending = self.pending_submits.get(str(submit_id or ""))
        if pending is None:
            return None
        if pending.status == "CONFIRMED":
            return pending
        updated = dataclasses.replace(pending, status="UNKNOWN", last_error=str(error or ""))
        self.pending_submits[pending.submit_id] = updated
        return updated

    def expire_unknown_submits(self, *, now_ts: float, max_age_s: float) -> list[str]:
        """Age UNKNOWN submits out of the active-unconfirmed set.

        Caller is responsible for choosing a window large enough that a
        server-accepted order would have surfaced via WSS by then. Returns
        the list of expired submit_ids so the caller can clear any
        provisional reservations or buy-cycle locks tied to them.
        """
        cutoff = float(now_ts) - max(0.0, float(max_age_s))
        expired: list[str] = []
        for submit_id, pending in self.pending_submits.items():
            if pending.status != "UNKNOWN":
                continue
            if pending.created_ts <= 0 or pending.created_ts > cutoff:
                continue
            self.pending_submits[submit_id] = dataclasses.replace(
                pending,
                status=_EXPIRED_UNKNOWN_STATUS,
                last_error=pending.last_error or "expired_unknown",
            )
            expired.append(submit_id)
        return expired

    def has_unconfirmed_submits(
        self,
        *,
        intent: str | None = None,
        asset_ids: set[str] | frozenset[str] | None = None,
    ) -> bool:
        for pending in self.pending_submits.values():
            if pending.status not in _PENDING_MATCH_STATUSES:
                continue
            if pending.confirmed_order_id:
                continue
            if intent is not None and pending.intent != intent:
                continue
            if asset_ids is not None and pending.asset_id not in asset_ids:
                continue
            return True
        return False

    def count_pending_entries(self) -> int:
        """Assets with live entry submits not yet reflected in owned_by_asset.

        During the WSS gap between submit-accepted and trade-CONFIRMED,
        owned_by_asset is blind.  Counting these pending submissions prevents
        duplicate entries when max_concurrent_positions would otherwise pass
        a second BUY before the first trade lands.
        """
        pending_assets: set[str] = set()
        for pending in self.pending_submits.values():
            if pending.intent != "entry":
                continue
            if pending.status not in ("PENDING", "UNKNOWN", "CONFIRMED"):
                continue
            if self.owned(pending.asset_id) > 0:
                continue
            pending_assets.add(pending.asset_id)
        return len(pending_assets)

    def owned(self, asset_id: str) -> Decimal:
        return self.owned_by_asset.get(asset_id, _DEC_ZERO)

    def settled(self, asset_id: str) -> Decimal:
        return self.settled_by_asset.get(asset_id, _DEC_ZERO)

    def reserved(self, asset_id: str) -> Decimal:
        return self.reserved_sell_by_asset.get(asset_id, _DEC_ZERO)

    def sellable(self, asset_id: str) -> Decimal:
        # MATCHED exposure is immediately sellable — waiting for CONFIRMED
        # settlement can miss the exit window on 5-min markets.  The venue
        # may reject with "not enough balance" if tokens haven't settled;
        # the orchestrator handles this with a per-asset 2s cooldown.
        liquid = self.owned(asset_id)
        s = liquid - self.reserved(asset_id)
        if s <= 0:
            return _DEC_ZERO
        return _floor_size_to_quantum(s)

    def can_sell(self, asset_id: str, size: Decimal) -> bool:
        requested = _floor_size_to_quantum(size)
        if requested <= 0:
            return False
        return self.sellable(asset_id) >= requested

    def reserve_sell_order(self, order_id: str, asset_id: str, size: Decimal, *, now_ts: float) -> OrderState | None:
        order_id = str(order_id or "").strip()
        asset_id = str(asset_id or "").strip()
        size = _parse_dec(size)
        if not order_id or not asset_id or size <= 0:
            return None
        record = OrderState(
            order_id=order_id,
            asset_id=asset_id,
            side="SELL",
            original_size=size,
            size_matched=_DEC_ZERO,
            remaining=size,
            status="LOCAL_SUBMITTED",
            updated_ts=float(now_ts),
        )
        self.orders[order_id] = record
        self._update_sell_reservation(record)
        return record

    def reserve_unknown_sell_submit(
        self,
        submit_id: str,
        asset_id: str,
        size: Decimal,
        *,
        now_ts: float,
    ) -> None:
        """Reserve SELL inventory for an UNKNOWN submit without inventing an order."""
        key = f"unknown:{str(submit_id or '').strip()}"
        asset_id = str(asset_id or "").strip()
        size = _parse_dec(size)
        if key == "unknown:" or not asset_id or size <= 0:
            return
        self._release_reservation_key(key)
        self._reserved_by_order[key] = (asset_id, size)
        self.reserved_sell_by_asset[asset_id] = self.reserved_sell_by_asset.get(asset_id, _DEC_ZERO) + size

    def average_entry_price(self, asset_id: str) -> Decimal:
        owned = self.owned(asset_id)
        if owned <= 0:
            return _DEC_ZERO
        cost = self.cost_by_asset.get(asset_id, _DEC_ZERO)
        return cost / owned if cost > 0 else _DEC_ZERO

    def position_size_and_entry(self, asset_id: str) -> tuple[Decimal, Decimal]:
        owned = self.owned_by_asset.get(asset_id, _DEC_ZERO)
        if owned <= 0:
            return _DEC_ZERO, _DEC_ZERO
        tradable = _floor_size_to_quantum(owned)
        if tradable <= 0:
            return _DEC_ZERO, _DEC_ZERO
        cost = self.cost_by_asset.get(asset_id, _DEC_ZERO)
        entry = cost / owned if cost > 0 else _DEC_ZERO
        return tradable, entry

    def stale_live_order_ids(self, *, now_ts: float, max_age_s: float, limit: int = 50) -> list[str]:
        cutoff = float(now_ts) - max(0.0, float(max_age_s))
        out: list[str] = []
        for order_id, order in self.orders.items():
            if order.status in _TERMINAL_ORDER_STATUSES or order.remaining <= 0 or order.updated_ts <= 0:
                continue
            if order.updated_ts <= cutoff:
                out.append(order_id)
                if len(out) >= limit:
                    break
        return out

    def live_order_ids(self, *, limit: int = 50) -> list[str]:
        out: list[str] = []
        for order_id, order in self.orders.items():
            if order.status in _TERMINAL_ORDER_STATUSES or order.remaining <= 0:
                continue
            out.append(order_id)
            if len(out) >= limit:
                break
        return out

    def has_open_exposure(self, token_ids: set[str] | frozenset[str] | None = None) -> bool:
        scope = token_ids if token_ids else None
        for asset_id, value in self.owned_by_asset.items():
            if scope is not None and asset_id not in scope:
                continue
            if _floor_size_to_quantum(value) > 0:
                return True
        for asset_id, value in self.reserved_sell_by_asset.items():
            if scope is not None and asset_id not in scope:
                continue
            if _floor_size_to_quantum(value) > 0:
                return True
        for order in self.orders.values():
            if scope is not None and order.asset_id not in scope:
                continue
            if order.status not in _TERMINAL_ORDER_STATUSES and _floor_size_to_quantum(order.remaining) > 0:
                return True
        return False

    def trade_count(self) -> int:
        return len(self.trades)

    def trade_count_in_scope(self, scope: set[str] | frozenset[str] | None) -> int:
        if not scope:
            return len(self.trades)
        n = 0
        for trade in self.trades.values():
            if trade.asset_id in scope:
                n += 1
        return n

    def on_order_event(self, msg: dict[str, Any]) -> OrderState | None:
        order_id = str(msg.get("id") or msg.get("order_id") or "").strip()
        if not order_id:
            return None

        prev = self.orders.get(order_id)
        status = _order_status(msg)
        asset_id = str(msg.get("asset_id") or (prev.asset_id if prev else "")).strip()
        side = str(msg.get("side") or (prev.side if prev else "")).upper().strip()

        original_size = _parse_dec(msg.get("original_size"))
        if original_size <= 0 and prev is not None:
            original_size = prev.original_size
        if original_size <= 0:
            original_size = _parse_dec(msg.get("size"))
        if original_size < 0:
            original_size = _DEC_ZERO

        size_matched = _parse_dec(msg.get("size_matched"))
        if size_matched <= 0 and prev is not None:
            size_matched = prev.size_matched
        if size_matched < 0:
            size_matched = _DEC_ZERO
        if original_size > 0 and size_matched > original_size:
            size_matched = original_size

        remaining = original_size - size_matched if original_size > size_matched else _DEC_ZERO
        updated_ts = _event_ts(msg)
        record = OrderState(
            order_id=order_id,
            asset_id=asset_id,
            side=side,
            original_size=original_size,
            size_matched=size_matched,
            remaining=remaining,
            status=status,
            updated_ts=updated_ts,
        )
        if prev is not None and prev.updated_ts > 0 and updated_ts > 0 and updated_ts < prev.updated_ts:
            return None
        matched_submit_id = self._match_submit_from_order(record, msg)
        if self._current_run_only and prev is None and not matched_submit_id:
            return None
        if prev is not None and self._same_order_state(prev, record):
            return None
        if matched_submit_id:
            self.confirm_submit_order_id(matched_submit_id, order_id, now_ts=updated_ts)
        self.orders[order_id] = record
        self._update_sell_reservation(record)

        if _DEBUG_USER_CHANNEL:
            sellable = self.sellable(asset_id) if asset_id else _DEC_ZERO
            _hot_print(
                f"order id={order_id} status={status or 'UNKNOWN'} side={side or '?'} "
                f"asset={asset_id or '?'} matched={size_matched} remaining={remaining} sellable={sellable}"
            )
        return record

    def on_trade_event(self, msg: dict[str, Any]) -> TradeState | None:
        trade_id = str(msg.get("id") or "").strip()
        if not trade_id:
            return None

        prev = self.trades.get(trade_id)
        taker_order_id = str(msg.get("taker_order_id") or (prev.taker_order_id if prev else "")).strip()
        if self._current_run_only and prev is None and not self._trade_belongs_to_current_run(taker_order_id, msg):
            return None
        status = _norm_status(msg.get("status")) or _norm_status(msg.get("type"))
        prev_status = prev.status if prev is not None else ""
        if _is_invalid_trade_transition(prev_status, status):
            return None

        size = _parse_dec(msg.get("size"))
        if size <= 0 and prev is not None:
            size = prev.size
        if size < 0:
            size = _DEC_ZERO

        price = _parse_dec(msg.get("price"))
        if price <= 0 and prev is not None:
            price = prev.price

        updated_ts = _event_ts(msg)
        if prev is not None and prev.updated_ts > 0 and updated_ts > 0 and updated_ts < prev.updated_ts:
            return None

        record = TradeState(
            trade_id=trade_id,
            taker_order_id=taker_order_id,
            asset_id=str(msg.get("asset_id") or (prev.asset_id if prev else "")).strip(),
            side=str(msg.get("side") or (prev.side if prev else "")).upper().strip(),
            size=size,
            price=price,
            status=status,
            applied=(prev.applied if prev is not None else False),
            finalized=(prev.finalized if prev is not None else False),
            updated_ts=updated_ts,
        )

        if status == "MATCHED" and not record.applied:
            self._apply_trade(record, reverse=False)
            record = dataclasses.replace(record, applied=True)
        elif status == "FAILED":
            if record.applied and not record.finalized:
                self._apply_trade(record, reverse=True)
            if not record.finalized:
                record = dataclasses.replace(record, finalized=True)
        elif status in _TERMINAL_TRADE_STATUSES and not record.finalized:
            if not record.applied:
                self._apply_trade(record, reverse=False)
                record = dataclasses.replace(record, applied=True)
            self._apply_settled_delta(record.asset_id, self._delta_for_trade(record))
            if record.side == "SELL" and record.taker_order_id:
                self._reduce_sell_reservation(record.taker_order_id, record.size)
            record = dataclasses.replace(record, finalized=True)

        if prev is not None and self._same_trade_state(prev, record):
            return None
        self.trades[trade_id] = record

        if _DEBUG_USER_CHANNEL:
            sellable = self.sellable(record.asset_id) if record.asset_id else _DEC_ZERO
            owned = self.owned(record.asset_id) if record.asset_id else _DEC_ZERO
            order_txt = record.taker_order_id or "?"
            _hot_print(
                f"trade id={trade_id} order={order_txt} status={status or 'UNKNOWN'} side={record.side or '?'} "
                f"asset={record.asset_id or '?'} size={record.size} price={record.price} owned={owned} sellable={sellable}"
            )
        return record

    def _delta_for_trade(self, record: TradeState) -> Decimal:
        if record.size <= 0:
            return _DEC_ZERO
        if record.side == "BUY":
            return record.size
        if record.side == "SELL":
            return -record.size
        return _DEC_ZERO

    def _apply_trade(self, record: TradeState, *, reverse: bool) -> None:
        delta = self._delta_for_trade(record)
        if reverse:
            delta = -delta
        if delta == 0 or not record.asset_id:
            return

        current_owned = self.owned(record.asset_id)
        current_cost = self.cost_by_asset.get(record.asset_id, _DEC_ZERO)
        if delta > 0:
            cost_delta = record.price * delta
        else:
            avg = current_cost / current_owned if current_owned > 0 and current_cost > 0 else _DEC_ZERO
            cost_delta = avg * delta

        self._apply_owned_delta(record.asset_id, delta)
        updated_owned = self.owned(record.asset_id)
        updated_cost = current_cost + cost_delta
        if updated_owned > 0 and updated_cost > 0:
            self.cost_by_asset[record.asset_id] = updated_cost
        else:
            self.cost_by_asset.pop(record.asset_id, None)

    @staticmethod
    def _same_order_state(prev: OrderState, nxt: OrderState) -> bool:
        return (
            prev.asset_id == nxt.asset_id
            and prev.side == nxt.side
            and prev.original_size == nxt.original_size
            and prev.size_matched == nxt.size_matched
            and prev.remaining == nxt.remaining
            and prev.status == nxt.status
        )

    @staticmethod
    def _same_trade_state(prev: TradeState, nxt: TradeState) -> bool:
        return (
            prev.taker_order_id == nxt.taker_order_id
            and prev.asset_id == nxt.asset_id
            and prev.side == nxt.side
            and prev.size == nxt.size
            and prev.price == nxt.price
            and prev.status == nxt.status
            and prev.applied == nxt.applied
            and prev.finalized == nxt.finalized
        )

    # Tolerance used when binding WSS orders/trades to pending submits whose
    # client-side response was lost (UNKNOWN status). Polymarket may round
    # price to tick or split parent orders. We accept original_size at or
    # above the pending size, and price within one tick.
    _MATCH_PRICE_TOLERANCE = Decimal("0.01")

    def _match_submit_from_order(self, order: OrderState, msg: dict[str, Any]) -> str:
        sid = self._order_to_submit.get(order.order_id)
        if sid:
            return sid
        hint_sid = self._submit_hints.get(order.order_id)
        if hint_sid:
            return hint_sid
        if not order.asset_id or not order.side:
            return ""

        price = _parse_dec(msg.get("price"))
        return self._best_pending_candidate(order.asset_id, order.side, order.original_size, price)

    def _best_pending_candidate(
        self,
        asset_id: str,
        side: str,
        order_size: Decimal,
        order_price: Decimal,
    ) -> str:
        candidates: list[str] = []
        for submit_id, pending in self.pending_submits.items():
            if pending.confirmed_order_id:
                continue
            if pending.status not in _MATCHABLE_SUBMIT_STATUSES:
                continue
            if pending.asset_id != asset_id or pending.side != side:
                continue
            if pending.size > 0 and order_size > 0:
                # Allow the venue to fill a smaller chunk than we asked for,
                # but never bind to a strictly larger requested size.
                if order_size > pending.size:
                    continue
            if pending.price > 0 and order_price > 0:
                diff = pending.price - order_price if pending.price > order_price else order_price - pending.price
                if diff > self._MATCH_PRICE_TOLERANCE:
                    continue
            candidates.append(submit_id)
            if len(candidates) > 1:
                return ""
        return candidates[0] if candidates else ""

    def _trade_belongs_to_current_run(self, taker_order_id: str, msg: dict[str, Any]) -> bool:
        if taker_order_id and (taker_order_id in self.orders or taker_order_id in self._order_to_submit):
            return True
        maker_orders = msg.get("maker_orders")
        if isinstance(maker_orders, list):
            for maker in maker_orders:
                if not isinstance(maker, dict):
                    continue
                oid = str(maker.get("order_id") or maker.get("id") or "").strip()
                if oid and (oid in self.orders or oid in self._order_to_submit):
                    return True
        # Last resort: a lost client-side response left a pending submit
        # in UNKNOWN with no order_id binding. Allow the trade to bind via
        # the same (asset, side, size, price) tolerance match used for
        # orders. This is what makes a transport-error -> server-accepted
        # path recover instead of leaking inventory.
        asset_id = str(msg.get("asset_id") or "").strip()
        side = str(msg.get("side") or "").upper().strip()
        if not asset_id or not side:
            return False
        size = _parse_dec(msg.get("size"))
        price = _parse_dec(msg.get("price"))
        sid = self._best_pending_candidate(asset_id, side, size, price)
        if not sid:
            return False
        if taker_order_id:
            # Bind the order_id to the recovered submit so subsequent events
            # for the same order_id route through the fast path.
            self.confirm_submit_order_id(sid, taker_order_id, now_ts=_event_ts(msg))
        return True

    def _apply_owned_delta(self, asset_id: str, delta: Decimal) -> None:
        if not asset_id or delta == 0:
            return
        current = self.owned_by_asset.get(asset_id, _DEC_ZERO)
        updated = current + delta
        if updated > 0:
            self.owned_by_asset[asset_id] = updated
            return
        if updated < 0 and asset_id not in self._underflow_assets:
            self._underflow_assets.add(asset_id)
            if _DEBUG_USER_CHANNEL:
                _hot_print(
                    f"warning asset={asset_id} inventory underflow while applying delta={delta}; "
                    "tracker likely started after positions were already open"
                )
        self.owned_by_asset.pop(asset_id, None)

    def _apply_settled_delta(self, asset_id: str, delta: Decimal) -> None:
        if not asset_id or delta == 0:
            return
        current = self.settled_by_asset.get(asset_id, _DEC_ZERO)
        updated = current + delta
        if updated > 0:
            self.settled_by_asset[asset_id] = updated
        else:
            self.settled_by_asset.pop(asset_id, None)

    def _update_sell_reservation(self, record: OrderState) -> None:
        prev_slot = self._reserved_by_order.get(record.order_id)
        if prev_slot is not None:
            prev_asset, prev_reserve = prev_slot
            if prev_asset and prev_reserve > 0:
                total = self.reserved_sell_by_asset.get(prev_asset, _DEC_ZERO) - prev_reserve
                if total > 0:
                    self.reserved_sell_by_asset[prev_asset] = total
                else:
                    self.reserved_sell_by_asset.pop(prev_asset, None)
            self._reserved_by_order.pop(record.order_id, None)

        reserve_next = _DEC_ZERO
        if record.side == "SELL" and record.remaining > 0 and record.status not in _TERMINAL_ORDER_STATUSES:
            reserve_next = record.remaining

        if reserve_next > 0 and record.asset_id:
            self._reserved_by_order[record.order_id] = (record.asset_id, reserve_next)
            self.reserved_sell_by_asset[record.asset_id] = self.reserved_sell_by_asset.get(record.asset_id, _DEC_ZERO) + reserve_next

    def _release_reservation_key(self, order_id: str) -> None:
        slot = self._reserved_by_order.pop(str(order_id or ""), None)
        self.orders.pop(str(order_id or ""), None)
        if slot is None:
            return
        asset_id, reserved = slot
        if not asset_id or reserved <= 0:
            return
        total = self.reserved_sell_by_asset.get(asset_id, _DEC_ZERO) - reserved
        if total > 0:
            self.reserved_sell_by_asset[asset_id] = total
        else:
            self.reserved_sell_by_asset.pop(asset_id, None)

    def _move_provisional_reservation(self, submit_id: str, order_id: str) -> None:
        provisional_key = f"unknown:{str(submit_id or '').strip()}"
        order_id = str(order_id or "").strip()
        if provisional_key == "unknown:" or not order_id:
            return
        slot = self._reserved_by_order.pop(provisional_key, None)
        self.orders.pop(provisional_key, None)
        if slot is None:
            return
        if order_id in self._reserved_by_order:
            self._release_reservation_key(order_id)
        self._reserved_by_order[order_id] = slot

    def _reduce_sell_reservation(self, order_id: str, fill_size: Decimal) -> None:
        if fill_size <= 0:
            return
        slot = self._reserved_by_order.get(order_id)
        if slot is None:
            return
        asset_id, reserved = slot
        used = fill_size if fill_size < reserved else reserved
        updated = reserved - used
        total = self.reserved_sell_by_asset.get(asset_id, _DEC_ZERO) - used
        if total > 0:
            self.reserved_sell_by_asset[asset_id] = total
        else:
            self.reserved_sell_by_asset.pop(asset_id, None)
        if updated > 0:
            self._reserved_by_order[order_id] = (asset_id, updated)
        else:
            self._reserved_by_order.pop(order_id, None)
        order = self.orders.get(order_id)
        if order is not None:
            matched = order.size_matched + used
            remaining = order.remaining - used
            if remaining < 0:
                remaining = _DEC_ZERO
            status = order.status if remaining > 0 else "MATCHED"
            self.orders[order_id] = dataclasses.replace(
                order,
                size_matched=matched,
                remaining=remaining,
                status=status,
            )

    def dump_positions(self) -> None:
        if not self.owned_by_asset and not self.settled_by_asset and not self.reserved_sell_by_asset:
            _safe_print("positions: empty")
            return
        _safe_print("positions:")
        assets = set(self.owned_by_asset.keys()) | set(self.settled_by_asset.keys()) | set(self.reserved_sell_by_asset.keys())
        for asset_id in sorted(assets):
            owned = self.owned(asset_id)
            settled = self.settled(asset_id)
            reserved = self.reserved(asset_id)
            sellable = self.sellable(asset_id)
            avg_entry = self.average_entry_price(asset_id)
            _safe_print(
                f"  asset={asset_id} owned={owned} settled={settled} reserved={reserved} "
                f"sellable={sellable} avg_entry={avg_entry}"
            )


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
    private_key = private_key.strip().strip('"').strip("'")
    if private_key.lower().startswith("0x"):
        private_key = private_key[2:]
    if len(private_key) != 64 or re.fullmatch(r"[0-9a-fA-F]{64}", private_key) is None:
        raise RuntimeError("POLY_PK/PRIVATE_KEY format is invalid; expected 64 hex chars (optional 0x prefix).")
    private_key = "0x" + private_key

    clob_host = os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com").strip() or "https://clob.polymarket.com"
    chain_id = int(os.getenv("POLY_CHAIN_ID", "137"))
    signature_type = int(os.getenv("POLY_SIG_TYPE", "0"))
    funder = os.getenv("POLY_FUNDER", "").strip()

    if funder:
        client = ClobClient(host=clob_host, key=private_key, chain_id=chain_id, signature_type=signature_type, funder=funder)
    else:
        client = ClobClient(host=clob_host, key=private_key, chain_id=chain_id, signature_type=signature_type)
    creds = await asyncio.to_thread(client.create_or_derive_api_creds)
    if not creds.api_key or not creds.api_secret or not creds.api_passphrase:
        raise RuntimeError("Failed to derive API credentials from POLY_PK.")
    return str(creds.api_key), str(creds.api_secret), str(creds.api_passphrase)


async def _consume_live(tracker: LocalOrderTracker) -> None:
    api_key, api_secret, api_passphrase = await _resolve_api_creds()
    ws_url = os.getenv("POLY_WS_USER", WS_USER_URL).strip() or WS_USER_URL

    backoff = 0.25
    while True:
        try:
            async with websockets.connect(
                ws_url,
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
                _safe_print("user channel subscribed")
                backoff = 0.25

                while True:
                    raw = await ws.recv()
                    try:
                        if isinstance(raw, bytes):
                            payload = orjson.loads(raw)
                        elif isinstance(raw, str):
                            msg = raw.strip()
                            if msg in ("PONG", "pong", "PING", "ping", ""):
                                continue
                            payload = orjson.loads(msg)
                        else:
                            payload = raw
                    except orjson.JSONDecodeError:
                        continue
                    for ev in _to_events(payload):
                        kind = _event_kind(ev)
                        if kind == "order":
                            tracker.on_order_event(ev)
                        elif kind == "trade":
                            tracker.on_trade_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _safe_print(f"user ws disconnected: {exc!r}; reconnecting in {backoff:.2f}s")
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 1.7)


def _iter_events_from_log(path: Path) -> Iterator[dict[str, Any]]:
    buf: list[str] = []
    depth = 0
    collecting = False
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not collecting:
                stripped = line.lstrip()
                if stripped.startswith("{"):
                    collecting = True
                    buf = [line]
                    depth = line.count("{") - line.count("}")
                    if depth == 0:
                        try:
                            obj = orjson.loads("".join(buf))
                            if isinstance(obj, dict):
                                yield obj
                        except orjson.JSONDecodeError:
                            pass
                        collecting = False
                        buf = []
                continue

            buf.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                text = "".join(buf)
                try:
                    obj = orjson.loads(text)
                    if isinstance(obj, dict):
                        yield obj
                except orjson.JSONDecodeError:
                    pass
                collecting = False
                buf = []
                depth = 0


def _run_replay(path: Path, tracker: LocalOrderTracker) -> None:
    if not path.exists():
        raise RuntimeError(f"Replay log not found: {path}")
    count = 0
    for ev in _iter_events_from_log(path):
        kind = _event_kind(ev)
        if kind == "order":
            tracker.on_order_event(ev)
            count += 1
        elif kind == "trade":
            tracker.on_trade_event(ev)
            count += 1
    _safe_print(f"replay complete events={count}")
    tracker.dump_positions()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Minimal order/trade tracker over Polymarket user channel. Tracks order status and sellable inventory."
    )
    parser.add_argument(
        "--replay-log",
        default="",
        help="Replay events from a user channel log file instead of live WS.",
    )
    return parser.parse_args()


async def _async_main() -> None:
    args = _parse_args()
    load_dotenv(SCRIPT_ENV_FILE, override=True)

    tracker = LocalOrderTracker()
    if args.replay_log:
        _run_replay(Path(args.replay_log).resolve(), tracker)
        return

    if DEFAULT_REPLAY_LOG.exists():
        _safe_print(f"tip: to validate quickly use --replay-log \"{DEFAULT_REPLAY_LOG}\"")
    await _consume_live(tracker)


if __name__ == "__main__":
    try:
        asyncio.run(_async_main())
    except RuntimeError as exc:
        _safe_print(f"config error: {exc}")
        raise SystemExit(2) from exc
