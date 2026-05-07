import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Protocol

from fast_order_submitter import FastOrderTemplate, extract_order_id
from order_tracker import LocalOrderTracker


_DEC_ZERO = Decimal("0")
_NS_PER_S = 1_000_000_000


def _is_fak_no_match(response: dict[str, Any]) -> bool:
    error = str(response.get("error") or response.get("errorMsg") or "").lower()
    return "no orders found to match with fak order" in error


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
    max_age_ns: int = 0
    allow_unconfirmed_sell: bool = False


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
        "_exposure_scope",
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
        self._exposure_scope: set[str] = set()

    def set_exposure_scope(self, token_ids: set[str] | frozenset[str]) -> None:
        # Called after every market event.  Do NOT clear _armed here — that
        # was the root cause of "not_armed" (templates destroyed before
        # on_signal could use them).  Market rotation already calls
        # disarm_all() + armory.reset() separately.
        self._exposure_scope = {str(token_id) for token_id in token_ids if token_id}

    def disarm_all(self) -> None:
        self._armed.clear()

    def arm(self, signal: str, template: FastOrderTemplate, guard: HotPathGuard) -> None:
        key = signal.upper()
        self._armed[key] = ArmedTemplate(
            template=template,
            guard=guard,
            side=template.side.upper(),
            size=template.size,
        )

    def disarm(self, signal: str) -> None:
        key = signal.upper()
        self._armed.pop(key, None)

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
        if armed.side == "BUY" and self._in_flight_buy:
            return HotPathResult(False, "buy_in_flight")

        template = armed.template
        quote = self._quotes.get(template.token_id)
        if quote is None:
            return HotPathResult(False, "quote_missing")

        max_age_ns = armed.guard.max_age_ns if armed.guard.max_age_ns > 0 else self._max_quote_age_ns
        age_ns = start_ns - quote.ts_ns
        if age_ns < 0 or age_ns > max_age_ns:
            return HotPathResult(False, "quote_stale")

        if armed.side == "BUY" and self._tracker is not None:
            # Max positions is WSS-authoritative: only tradable inventory from
            # the user channel counts as an open position.
            # In a binary market (2 tokens), max_concurrent_positions
            # effectively means "how many sides can be entered."  Default 3
            # allows both YES and NO; set to 1 for single-side only.
            open_assets: set[str] = set()
            scope = self._exposure_scope
            for aid in self._tracker.owned_by_asset:
                if scope and aid not in scope:
                    continue
                owned, _entry = self._tracker.position_size_and_entry(aid)
                if owned > 0:
                    open_assets.add(aid)
            for pending in self._tracker.pending_submits.values():
                if pending.intent != "entry":
                    continue
                if pending.status not in ("PENDING", "UNKNOWN"):
                    continue
                if scope and pending.asset_id not in scope:
                    continue
                open_assets.add(pending.asset_id)
            if len(open_assets) >= self._max_concurrent_positions:
                return HotPathResult(False, "max_positions")
            # Additional guard: never buy the same token twice while a
            # position exists.  Without this, the unique-asset check above
            # never fires in a binary market (max 2 tokens < cap of 3).
            if template.token_id in open_assets:
                return HotPathResult(False, "max_positions")

        if armed.side == "SELL" and not armed.guard.allow_unconfirmed_sell:
            if self._tracker is None or not self._tracker.can_sell(template.token_id, armed.size):
                return HotPathResult(False, "insufficient_sellable")

        if armed.side == "BUY":
            self._in_flight_buy = True
        submit_id = ""
        if self._tracker is not None:
            submit_id = self._tracker.register_submit(
                "entry" if armed.side == "BUY" else "exit",
                template.token_id,
                armed.side,
                armed.size,
                template.price,
                now_ts=start_ns / _NS_PER_S,
            )
        attempted_submit = False
        keep_armed_after_attempt = False
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
                self._handle_unknown_submit(armed, submit_id, repr(exc))
                keep_armed_after_attempt = True
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
                    end_ns = self._now_ns()
                    return HotPathResult(
                        submitted=True,
                        reason="submitted",
                        order_id=order_id,
                        latency_ns=max(0, end_ns - start_ns),
                        response=response,
                    )
                self._handle_unknown_submit(armed, submit_id, "accepted_missing_order_id")
                keep_armed_after_attempt = True
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
                    submit_id,
                    str(response.get("error") or response.get("_http_status") or "unknown"),
                )
                keep_armed_after_attempt = True
                end_ns = self._now_ns()
                return HotPathResult(
                    submitted=False,
                    reason="submit_unknown",
                    order_id=order_id,
                    latency_ns=max(0, end_ns - start_ns),
                    response=response,
                )

            # Definitive server rejection.
            if armed.side == "BUY" and _is_fak_no_match(response):
                keep_armed_after_attempt = True
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
            if attempted_submit and not keep_armed_after_attempt:
                self.disarm(key)
            if armed.side == "BUY":
                self._in_flight_buy = False

    def _handle_unknown_submit(self, armed: ArmedTemplate, submit_id: str, error: str) -> None:
        if self._tracker is not None and submit_id:
            self._tracker.mark_submit_unknown(submit_id, error=error)
        if armed.side == "BUY":
            return
        # FAK exits are allowed to retry aggressively. Inventory remains
        # WSS-authoritative; venue balance checks reject redundant attempts.


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
