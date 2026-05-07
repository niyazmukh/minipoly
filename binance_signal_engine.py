from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable


@dataclass(frozen=True, slots=True)
class BinanceTick:
    event_time_us: int
    update_id: int
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass(frozen=True, slots=True)
class BinanceSignalConfig:
    strike: float
    max_lag_us: int = 250_000
    min_window_us: int = 250_000
    max_window_us: int = 2_000_000
    max_spread: float = 2.0
    min_abs_move: float = 0.50
    min_abs_ofi: float = 1.0
    min_imbalance: float = 0.12
    min_total_qty: float = 0.000001
    ring_size: int = 128


@dataclass(frozen=True, slots=True)
class BinanceSignal:
    side: str
    reason: str
    event_time_us: int
    update_id: int
    microprice: float
    move: float
    sigma_px: float = 0.0
    strike: float = 0.0


@dataclass(frozen=True, slots=True)
class BinanceSignalSnapshot:
    ticks: int
    last_microprice: float
    last_ofi: float
    last_imbalance: float


@dataclass(slots=True)
class BinanceSignalStats:
    accepted: int = 0
    stale_updates: int = 0
    stale_event_time: int = 0
    stale_lag: int = 0
    invalid_quotes: int = 0
    spread_rejects: int = 0
    signals: int = 0


class BinanceSignalEngine:
    __slots__ = (
        "_cfg",
        "_now_us",
        "_event_times",
        "_microprices",
        "_ofis",
        "_cursor",
        "_count",
        "_last_event_time_us",
        "_last_update_id",
        "_last_bid",
        "_last_ask",
        "_last_bid_qty",
        "_last_ask_qty",
        "_last_microprice",
        "_last_ofi",
        "_last_imbalance",
        "stats",
    )

    def __init__(
        self,
        cfg: BinanceSignalConfig,
        *,
        now_us: Callable[[], int] | None = None,
    ) -> None:
        ring_size = max(8, int(cfg.ring_size))
        self._cfg = BinanceSignalConfig(
            strike=float(cfg.strike),
            max_lag_us=max(0, int(cfg.max_lag_us)),
            min_window_us=max(1, int(cfg.min_window_us)),
            max_window_us=max(int(cfg.min_window_us), int(cfg.max_window_us)),
            max_spread=float(cfg.max_spread),
            min_abs_move=float(cfg.min_abs_move),
            min_abs_ofi=float(cfg.min_abs_ofi),
            min_imbalance=float(cfg.min_imbalance),
            min_total_qty=max(0.0, float(cfg.min_total_qty)),
            ring_size=ring_size,
        )
        self._now_us = now_us if now_us is not None else _time_us
        self._event_times = [0] * ring_size
        self._microprices = [0.0] * ring_size
        self._ofis = [0.0] * ring_size
        self._cursor = 0
        self._count = 0
        self._last_event_time_us = 0
        self._last_update_id = 0
        self._last_bid = 0.0
        self._last_ask = 0.0
        self._last_bid_qty = 0.0
        self._last_ask_qty = 0.0
        self._last_microprice = 0.0
        self._last_ofi = 0.0
        self._last_imbalance = 0.0
        self.stats = BinanceSignalStats()

    def snapshot(self) -> BinanceSignalSnapshot:
        return BinanceSignalSnapshot(
            ticks=self._count,
            last_microprice=self._last_microprice,
            last_ofi=self._last_ofi,
            last_imbalance=self._last_imbalance,
        )

    @property
    def strike(self) -> float:
        return self._cfg.strike

    def set_strike(self, strike: float, *, reset_window: bool = True) -> None:
        self._cfg = replace(self._cfg, strike=float(strike))
        if reset_window:
            self.reset_window()

    def reset_window(self) -> None:
        size = len(self._event_times)
        for i in range(size):
            self._event_times[i] = 0
            self._microprices[i] = 0.0
            self._ofis[i] = 0.0
        self._cursor = 0
        self._count = 0

    def on_tick(self, tick: BinanceTick) -> BinanceSignal | None:
        return self.on_tick_fields(
            tick.event_time_us,
            tick.update_id,
            tick.bid,
            tick.ask,
            tick.bid_qty,
            tick.ask_qty,
        )

    def on_tick_fields(
        self,
        event_time_us: int,
        update_id: int,
        bid: float,
        ask: float,
        bid_qty: float,
        ask_qty: float,
    ) -> BinanceSignal | None:
        cfg = self._cfg
        event_time_us = int(event_time_us)
        update_id = int(update_id)
        if update_id <= self._last_update_id:
            self.stats.stale_updates += 1
            return None
        if event_time_us <= self._last_event_time_us:
            self.stats.stale_event_time += 1
            return None
        if cfg.max_lag_us > 0 and self._now_us() - event_time_us > cfg.max_lag_us:
            self.stats.stale_lag += 1
            return None

        bid = float(bid)
        ask = float(ask)
        bid_qty = float(bid_qty)
        ask_qty = float(ask_qty)
        total_qty = bid_qty + ask_qty
        spread = ask - bid
        if bid <= 0.0 or ask <= 0.0 or ask < bid or total_qty < cfg.min_total_qty:
            self.stats.invalid_quotes += 1
            return None
        if cfg.max_spread > 0.0 and spread > cfg.max_spread:
            self.stats.spread_rejects += 1
            return None

        microprice = ((bid * ask_qty) + (ask * bid_qty)) / total_qty
        imbalance = (bid_qty - ask_qty) / total_qty
        ofi = self._compute_ofi(bid, ask, bid_qty, ask_qty)

        self._last_update_id = update_id
        self._last_event_time_us = event_time_us
        self._last_bid = bid
        self._last_ask = ask
        self._last_bid_qty = bid_qty
        self._last_ask_qty = ask_qty
        self._last_microprice = microprice
        self._last_ofi = ofi
        self._last_imbalance = imbalance
        self.stats.accepted += 1
        self._append(event_time_us, microprice, ofi)
        # Strike is anchored externally (orchestrator reads slug_ts and seeds
        # the median Binance microprice over [slug_ts, slug_ts+0.3s]). When
        # strike has not yet been set, we accumulate window samples but do
        # not produce a signal.
        if self._cfg.strike <= 0.0:
            return None

        return self._maybe_signal(event_time_us, update_id, microprice, imbalance)

    def _compute_ofi(self, bid: float, ask: float, bid_qty: float, ask_qty: float) -> float:
        if self._count == 0:
            return 0.0
        prev_bid = self._last_bid
        prev_ask = self._last_ask
        prev_bid_qty = self._last_bid_qty
        prev_ask_qty = self._last_ask_qty
        bid_flow = (bid_qty if bid >= prev_bid else 0.0) - (prev_bid_qty if bid <= prev_bid else 0.0)
        ask_flow = (prev_ask_qty if ask >= prev_ask else 0.0) - (ask_qty if ask <= prev_ask else 0.0)
        return bid_flow + ask_flow

    def _append(self, event_time_us: int, microprice: float, ofi: float) -> None:
        idx = self._cursor
        self._event_times[idx] = event_time_us
        self._microprices[idx] = microprice
        self._ofis[idx] = ofi
        self._cursor = (idx + 1) % len(self._event_times)
        if self._count < len(self._event_times):
            self._count += 1

    def _maybe_signal(
        self,
        event_time_us: int,
        update_id: int,
        microprice: float,
        imbalance: float,
    ) -> BinanceSignal | None:
        if self._count < 2:
            return None

        oldest_time, oldest_micro, ofi_sum, sigma_px = self._window_baseline(event_time_us)
        if oldest_time <= 0:
            return None
        window_us = event_time_us - oldest_time
        if window_us < self._cfg.min_window_us:
            return None

        move = microprice - oldest_micro
        # Side is determined by momentum direction alone — NOT by whether
        # microprice currently sits above/below strike. This lets the engine
        # fire YES when BTC is moving up toward strike from below (YES tokens
        # are cheap) and NO when BTC is moving down toward strike from above
        # (NO tokens are cheap). The probability model in signal_decision
        # still accounts for distance-to-strike via the drift term.
        side = ""
        if move >= self._cfg.min_abs_move:
            side = "YES"
        elif move <= -self._cfg.min_abs_move:
            side = "NO"
        else:
            return None

        if side == "YES":
            if ofi_sum < self._cfg.min_abs_ofi or imbalance < self._cfg.min_imbalance:
                return None
        elif ofi_sum > -self._cfg.min_abs_ofi or imbalance > -self._cfg.min_imbalance:
            return None

        self.stats.signals += 1
        return BinanceSignal(
            side=side,
            reason="microprice_momentum",
            event_time_us=event_time_us,
            update_id=update_id,
            microprice=microprice,
            move=move,
            sigma_px=sigma_px,
            strike=self._cfg.strike,
        )

    def _window_baseline(self, event_time_us: int) -> tuple[int, float, float, float]:
        cutoff = event_time_us - self._cfg.max_window_us
        oldest_time = 0
        oldest_micro = 0.0
        ofi_sum = 0.0
        n = 0
        # Realized-volatility accumulator over consecutive in-window returns.
        # sigma_px = sqrt(Σ dp² / total_dt_s)  — instantaneous vol in price/√s.
        # This replaces the prior Welford stddev of microprice *levels*, which
        # conflated trending moves with volatility and collapsed P_yes to ~0.5
        # during directional episodes.
        prev_ts = 0
        prev_micro = 0.0
        sum_sq_returns = 0.0
        total_dt_s = 0.0
        count = self._count
        size = len(self._event_times)
        start = (self._cursor - count) % size
        for offset in range(count):
            idx = (start + offset) % size
            ts = self._event_times[idx]
            if ts < cutoff:
                continue
            micro = self._microprices[idx]
            if oldest_time == 0:
                oldest_time = ts
                oldest_micro = micro
            ofi_sum += self._ofis[idx]
            if n > 0:
                dt_s = max(ts - prev_ts, 1000) / 1_000_000.0
                dp = micro - prev_micro
                sum_sq_returns += dp * dp
                total_dt_s += dt_s
            prev_ts = ts
            prev_micro = micro
            n += 1
        if n >= 2 and total_dt_s > 0.0:
            sigma_px = (sum_sq_returns / total_dt_s) ** 0.5
        else:
            sigma_px = 0.0
        return oldest_time, oldest_micro, ofi_sum, sigma_px


def _time_us() -> int:
    return time.time_ns() // 1000
