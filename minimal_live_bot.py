from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable, Protocol

import aiohttp
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client_v2 import ClobClient as ClobClientV2

MINIMAL_ROOT = Path(__file__).resolve().parent

import binance_sbe_listener
import market_ws
import user_channel_ws
from auth import L2Auth
from basis_estimator import BasisEstimator, BasisEstimatorConfig
from binance_signal_engine import BinanceSignalConfig
from config import BotConfig
from exit_policy import ExitPolicyConfig
from fast_order_submitter import DryRunOrderSubmitter, FastOrderSubmitter, HeaderSigner, prepare_template
from http_client import CLOBHttpClient
from order_placer import MinimalOrderConfig, _env_float, _env_int
from runtime_state import MinimalRuntimeState
from runtime_wiring import MinimalRuntime, RuntimeWiringConfig, build_runtime
from signal_decision import SignalDecisionConfig
from signal_model import CalibratedSignalModel, load_calibrated_model
from template_armory import ArmoryConfig


SCRIPT_ENV_FILE = Path(__file__).resolve().parent / ".env.poly"


class _Runtime(Protocol):
    orchestrator: object
    tracker: object
    hot_path: object


Listener = Callable[[Callable[..., object]], Awaitable[None]]


@dataclass(slots=True)
class LiveBot:
    runtime: MinimalRuntime
    market_cfg: BotConfig
    http: CLOBHttpClient
    session: aiohttp.ClientSession
    submitter: FastOrderSubmitter
    basis_path: Path | None = None

    async def close(self) -> None:
        try:
            await cancel_open_orders_once(tracker=self.runtime.tracker, submitter=self.submitter, limit=512)
        finally:
            if self.basis_path is not None and self.runtime.basis_estimator is not None:
                try:
                    self.runtime.basis_estimator.save(self.basis_path)
                except Exception:
                    pass
            await self.http.close()
            await self.session.close()


def _dec_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, "").strip()
    return Decimal(raw or default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _required_dec_env(name: str) -> Decimal:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required for live entry boundary enforcement.")
    return Decimal(raw)


def _required_int_env(name: str) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} is required for live entry boundary enforcement.")
    return int(raw)


def _entry_boundary_config() -> tuple[Decimal, Decimal, int]:
    min_buy = _required_dec_env("MINIMAL_MIN_BUY_LIMIT")
    max_buy = _dec_env("MINIMAL_MAX_BUY_LIMIT", os.getenv("MINIMAL_MAX_ASK", "0.60"))
    min_tte_us = _required_int_env("MINIMAL_DECISION_MIN_TTE_US")
    if min_buy <= 0:
        raise RuntimeError("MINIMAL_MIN_BUY_LIMIT must be > 0.")
    if max_buy <= min_buy:
        raise RuntimeError("MINIMAL_MAX_BUY_LIMIT must be greater than MINIMAL_MIN_BUY_LIMIT.")
    if min_tte_us <= 0:
        raise RuntimeError("MINIMAL_DECISION_MIN_TTE_US must be > 0.")
    return min_buy, max_buy, min_tte_us


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _order_type_env(name: str, default: str = "FAK") -> str:
    raw = os.getenv(name, "").strip().upper()
    order_type = raw or "FAK"
    if order_type in {"GTC", "GTD"} and not _bool_env("MINIMAL_ALLOW_RESTING_ORDERS", False):
        raise RuntimeError(
            f"{name}={order_type} is a resting order type. Set MINIMAL_ALLOW_RESTING_ORDERS=true intentionally."
        )
    return order_type


def _dry_run_order_mode() -> bool:
    dry_run = _bool_env("MINIMAL_DRY_RUN_ORDERS", False)
    live_orders = _bool_env("POLY_ALLOW_LIVE_ORDERS", False)
    if dry_run:
        return True
    if not live_orders:
        raise RuntimeError(
            "Refusing bot startup. Set POLY_ALLOW_LIVE_ORDERS=true for live orders "
            "or MINIMAL_DRY_RUN_ORDERS=true for non-transactional smoke tests."
        )
    return False


def _maybe_load_calibrated_model(path: Path, *, required: bool) -> CalibratedSignalModel | None:
    """Load and validate the calibrated signal model file.

    Returns None when MINIMAL_REQUIRE_CALIBRATED_MODEL=false and the file is
    missing — this is the only path that lets cold plumbing tests run on a
    machine with no calibrated artifact. In all other cases the model is
    loaded, schema-checked, and returned for application to the runtime
    decision/signal-engine configs.
    """
    if not required and not path.is_file():
        return None
    model = load_calibrated_model(path)
    logging.getLogger("minimal_live_bot.calibration").warning(model.summary_line())
    return model


def _ensure_calibrated_model(path: Path, *, required: bool) -> None:
    """Backwards-compatible wrapper used by tests; raises if required & missing."""
    _maybe_load_calibrated_model(path, required=required)


async def build_live_bot() -> LiveBot:
    load_dotenv(SCRIPT_ENV_FILE, override=True)
    dry_run_orders = _dry_run_order_mode()

    signal_model_path = Path(os.getenv("MINIMAL_SIGNAL_MODEL_PATH", "").strip() or (
        Path(__file__).resolve().parent / "state" / "signal_model.json"
    ))
    calibrated_model = _maybe_load_calibrated_model(
        signal_model_path,
        required=_bool_env("MINIMAL_REQUIRE_CALIBRATED_MODEL", True),
    )

    order_cfg = MinimalOrderConfig.from_env()
    if order_cfg.allow_untracked_sell:
        raise RuntimeError("POLY_ALLOW_UNTRACKED_SELL must stay false for the autonomous runtime.")

    if order_cfg.funder:
        clob = ClobClient(
            host=order_cfg.host,
            key=order_cfg.private_key,
            chain_id=order_cfg.chain_id,
            signature_type=order_cfg.signature_type,
            funder=order_cfg.funder,
        )
    else:
        clob = ClobClient(
            host=order_cfg.host,
            key=order_cfg.private_key,
            chain_id=order_cfg.chain_id,
            signature_type=order_cfg.signature_type,
        )
    creds = await asyncio.to_thread(clob.create_or_derive_api_creds)
    clob.set_api_creds(creds)
    v2_clob = ClobClientV2(
        host=order_cfg.host,
        key=order_cfg.private_key,
        chain_id=order_cfg.chain_id,
        signature_type=order_cfg.signature_type,
        funder=order_cfg.funder or None,
    )
    address = str(clob.get_address() or "")
    api_key = str(creds.api_key or "")
    api_secret = str(creds.api_secret or "")
    api_passphrase = str(creds.api_passphrase or "")
    if not address or not api_key or not api_secret or not api_passphrase:
        raise RuntimeError("Failed to initialize Polymarket API credentials.")

    session = aiohttp.ClientSession(
        base_url=order_cfg.host,
        timeout=aiohttp.ClientTimeout(total=_env_float("POLY_HTTP_TIMEOUT_S", 3.0)),
        raise_for_status=False,
        skip_auto_headers={"User-Agent"},
        connector=aiohttp.TCPConnector(
            limit=_env_int("POLY_HTTP_CONN_LIMIT", 8),
            limit_per_host=_env_int("POLY_HTTP_CONN_LIMIT_PER_HOST", 8),
            ttl_dns_cache=_env_int("POLY_HTTP_DNS_TTL_S", 600),
            use_dns_cache=True,
            keepalive_timeout=_env_float("POLY_HTTP_KEEPALIVE_S", 75.0),
            enable_cleanup_closed=True,
            force_close=False,
        ),
    )
    signer = HeaderSigner(address=address, api_key=api_key, api_passphrase=api_passphrase, api_secret=api_secret)
    submitter = DryRunOrderSubmitter(session) if dry_run_orders else FastOrderSubmitter(session, signer)

    cfg = BotConfig.from_env()
    auth = L2Auth(
        ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase),
        poly_address=address,
        cache_max_entries=cfg.auth_cache_max_entries,
    )
    http = CLOBHttpClient(cfg, auth, logging.getLogger("minimal_live_bot.market_http"))
    await ensure_flat_start(
        http,
        order_cfg.funder or address,
        allow_dirty_start=_bool_env("MINIMAL_ALLOW_DIRTY_START", False),
    )

    async def _build_template(**kwargs):
        # The autonomous runtime always passes an explicit order_type via
        # the entry/exit armory configs, both of which are routed through
        # _order_type_env's resting-order guard. Refusing the default-fallback
        # path here closes the H2 hole: no caller can silently produce a
        # GTC/GTD order without first opting into MINIMAL_ALLOW_RESTING_ORDERS.
        if "order_type" not in kwargs:
            raise RuntimeError(
                "_build_template requires an explicit `order_type`. "
                "This is enforced to prevent accidental resting orders."
            )
        kwargs.pop("owner", None)
        return await prepare_template(
            v2_clob,
            owner=api_key,
            order_type=kwargs.pop("order_type"),
            post_only=bool(kwargs.pop("post_only", order_cfg.post_only)),
            **kwargs,
        )

    basis_cfg = BasisEstimatorConfig(
        alpha=_float_env("MINIMAL_BASIS_EMA_ALPHA", 0.05),
        mid_tol=_float_env("MINIMAL_BASIS_MID_TOL", 0.05),
        min_tte_us=_int_env("MINIMAL_BASIS_MIN_TTE_US", 30_000_000),
        seed_basis=_float_env("MINIMAL_BINANCE_BASIS_USD", 0.0),
        seed_weight=1.0 if os.getenv("MINIMAL_BINANCE_BASIS_USD", "").strip() else 0.0,
    )
    basis_path = Path(os.getenv("MINIMAL_BASIS_STATE_PATH", "").strip() or (
        Path(__file__).resolve().parent / "state" / "basis.json"
    ))
    basis_estimator = BasisEstimator.load(basis_path, basis_cfg)
    min_buy_limit, max_buy_limit, min_entry_tte_us = _entry_boundary_config()

    runtime = build_runtime(
        RuntimeWiringConfig(owner=api_key, max_quote_age_ns=_int_env("MINIMAL_MAX_QUOTE_AGE_NS", 250_000_000)),
        state=MinimalRuntimeState(now_ns=time.monotonic_ns),
        submitter=submitter,
        build_template=_build_template,
        entry_cfg=ArmoryConfig(
            usdc_per_trade=_dec_env("MINIMAL_USDC_PER_TRADE", "10"),
            entry_slippage=_dec_env("MINIMAL_ENTRY_SLIPPAGE", "0"),
            min_size=_dec_env("MINIMAL_MIN_SIZE", "0.01"),
            min_buy_limit=min_buy_limit,
            max_buy_limit=max_buy_limit,
            order_type=_order_type_env("MINIMAL_ENTRY_ORDER_TYPE", "FAK"),
            post_only=_bool_env("MINIMAL_ENTRY_POST_ONLY", order_cfg.post_only),
            reprice_hysteresis_pct=_dec_env("MINIMAL_REPRICE_HYSTERESIS_PCT", "0.002"),
            max_quote_age_ns=_int_env("MINIMAL_MAX_QUOTE_AGE_NS", 250_000_000),
        ),
        exit_cfg=ExitPolicyConfig(
            take_profit_bps=_int_env("MINIMAL_TAKE_PROFIT_BPS", 1200),
            stop_loss_bps=_int_env("MINIMAL_STOP_LOSS_BPS", 1800),
            max_hold_us=_int_env("MINIMAL_MAX_HOLD_US", 0),
            force_exit_tte_us=_int_env("MINIMAL_FORCE_EXIT_TTE_US", 10_000_000),
            max_quote_age_us=_int_env("MINIMAL_EXIT_MAX_QUOTE_AGE_US", 250_000),
            min_bid=_dec_env("MINIMAL_EXIT_MIN_BID", "0.01"),
            order_type=_order_type_env("MINIMAL_EXIT_ORDER_TYPE", "FAK"),
        ),
        signal_cfg=_apply_signal_engine_overrides(_binance_signal_cfg(), calibrated_model),
        decision_cfg=_apply_decision_overrides(
            _entry_decision_cfg(min_buy_limit, max_buy_limit, min_entry_tte_us),
            calibrated_model,
        ),
        now_s=time.time,
        now_ns=time.monotonic_ns,
        basis_estimator=basis_estimator,
    )
    return LiveBot(
        runtime=runtime,
        market_cfg=cfg,
        http=http,
        session=session,
        submitter=submitter,
        basis_path=basis_path,
    )


def _apply_decision_overrides(
    cfg: SignalDecisionConfig, model: CalibratedSignalModel | None
) -> SignalDecisionConfig:
    if model is None:
        return cfg
    return model.apply_to_decision(cfg)


def _apply_signal_engine_overrides(
    cfg: BinanceSignalConfig, model: CalibratedSignalModel | None
) -> BinanceSignalConfig:
    if model is None:
        return cfg
    return model.apply_to_signal_engine(cfg)


def _binance_signal_cfg() -> BinanceSignalConfig:
    return BinanceSignalConfig(
        strike=_float_env("MINIMAL_STRIKE", float(os.getenv("MARKET_STRIKE", "0") or 0)),
        max_lag_us=_int_env("MINIMAL_SIGNAL_MAX_LAG_US", 400_000),
        min_window_us=_int_env("MINIMAL_SIGNAL_MIN_WINDOW_US", 250_000),
        max_window_us=_int_env("MINIMAL_SIGNAL_MAX_WINDOW_US", 2_000_000),
        max_spread=_float_env("MINIMAL_SIGNAL_MAX_SPREAD", 2.0),
        min_abs_move=_float_env("MINIMAL_SIGNAL_MIN_ABS_MOVE", 0.50),
        min_abs_ofi=_float_env("MINIMAL_SIGNAL_MIN_ABS_OFI", 1.0),
        min_imbalance=_float_env("MINIMAL_SIGNAL_MIN_IMBALANCE", 0.12),
        min_total_qty=_float_env("MINIMAL_SIGNAL_MIN_TOTAL_QTY", 0.000001),
        signal_cooldown_us=_int_env("MINIMAL_SIGNAL_COOLDOWN_US", 1_000_000),
        ring_size=_int_env("MINIMAL_SIGNAL_RING_SIZE", 128),
    )


def _entry_decision_cfg(
    min_buy_limit: Decimal,
    max_buy_limit: Decimal,
    min_entry_tte_us: int,
) -> SignalDecisionConfig:
    return SignalDecisionConfig(
        max_ask=float(max_buy_limit),
        min_ask=float(min_buy_limit),
        max_quote_age_us=_int_env("MINIMAL_DECISION_MAX_QUOTE_AGE_US", 250_000),
        min_tte_us=min_entry_tte_us,
        min_strength=_float_env("MINIMAL_DECISION_MIN_STRENGTH", 3.0),
        min_edge=_float_env("MINIMAL_DECISION_MIN_EDGE", 0.0),
        strength_price_scale=_float_env("MINIMAL_DECISION_STRENGTH_PRICE_SCALE", 0.03),
        prob_alpha_ofi=_float_env("MINIMAL_PROB_ALPHA_OFI", 0.0),
        prob_beta_imb=_float_env("MINIMAL_PROB_BETA_IMB", 0.0),
        prob_sigma_scale=_float_env("MINIMAL_PROB_SIGMA_SCALE", 1.5),
        prob_sigma_floor_usd=_float_env("MINIMAL_PROB_SIGMA_FLOOR_USD", 5.0),
        prob_floor=_float_env("MINIMAL_PROB_FLOOR", 0.02),
        prob_ceil=_float_env("MINIMAL_PROB_CEIL", 0.98),
        min_prob=_float_env("MINIMAL_PROB_MIN_PROB", 0.55),
        max_tte_us=_int_env("MINIMAL_PROB_MAX_TTE_US", 600_000_000),
        use_legacy_fair=_bool_env("MINIMAL_PROB_USE_LEGACY", False),
    )


async def ensure_flat_start(http, address: str, *, allow_dirty_start: bool) -> None:
    if allow_dirty_start:
        return
    # Historical positions are intentionally ignored (current-run-only design).
    # Only resting open orders are checked: an old order could fill during this
    # run and create invisible inventory that the bot never sold.
    open_orders = await http.clob_open_order_count()
    if open_orders > 0:
        raise RuntimeError(
            "Refusing startup with open Polymarket orders. "
            f"open_orders={open_orders}. Cancel open orders or set MINIMAL_ALLOW_DIRTY_START=true for manual recovery."
        )


async def _exit_loop(runtime: _Runtime, interval_s: float) -> None:
    delay = max(0.0, float(interval_s))
    while True:
        await runtime.orchestrator.evaluate_exit()  # type: ignore[attr-defined]
        await asyncio.sleep(delay)


async def cancel_stale_orders_once(
    *,
    tracker,
    submitter,
    now_ts: Callable[[], float],
    max_age_s: float,
    limit: int,
) -> int:
    ts = now_ts()
    order_ids = tracker.stale_live_order_ids(now_ts=ts, max_age_s=max_age_s, limit=limit)
    if not order_ids:
        return 0
    response = await submitter.cancel_orders(order_ids)
    if _cancel_accepted(response):
        _mark_cancelled(tracker, order_ids, ts)
    return len(order_ids)


async def cancel_open_orders_once(*, tracker, submitter, limit: int) -> int:
    order_ids = tracker.live_order_ids(limit=limit)
    if not order_ids:
        return 0
    response = await submitter.cancel_orders(order_ids)
    if _cancel_accepted(response):
        _mark_cancelled(tracker, order_ids, time.time())
    return len(order_ids)


def _cancel_accepted(response) -> bool:
    if isinstance(response, dict):
        status = response.get("_http_status")
        try:
            if status is not None and int(status) >= 400:
                return False
        except (TypeError, ValueError):
            return False
        if response.get("success") is False:
            return False
    return True


def _mark_cancelled(tracker, order_ids: list[str], ts: float) -> None:
    on_order_event = getattr(tracker, "on_order_event", None)
    if on_order_event is None:
        return
    for order_id in order_ids:
        on_order_event({"event_type": "order", "id": order_id, "status": "CANCELED", "timestamp": ts})


async def _cancel_loop(runtime: _Runtime, submitter: FastOrderSubmitter, interval_s: float, max_age_s: float) -> None:
    delay = max(0.001, float(interval_s))
    while True:
        await cancel_stale_orders_once(
            tracker=runtime.tracker,
            submitter=submitter,
            now_ts=time.time,
            max_age_s=max_age_s,
            limit=50,
        )
        await asyncio.sleep(delay)


def expire_unknown_submits_once(
    runtime: _Runtime,
    *,
    now_ts: Callable[[], float],
    max_age_s: float,
) -> int:
    """Expire stale UNKNOWN submits and release their local safeguards."""
    tracker = runtime.tracker
    expired = tracker.expire_unknown_submits(now_ts=now_ts(), max_age_s=max_age_s)
    for submit_id in expired:
        tracker.release_provisional_reservation(submit_id)
    if expired:
        release_lock = getattr(runtime.hot_path, "release_expired_unknown_buy_lock", None)
        if release_lock is not None:
            release_lock()
    return len(expired)


async def _unknown_submit_expiry_loop(runtime: _Runtime, interval_s: float, max_age_s: float) -> None:
    delay = max(0.001, float(interval_s))
    while True:
        expire_unknown_submits_once(runtime, now_ts=time.time, max_age_s=max_age_s)
        await asyncio.sleep(delay)


async def _basis_save_loop(estimator, path: Path, interval_s: float) -> None:
    delay = max(1.0, float(interval_s))
    while True:
        await asyncio.sleep(delay)
        try:
            estimator.save(path)
        except Exception:
            pass


async def run_supervised(
    runtime: _Runtime,
    *,
    market_listener: Listener,
    user_listener: Listener,
    binance_listener: Listener,
    exit_interval_s: float,
    cancel_submitter: FastOrderSubmitter | None = None,
    cancel_interval_s: float = 0.0,
    cancel_max_age_s: float = 0.0,
    unknown_submit_interval_s: float = 0.0,
    unknown_submit_max_age_s: float = 0.0,
    basis_save_path: Path | None = None,
    basis_save_interval_s: float = 0.0,
) -> None:
    orchestrator = runtime.orchestrator
    tasks = [
        asyncio.create_task(market_listener(orchestrator.on_market_event), name="minimal-market-ws"),
        asyncio.create_task(user_listener(orchestrator.on_user_event), name="minimal-user-ws"),
        asyncio.create_task(binance_listener(orchestrator.on_binance_tick_fields), name="minimal-binance-sbe"),
        asyncio.create_task(_exit_loop(runtime, exit_interval_s), name="minimal-exit-loop"),
    ]
    if cancel_submitter is not None and cancel_interval_s > 0 and cancel_max_age_s > 0:
        tasks.append(
            asyncio.create_task(
                _cancel_loop(runtime, cancel_submitter, cancel_interval_s, cancel_max_age_s),
                name="minimal-cancel-loop",
            )
        )
    if unknown_submit_interval_s > 0 and unknown_submit_max_age_s > 0:
        tasks.append(
            asyncio.create_task(
                _unknown_submit_expiry_loop(runtime, unknown_submit_interval_s, unknown_submit_max_age_s),
                name="minimal-unknown-submit-expiry",
            )
        )
    if (
        basis_save_path is not None
        and basis_save_interval_s > 0
        and getattr(runtime, "basis_estimator", None) is not None
    ):
        tasks.append(
            asyncio.create_task(
                _basis_save_loop(runtime.basis_estimator, basis_save_path, basis_save_interval_s),
                name="minimal-basis-save",
            )
        )
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    for task in pending:
        with suppress(asyncio.CancelledError):
            await task
    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


def _binance_args() -> argparse.Namespace:
    return SimpleNamespace(
        ws_url="",
        symbol="",
        stream="",
        api_key="",
        schema_url="",
        message_name="",
        decode_symbol=False,
        dry_run=False,
    )


async def run_live() -> None:
    bot = await build_live_bot()
    try:
        bcfg = binance_sbe_listener._resolve_config(_binance_args())
        bspec = binance_sbe_listener._compile_schema(bcfg.schema_url, bcfg.message_name)
        if bcfg.disable_gc:
            gc.disable()

        await run_supervised(
            bot.runtime,
            market_listener=lambda cb: market_ws.listen_forever(bot.market_cfg, bot.http, on_event=cb),
            user_listener=lambda cb: user_channel_ws.listen_forever(on_event=cb),
            binance_listener=lambda cb: binance_sbe_listener.listen_forever(bcfg, bspec, on_tick_fields=cb),
            exit_interval_s=_float_env("MINIMAL_EXIT_INTERVAL_S", 0.05),
            cancel_submitter=bot.submitter,
            cancel_interval_s=_float_env("MINIMAL_CANCEL_INTERVAL_S", 0.25),
            cancel_max_age_s=_float_env("MINIMAL_CANCEL_STALE_ORDER_S", 2.0),
            unknown_submit_interval_s=_float_env("MINIMAL_UNKNOWN_SUBMIT_INTERVAL_S", 0.05),
            unknown_submit_max_age_s=_float_env("MINIMAL_PENDING_UNKNOWN_TIMEOUT_S", 2.0),
            basis_save_path=bot.basis_path,
            basis_save_interval_s=_float_env("MINIMAL_BASIS_SAVE_INTERVAL_S", 60.0),
        )
    finally:
        await bot.close()


def main() -> int:
    load_dotenv(SCRIPT_ENV_FILE, override=True)
    logging.basicConfig(level=os.getenv("MINIMAL_LOG_LEVEL", "WARNING").upper(), force=True)
    os.chdir(MINIMAL_ROOT)
    try:
        asyncio.run(run_live())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
