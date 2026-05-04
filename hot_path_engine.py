import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Protocol

from fast_order_submitter import FastOrderTemplate, extract_order_id
from order_tracker import LocalOrderTracker


_DEC_ZERO = Decimal("0")
_NS_PER_S = 1_000_000_000


class _Submitter(Protocol):
    async def submit(self, template: FastOrderTemplate) -> dict[str, Any]:
        ...


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    bid: Decimal
    ask: Decimal
    ts_ns: int


@dataclass(frozen=True, slots=True)
class HotPathGuard:
    max_ask: Decimal
    min_ask: Decimal = _DEC_ZERO
    min_bid: Decimal = _DEC_ZERO
    max_age_ns: int = 0


@dataclass(frozen=True, slots=True)
class ArmedTemplate:
    template: FastOrderTemplate
    guard: HotPathGuard
    side: str
    size: Decimal


@dataclass(frozen=True, slots=True)
class HotPathResult:
    submitted: bool
    reason: str
    order_id: str = ""
    latency_ns: int = 0
    response: dict[str, Any] | None = None


class HotPathEngine:
    """Submits armed templates on signal with strict guards."""

    __slots__ = (
        "_submitter",
        "_tracker",
        "_now_ns",
        "_max_quote_age_ns",
        "_quotes",
        "_armed",
        "_in_flight_buy",
        "_in_flight_sell",
        "_fired",
        "_active_buy_assets",
        "_max_concurrent_positions",
    )

    def __init__(
        self,
        *,
        submitter: _Submitter,
        tracker: LocalOrderTracker | None = None,
        now_ns: Callable[[], int] = time.monotonic_ns,
        max_quote_age_ns: int = 250_000_000,
        max_concurrent_positions: int = 3,
    ) -> None:
        self._submitter = submitter
        self._tracker = tracker
        self._now_ns = now_ns
        self._max_quote_age_ns = max(1, int(max_quote_age_ns))
        self._max_concurrent_positions = max(1, int(max_concurrent_positions))
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._armed: dict[str, ArmedTemplate] = {}
        self._in_flight_buy = False
        self._in_flight_sell = False
        self._fired: set[str] = set()
        self._active_buy_assets: set[str] = set()

    def set_exposure_scope(self, token_ids: set[str] | frozenset[str]) -> None:
        # Called after every market event.  Do NOT clear _armed here — that
        # was the root cause of "not_armed" (templates destroyed before
        # on_signal could use them).  Market rotation already calls
        # disarm_all() + armory.reset() separately.
        return

    def disarm_all(self) -> None:
        self._armed.clear()
        self._fired.clear()
        self._active_buy_assets.clear()

    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        key = signal.upper()
        self._armed[key] = ArmedTemplate(
            template=template,
            guard=guard,
            side=template.side.upper(),
            size=Decimal(str(template.size)),
        )
        self._fired.discard(key)

    def disarm(self, signal: str) -> None:
        key = signal.upper()
        self._armed.pop(key, None)
        self._fired.discard(key)

    def update_quote(self, token_id: str, *, bid: Decimal, ask: Decimal, ts_ns: int | None = None) -> None:
        self._quotes[token_id] = QuoteSnapshot(
            bid=bid if bid > 0 else _DEC_ZERO,
            ask=ask if ask > 0 else _DEC_ZERO,
            ts_ns=int(ts_ns if ts_ns is not None else self._now_ns()),
        )

    async def on_signal(self, signal: str) -> HotPathResult:
        key = signal.upper()
        start_ns = self._now_ns()
        armed = self._armed.get(key)
        if armed is None:
            return HotPathResult(False, "not_armed")
        if key in self._fired:
            return HotPathResult(False, "already_fired")
        if armed.side == "BUY" and self._in_flight_buy:
            return HotPathResult(False, "buy_in_flight")
        if armed.side == "SELL" and self._in_flight_sell:
            return HotPathResult(False, "sell_in_flight")

        template = armed.template
        quote = self._quotes.get(template.token_id)
        if quote is None:
            return HotPathResult(False, "quote_missing")

        max_age_ns = armed.guard.max_age_ns if armed.guard.max_age_ns > 0 else self._max_quote_age_ns
        age_ns = start_ns - quote.ts_ns
        if age_ns < 0 or age_ns > max_age_ns:
            return HotPathResult(False, "quote_stale")

        if armed.side == "BUY" and self._tracker is not None:
            open_assets: set[str] = set()
            open_assets.update(aid for aid, v in self._tracker.owned_by_asset.items() if v > 0)
            open_assets.update(self._active_buy_assets)
            for pending in self._tracker.pending_submits.values():
                if pending.intent != "entry":
                    continue
                if pending.status not in ("PENDING", "UNKNOWN", "CONFIRMED"):
                    continue
                open_assets.add(pending.asset_id)
            if len(open_assets) >= self._max_concurrent_positions:
                return HotPathResult(False, "max_positions")

        if armed.side == "BUY" and armed.guard.min_ask > 0 and quote.ask < armed.guard.min_ask:
            return HotPathResult(False, "ask_below_guard")
        if armed.guard.max_ask > 0 and (quote.ask <= 0 or quote.ask > armed.guard.max_ask):
            return HotPathResult(False, "ask_above_guard")
        if armed.guard.min_bid > 0 and quote.bid < armed.guard.min_bid:
            return HotPathResult(False, "bid_below_guard")
        if armed.side == "SELL":
            if self._tracker is None or not self._tracker.can_sell(template.token_id, armed.size):
                return HotPathResult(False, "insufficient_sellable")

        if armed.side == "BUY":
            self._in_flight_buy = True
        else:
            self._in_flight_sell = True
        submit_id = ""
        if self._tracker is not None:
            submit_id = self._tracker.register_submit(
                "entry" if armed.side == "BUY" else "exit",
                template.token_id,
                armed.side,
                armed.size,
                Decimal(str(template.price)),
                now_ts=start_ns / _NS_PER_S,
            )
        attempted_submit = False
        try:
            try:
                attempted_submit = True
                response = await self._submitter.submit(template)
            except Exception as exc:
                # Transport-level failure: the submit *may* have reached the
                # venue. We must not mark this as a definitive rejection,
                # otherwise a server-accepted order would become invisible
                # inventory. Instead mark UNKNOWN and conservatively treat
                # the cycle as if the order were live.
                self._handle_unknown_submit(armed, template, submit_id, start_ns, repr(exc))
                end_ns = self._now_ns()
                return HotPathResult(
                    submitted=False,
                    reason="submit_unknown",
                    latency_ns=max(0, end_ns - start_ns),
                    response={"success": False, "error": "submit_exception", "detail": repr(exc)},
                )
            classification = _classify_submit_response(response)
            order_id = extract_order_id(response)
            if classification == "accepted":
                if order_id:
                    if self._tracker is not None and submit_id:
                        self._tracker.confirm_submit_order_id(
                            submit_id,
                            order_id,
                            now_ts=self._now_ns() / _NS_PER_S,
                        )
                    self._fired.add(key)
                    if armed.side == "BUY":
                        self._active_buy_assets.add(template.token_id)
                    elif armed.side == "SELL":
                        self._active_buy_assets.discard(template.token_id)
                        if self._tracker is not None:
                            self._tracker.reserve_sell_order(
                                order_id,
                                template.token_id,
                                armed.size,
                                now_ts=start_ns / _NS_PER_S,
                            )
                    end_ns = self._now_ns()
                    return HotPathResult(
                        submitted=True,
                        reason="submitted",
                        order_id=order_id,
                        latency_ns=max(0, end_ns - start_ns),
                        response=response,
                    )
                self._handle_unknown_submit(
                    armed,
                    template,
                    submit_id,
                    start_ns,
                    "accepted_missing_order_id",
                )
                end_ns = self._now_ns()
                return HotPathResult(
                    submitted=False,
                    reason="submit_unknown",
                    latency_ns=max(0, end_ns - start_ns),
                    response=response,
                )

            if classification == "unknown":
                self._handle_unknown_submit(
                    armed,
                    template,
                    submit_id,
                    start_ns,
                    str(response.get("error") or response.get("_http_status") or "unknown"),
                )
                end_ns = self._now_ns()
                return HotPathResult(
                    submitted=False,
                    reason="submit_unknown",
                    order_id=order_id,
                    latency_ns=max(0, end_ns - start_ns),
                    response=response,
                )

            # Definitive server rejection.
            if armed.side == "BUY":
                self._active_buy_assets.discard(template.token_id)
            if self._tracker is not None and submit_id:
                self._tracker.mark_submit_failed(
                    submit_id,
                    error=str(response.get("error") or response.get("_http_status") or "rejected"),
                )
            end_ns = self._now_ns()
            return HotPathResult(
                submitted=False,
                reason="submit_failed",
                order_id=order_id,
                latency_ns=max(0, end_ns - start_ns),
                response=response,
            )
        finally:
            if attempted_submit:
                self.disarm(key)
            # If we reach finally without having added to _active_buy_assets
            # (exception or unknown path), the BUY didn't create a real position
            # so it must not count against the concurrency cap.
            if armed.side == "BUY" and not attempted_submit:
                self._active_buy_assets.discard(template.token_id)
            if armed.side == "BUY":
                self._in_flight_buy = False
            else:
                self._in_flight_sell = False

    def _handle_unknown_submit(
        self,
        armed: ArmedTemplate,
        template: FastOrderTemplate,
        submit_id: str,
        start_ns: int,
        error: str,
    ) -> None:
        if self._tracker is not None and submit_id:
            self._tracker.mark_submit_unknown(submit_id, error=error)
        if armed.side == "BUY":
            return
        # SELL unknown: provisionally reserve inventory under the submit_id
        # so the same inventory is not double-spent by a subsequent retry.
        # This must not create a synthetic live OrderState, otherwise cancel
        # and exposure checks will see a fake venue order forever.
        if self._tracker is not None and submit_id:
            self._tracker.reserve_unknown_sell_submit(
                submit_id,
                template.token_id,
                armed.size,
                now_ts=start_ns / _NS_PER_S,
            )


def _accepted_submit(response: dict[str, Any], order_id: str) -> bool:
    return _classify_submit_response(response) == "accepted" and bool(order_id)


def _classify_submit_response(response: dict[str, Any]) -> str:
    """Map a submitter response to one of: accepted | rejected | unknown.

    accepted: 2xx with success!=False (caller still needs to verify order_id).
    rejected: 4xx with a parseable body, or success=False and a clear error
              field — the order definitely did not land.
    unknown:  transport_error, 5xx without a definitive error, _http_status==0,
              or any response we cannot classify with confidence. The order
              MAY have been accepted server-side; callers must keep the
              pending submit alive for WSS reconciliation.
    """
    raw_status = response.get("_http_status")
    try:
        # Missing status is treated as 200 (legacy submitter shape used by
        # tests and any caller that does not annotate transport metadata).
        # Real production responses always carry _http_status.
        status = int(raw_status) if raw_status is not None else 200
    except (TypeError, ValueError):
        return "unknown"

    error = response.get("error")
    if error == "transport_error" or status == 0:
        return "unknown"

    if status >= 500:
        # 5xx is ambiguous unless the body explicitly says no order was placed.
        if response.get("success") is False and error and "transport" not in str(error).lower():
            return "rejected"
        return "unknown"
    if status >= 400:
        return "rejected"
    if response.get("success") is False:
        return "rejected"
    return "accepted"
