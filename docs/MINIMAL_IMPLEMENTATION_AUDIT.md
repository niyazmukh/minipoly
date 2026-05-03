# Minimal Implementation Audit

Date: 2026-05-02

## Scope

This audit covers the prototype runtime under `minimal/`. It is separate from `src/`; use `minimal/graphify-out/` for code graph review.

## Current Runtime Shape

`minimal/` now has the core components of an independent asyncio bot runtime:

1. `minimal_live_bot.py`
   - Starts the standalone EC2 runtime.
   - Creates one shared aiohttp order session.
   - Runs Polymarket market WS, Polymarket user WS, Binance SBE, and exit evaluation as supervised asyncio tasks.
   - Cancels sibling tasks on failure instead of leaving partial runtime state alive.
   - Fails startup on non-zero unseeded inventory unless explicitly overridden for manual recovery.
   - Runs stale-order cancellation as a separate slow supervisor loop, not on the signal path.
   - Cancels locally tracked live orders during shutdown before HTTP clients close.
   - Uses `minimal/.env.poly` as the EC2 startup environment file.

2. `runtime_wiring.py`
   - Builds one shared `MinimalRuntime`.
   - Shares one `LocalOrderTracker` between exit policy and hot-path sell guards.

3. `market_ws.py`
   - Discovers and subscribes to the active Polymarket BTC up/down market.
   - In callback mode, sends target market events directly to the orchestrator without pretty-printing.
   - In standalone mode, still prints events for packet debugging.

4. `user_channel_ws.py`
   - Authenticates to the Polymarket user channel.
   - In callback mode, sends order/trade events directly to the orchestrator without event dumps.
   - In standalone mode, remains a packet inspector.

5. `binance_sbe_listener.py`
   - Decodes Binance SBE best-bid/ask frames.
   - `on_tick_fields` is the preferred hot callback because it avoids allocating `BinanceTick` objects.
   - Callback mode suppresses periodic status formatting.

6. `bot_orchestrator.py`
   - Applies market events directly through `apply_market_event`.
   - Routes user confirmations into `LocalOrderTracker`.
   - Evaluates Binance buy signals and sell exits.

7. `template_armory.py` and `exit_armory.py`
   - Prebuild buy/sell templates for fast submission.

8. `hot_path_engine.py`
   - Enforces quote freshness, price guards, in-flight/duplicate guard, one-unsold-position guard, and sellable inventory guard before submit.

9. `fast_order_submitter.py`
   - Sends prebuilt order bodies with fresh L2 headers through an existing aiohttp session.
   - Cancels live orders in batch through authenticated `DELETE /orders`.

## Removed Or Avoided Overhead

- Removed the no-value `PolymarketMarketFeed` wrapper; the orchestrator calls `apply_market_event` directly.
- Added `LocalOrderTracker.position_size_and_entry(...)` to avoid repeated position/cost lookups during exit sync.
- Added callback dispatch to market and user websocket listeners so EC2 runtime does not need subprocess/log parsing.
- Suppressed callback-mode context/status/event prints. Pretty JSON dumps remain only for standalone probe mode.
- Suppressed Binance SBE status formatting when a tick callback is attached.
- Added a single in-process EC2 entrypoint instead of subprocess wrappers.
- Added a startup dirty-inventory guard instead of unsafe partial position seeding.
- Added stale live-order scanning/cancellation on a periodic slow path.
- Added shutdown cancellation for locally tracked live orders as lifecycle cleanup.
- Added a one-unsold-position rule: after a buy submits, further buys are blocked until the tracked token is sold and exposure is flat.
- Scoped the one-unsold-position rule to the current market's YES/NO tokens, so unsold tokens from an old market do not block a new market.
- Added an EC2 env-file runbook and template.
- Added `cold_latency_probe.py` for no-exchange local buy/sell hot-path latency checks.

## Remaining Debug/Probe Surface

- `order_placer.py` remains a guarded manual live-order probe. The runtime should use `fast_order_submitter.py`.
- Reference captures under `docs/` are not runtime inputs unless explicitly replayed.

## Remaining Gaps

- Add deeper reconciliation if exchange/account APIs expose complete open-order snapshots. Current runtime cancels locally tracked stale orders while running and locally tracked live orders during shutdown.
- Keep reviewing exit and risk measures independently; do not blindly copy from `src/`.

## Verification Baseline

Current baseline after the latest streamlining pass:

- Full minimal tests: run `python -m pytest minimal\tests -q`.
- Callback suppression tests cover market/user websocket callback mode and Binance SBE callback mode.
- Standalone runtime tests cover callback wiring, sibling cancellation, config field compatibility, startup dirty-inventory guard, stale cancellation, and shutdown cancellation.
- Hot-path tests cover the one-unsold-position rule.
- Order-cancel tests cover tracker selection and authenticated batch cancel payloads.
- Graphify map was updated after code changes.
