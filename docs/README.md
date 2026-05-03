# Minimal Runtime Notes

`minimal/` is now a low-overhead prototype bot runtime, not just a probe folder.

The intended deployment target is one Python asyncio process on AWS Ubuntu EC2. The hot execution path should stay in-process:

- Polymarket market websocket -> `market_ws.listen_forever(..., on_event=runtime.orchestrator.on_market_event)`
- Polymarket user websocket -> `user_channel_ws.listen_forever(on_event=runtime.orchestrator.on_user_event)`
- Binance SBE websocket -> `binance_sbe_listener.listen_forever(..., on_tick_fields=runtime.orchestrator.on_binance_tick_fields)`
- Runtime state, local inventory, entry armory, exit armory, and hot-path order submitter are wired by `runtime_wiring.build_runtime(...)`.
- Stale live orders are cancelled by a slow supervisor loop using `LocalOrderTracker.stale_live_order_ids(...)` and `FastOrderSubmitter.cancel_orders(...)`.
- Shutdown cancels locally tracked live orders before closing HTTP clients, so an EC2 stop/restart does not intentionally leave known live orders behind.
- Buy submission is single-position by design: after a buy submits, further buys are blocked until the tracker sees the position sold and exposure returns flat.

Callback mode is the production shape. It avoids subprocess fan-out, stdout parsing, per-event pretty JSON formatting, and runtime log writes. Standalone CLI mode remains available for manual debugging and packet inspection.

Run the standalone runtime from the repo root after filling `minimal/.env.poly` from `minimal/docs/ec2.env.example`:

```bash
python minimal/minimal_live_bot.py
```

Startup is intentionally guarded:

- `POLY_ALLOW_LIVE_ORDERS=true` is required.
- `POLY_ALLOW_UNTRACKED_SELL` must stay false for autonomous runtime use.
- Historical startup position hydration is intentionally not implemented. If Data API reports existing positions, startup fails unless `MINIMAL_ALLOW_DIRTY_START=true` is set for manual recovery.

## Hot Path Rules

- No raw event pretty-printing in callback mode.
- No JSON log writes on the execution path.
- No subprocess wrappers for the bot runtime.
- Use callback hooks directly in one event loop where viable.
- Keep debug output in standalone probe mode or exception/status paths only.
- Sell decisions must use `LocalOrderTracker` sellable inventory from user-channel confirmations.
- Buy decisions must respect the one-unsold-position rule through `HotPathEngine` and `LocalOrderTracker.has_open_exposure()`.
- Stale order cancellation must stay off the signal hot path; it runs as a separate periodic supervisor task.
- Shutdown order cancellation is a lifecycle cleanup step, not a per-event operation.

## Current Implementation Map

- `runtime_wiring.py` builds shared runtime objects.
- `minimal_live_bot.py` starts the coherent EC2 runtime, supervises market WS, user WS, Binance SBE, exit evaluation, stale cancellation, and shutdown cleanup.
- `cold_latency_probe.py` measures local buy/sell hot-path placement latency with a stub submitter and no live exchange calls.
- `bot_orchestrator.py` routes market/user/Binance/exit events.
- `runtime_state.py` owns active market and quote state.
- `polymarket_market_feed.py` converts market websocket packets into quote/market state updates.
- `binance_signal_engine.py` converts Binance best-bid/ask movement into directional signals.
- `signal_decision.py` gates buy decisions.
- `template_armory.py` prebuilds entry order templates.
- `exit_policy.py` decides take-profit, stop-loss, expiry, and time exits.
- `exit_armory.py` prebuilds sell templates from exit decisions.
- `hot_path_engine.py` checks quote, inventory, and one-position-cycle guards, then submits armed templates.
- `order_tracker.py` tracks user-channel orders, fills, owned, reserved, sellable, cost basis, exposure state, stale live order ids, and currently live order ids.
- `fast_order_submitter.py` submits prebuilt signed order bodies with fresh L2 headers and batches order cancels through `DELETE /orders`.

## Runtime Knobs

- `MINIMAL_CANCEL_INTERVAL_S`: stale-order scan interval, default `0.25`.
- `MINIMAL_CANCEL_STALE_ORDER_S`: live-order age before batch cancel, default `2.0`.

## EC2 Startup

- Fill `minimal/.env.poly` on EC2 from `minimal/docs/ec2.env.example`.
- Use `minimal/docs/EC2_STANDALONE_RUNBOOK.md` for the `systemd` unit and startup commands.
- The filled env file is expected to live only on the protected EC2 host.

## Cold Latency Probe

Measure local hot-path overhead without placing orders:

```bash
python minimal/cold_latency_probe.py --runs 1000
```

The probe reports:

- `buy_submit_path_ns`: in-process BUY signal to submitter return.
- `sell_submit_path_ns`: in-process SELL signal to submitter return.
- `buy_to_sell_probe_gap_ns`: synthetic gap between the completed buy-path measurement and the sell-path trigger.

## Debug-Only Files

- `order_placer.py` is a guarded manual live-order probe, not the hot-path submitter.
- `docs/userchannel.log` and `docs/binanceseb.log` are replay/reference captures.
- Historical `_tmp_*` API snapshots were removed from active docs because they were stale, large, and not runtime inputs. Use live official docs for final decisions when behavior may have changed.

Use `graphify-out/GRAPH_REPORT.md` or `graphify-out/graph.json` before changing `/minimal`, then update `graphify-out/` after changes.
