# Minimal Runtime Notes

`minimal/` is now a low-overhead prototype bot runtime, not just a probe folder.

The intended deployment target is one Python asyncio process on AWS Ubuntu EC2. The hot execution path should stay in-process:

- Polymarket market websocket -> `market_ws.listen_forever(..., on_event=runtime.orchestrator.on_market_event)`
- Polymarket user websocket -> `user_channel_ws.listen_forever(on_event=runtime.orchestrator.on_user_event)`
- Binance SBE websocket -> `binance_sbe_listener.listen_forever(..., on_tick_fields=runtime.orchestrator.on_binance_tick_fields)`
- Runtime state, local inventory, entry armory, exit armory, and hot-path order submitter are wired by `runtime_wiring.build_runtime(...)`.
- Stale live orders are cancelled by a slow supervisor loop using `LocalOrderTracker.stale_live_order_ids(...)` and `FastOrderSubmitter.cancel_orders(...)`.
- Shutdown cancels locally tracked live orders before closing HTTP clients, so an EC2 stop/restart does not intentionally leave known live orders behind.
- Buy submission is multi-position by design: up to `MINIMAL_MAX_CONCURRENT_POSITIONS` (default 3) concurrent entries are allowed across all market scopes.  The cap counts confirmed owned positions plus pending entry submits not yet reflected in WSS-owned inventory.

Callback mode is the production shape. It avoids subprocess fan-out, stdout parsing, per-event pretty JSON formatting, and runtime log writes. Standalone CLI mode remains available for manual debugging and packet inspection.

Run the standalone runtime from the repo root after filling `minimal/.env.poly` from `minimal/docs/ec2.env.example`:

```bash
python minimal/minimal_live_bot.py
```

Startup is intentionally guarded:

- `POLY_ALLOW_LIVE_ORDERS=true` is required for production live trading.
- `MINIMAL_DRY_RUN_ORDERS=true` enables non-transactional smoke tests; the runtime can connect to live feeds and build/sign templates, but submit/cancel calls are local no-ops and never hit Polymarket order endpoints.
- `MINIMAL_USDC_PER_TRADE` must be at least `1.01` for marketable BUY orders. The venue rejects effectively sub-dollar BUYs after amount truncation, so `1.00` is not executable live.
- `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are required live entry boundaries. Startup fails closed if either is missing or incoherent. Use `MINIMAL_DECISION_MIN_TTE_US=45000000` for a 45-second no-entry window before market expiry.
- `POLY_ALLOW_UNTRACKED_SELL` must stay false for autonomous runtime use.
- Historical startup positions are intentionally ignored (current-run-only design). Only resting open orders are checked at startup — an old order could still fill. Set `MINIMAL_ALLOW_DIRTY_START=true` to skip the open-order check for manual recovery.

## Hot Path Rules

- No raw event pretty-printing in callback mode.
- No JSON log writes on the execution path.
- No subprocess wrappers for the bot runtime.
- Use callback hooks directly in one event loop where viable.
- Keep debug output in standalone probe mode or exception/status paths only.
- Sell decisions must use `LocalOrderTracker` sellable inventory from user-channel confirmations.
- `LocalOrderTracker` now distinguishes:
  - `owned`: local exposure seen once a trade reaches `MATCHED`
  - `sellable`: confirmed liquid inventory only; BUY size is not sellable until the same trade reaches `CONFIRMED`
  This prevents the bot from trying to sell just-matched BUY fills before venue balance is actually available.
- Exit inventory is floored to the venue-supported `0.01` share quantum before
  it is treated as sellable or position-bearing. Residual dust below `0.01`
  remains in raw tracker accounting but does not trigger invalid zero-size SELL
  attempts or block new entries as false open exposure.
- Buy decisions must respect the `max_concurrent_positions` cap through `HotPathEngine` and `LocalOrderTracker.count_pending_entries()`.
- Buy decisions must use the same explicit min/max entry-price and no-entry TTE boundaries in `SignalDecisionConfig`, `TemplateArmory`, and `HotPathGuard`; do not rely on permissive defaults for live trading.
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
- `order_tracker.py` tracks user-channel orders, fills, `owned`, `settled`, reserved inventory, confirmed `sellable`, cost basis, exposure state, stale live order ids, and currently live order ids.
- `fast_order_submitter.py` submits prebuilt signed order bodies with fresh L2 headers and batches order cancels through `DELETE /orders`.
- `fast_order_submitter.py` signs order templates with `py_clob_client_v2` in the background but keeps the hot path as raw prebuilt body bytes plus fresh L2 headers.

## Runtime Knobs

- `MINIMAL_CANCEL_INTERVAL_S`: stale-order scan interval, default `0.25`.
- `MINIMAL_CANCEL_STALE_ORDER_S`: live-order age before batch cancel, default `2.0`.

## Strike Anchoring

For each market rotation, the orchestrator chooses the signal-engine strike
in this order:

1. **Polymarket Gamma explicit strike** (e.g. "BTC above $X" markets) — used
   verbatim when present (`event.strike > 0`).
2. **Slug-time Binance microprice anchor** for direction-only ("Up or Down")
   markets where Gamma carries no strike. The orchestrator buffers Binance
   microprice ticks and computes the median over the window
   `[slug_ts, slug_ts + 0.3 s]`. Strike is then immutable for the market.
3. **Fail closed** if the anchor window has already passed without any
   buffered ticks, or if `slug_ts` is missing. The orchestrator marks the
   market inactive (`anchor_unavailable`) and waits for the next rotation.

`BasisEstimator` is now telemetry-only — it no longer adjusts the engine's
strike. `MINIMAL_BINANCE_BASIS_USD` is preserved as a seed for the basis
estimator but does not affect trading thresholds.

## Probabilistic Signal Decision

`signal_decision.decide_buy` uses a Brownian barrier-cross probability:

```
P_yes = Phi((microprice - strike + gamma*move + alpha*OFI + beta*imbalance*sigma_px)
            / (sigma_scale * sigma_px * sqrt(tte_s)))
```

`sigma_px` is the realized volatility (stddev of consecutive microprice returns
normalized to per-second, with a floor). `move` is the microprice change over
the engine's window — it projects the current trend forward. The signal side
(YES/NO) is determined by momentum direction alone, not by whether microprice
currently sits above/below strike. This allows the engine to fire when tokens
are cheap (below 0.50) and momentum projects a crossing.

The decision computes `edge = side_prob - ask` and gates on `min_edge`
plus a hard probability floor `min_prob` (default 0.55). The path fails
closed (`prob_unavailable`) when strike or microprice is unset, tte is
out of bounds, or the implied sigma_eff is degenerate.

Set `MINIMAL_PROB_USE_LEGACY=true` to fall back to the legacy
`fair = 0.5 + strength*scale` heuristic. Defaults are conservative and
will trade *less* often than the legacy heuristic until alpha/beta/gamma
are fitted.

| Var | Default |
| --- | --- |
| `MINIMAL_PROB_ALPHA_OFI` | `0.0` |
| `MINIMAL_PROB_BETA_IMB` | `0.0` |
| `MINIMAL_PROB_GAMMA_MOVE` | `0.5` |
| `MINIMAL_PROB_SIGMA_SCALE` | `1.5` |
| `MINIMAL_PROB_SIGMA_FLOOR_USD` | `2.0` |
| `MINIMAL_PROB_FLOOR` | `0.02` |
| `MINIMAL_PROB_CEIL` | `0.98` |
| `MINIMAL_PROB_MIN_PROB` | `0.55` |
| `MINIMAL_PROB_MAX_TTE_US` | `600000000` |
| `MINIMAL_PROB_USE_LEGACY` | `false` |

## FAK Latency

`FastOrderSubmitter.submit` enforces a per-request `aiohttp.ClientTimeout`
of `total=1.0s, sock_connect=0.3s, sock_read=1.0s` so a stuck socket fails
fast instead of riding the session-wide 3 s timeout. Session keepalive is
75 s with persistent connection pool (`POLY_HTTP_CONN_LIMIT=8`).

The single biggest remaining latency win — moving the bot to `us-east-1`
near the Polymarket origin — is an infrastructure change tracked outside
this code.

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
