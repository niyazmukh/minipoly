# Minimal Runtime Notes

`minimal/` is now a low-overhead prototype bot runtime, not just a probe folder.

The intended deployment target is one Python asyncio process on AWS Ubuntu EC2. The hot execution path should stay in-process:

- Polymarket market websocket -> `market_ws.listen_forever(..., on_event=runtime.orchestrator.on_market_event)`
- Polymarket user websocket -> `user_channel_ws.listen_forever(on_event=runtime.orchestrator.on_user_event)`
- Binance SBE websocket -> `binance_sbe_listener.listen_forever(..., on_tick_fields=runtime.orchestrator.on_binance_tick_fields)`
- Runtime state, local inventory, entry armory, exit armory, and hot-path order submitter are wired by `runtime_wiring.build_runtime(...)`.
- Entries and exits are FAK-only. The active runtime has no resting-order dependency or cancel supervisor.
- Startup still checks for pre-existing open CLOB orders unless `MINIMAL_ALLOW_DIRTY_START=true`; old resting orders from previous runs can create invisible fills.
- Buy submission is multi-position by design: up to `MINIMAL_MAX_CONCURRENT_POSITIONS` (default 3) concurrent entries are allowed across the active market scope. The hot-path guard counts WSS-owned positions plus entry submits that are still `PENDING` or `UNKNOWN` while WSS ownership has not landed yet.

Callback mode is the production shape. It avoids subprocess fan-out, stdout parsing, per-event pretty JSON formatting, and runtime log writes. Standalone CLI mode remains available for manual debugging and packet inspection.

Run the standalone runtime from the repo root after filling `minimal/.env.poly` from `minimal/docs/ec2.env.example`:

```bash
python minimal/minimal_live_bot.py
```

Startup is intentionally guarded:

- `POLY_ALLOW_LIVE_ORDERS=true` is required for production live trading.
- `MINIMAL_DRY_RUN_ORDERS=true` enables non-transactional smoke tests; the runtime can connect to live feeds and build/sign templates, but submit calls are local no-ops and never hit Polymarket order endpoints.
- `MINIMAL_USDC_PER_TRADE` must be at least `1.01` for marketable BUY orders. The venue rejects effectively sub-dollar BUYs after amount truncation, so `1.00` is not executable live.
- `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are required live entry boundaries. Startup fails closed if either is missing or incoherent. Use `MINIMAL_DECISION_MIN_TTE_US=45000000` for a 45-second no-entry window before market expiry.
- Historical startup positions are intentionally ignored (current-run-only design). Only resting open orders are checked at startup â€” an old order could still fill. Set `MINIMAL_ALLOW_DIRTY_START=true` to skip the open-order check for manual recovery.

## Hot Path Rules

- No raw event pretty-printing in callback mode.
- No JSON log writes on the execution path.
- No subprocess wrappers for the bot runtime.
- Use callback hooks directly in one event loop where viable.
- Keep debug output in standalone probe mode or exception/status paths only.
- Sell decisions must use `LocalOrderTracker` sellable inventory from user-channel confirmations.
- `LocalOrderTracker` now distinguishes:
  - `sellable`: MATCHED exposure is immediately sellable (no CONFIRMED wait). This lets exits fire faster â€” the venue accepts sells against just-matched inventory.
  - `owned`: local exposure tracked from MATCHED onward.
- HTTP matched responses are useful for an immediate SELL attempt, but they are not final inventory authority. Raw user-WSS `TRADE` events are the inventory authority.
- HTTP timeout/transport failures are tracked as `UNKNOWN` submits and remain WSS-bindable by order id or asset/side/size/price tolerance.
- Exit inventory is floored to the venue-supported `0.01` share quantum before
  it is treated as sellable or position-bearing. Residual dust below `0.01`
  remains in raw tracker accounting but does not trigger invalid zero-size SELL
  attempts or block new entries as false open exposure.
- Buy decisions must respect the `max_concurrent_positions` cap through `HotPathEngine`; the hot-path guard uses WSS-owned positions plus still-unconfirmed entry submits.
- Buy decisions must use the same explicit min/max entry-price and no-entry TTE boundaries in `SignalDecisionConfig`, `TemplateArmory`, and `HotPathGuard`; do not rely on permissive defaults for live trading.

## Current Implementation Map

- `runtime_wiring.py` builds shared runtime objects.
- `minimal_live_bot.py` starts the coherent EC2 runtime, supervises market WS, user WS, Binance SBE, exit evaluation, and UNKNOWN-submit expiry. Wires already-derived Polymarket API credentials into the user-channel WSS (no second independent credential-derivation path). Configures `MINIMAL_MAX_NOTIONAL_OVERRUN` and `MINIMAL_MAX_NOTIONAL_OVERRUN_BPS` for the entry armory.
- `bot_orchestrator.py` routes market/user/Binance/exit events. Includes sampled exit diagnostics (`exit_diag`) throttled to one line per 5s. Logs `entry_hot_path_result` and `exit_hot_path_result` at WARNING level.
- `runtime_state.py` owns active market and quote state (including L2 bid/ask depth, preserved across top-of-book-only updates).
- `polymarket_market_feed.py` converts market websocket packets into quote/market state updates, extracting and preserving L2 depth from book snapshots.
- `binance_signal_engine.py` converts Binance best-bid/ask movement into directional signals. It does not debounce valid signals; duplicate exposure belongs to `HotPathEngine` pending-submit and inventory guards.
- `signal_decision.py` gates buy decisions.
- `template_armory.py` prebuilds entry order templates. Uses `canonical_buy_target_for_notional()` to choose a venue-valid BUY size that does not silently exceed the configured trade notional beyond `max_notional_overrun` (default $0.01). Rejects at armory level when no valid size satisfies both venue minimum and notional cap. Stores armed state from template actuals (post-canonicalization).
- `exit_policy.py` decides take-profit and expiry exits only. There is no local stop-loss or max-hold exit rule in the minimal FAK runtime.
- `exit_armory.py` prebuilds sell templates from exit decisions. Logs previously-silent failures (`exit_armory_prepare_failed`, `exit_armory_not_armed`) without hot-path spam.
- `hot_path_engine.py` checks quote, inventory, and one-position-cycle guards, then submits armed templates. Uses `Decimal` fields directly from `FastOrderTemplate`.
- `order_tracker.py` tracks user-channel orders and trades, WSS-owned inventory, settled amount, sellable size, and cost basis. Includes race-safe trade-to-submit bind (`_match_submit_from_trade_msg`) so a WSS trade arriving before its order event can still reconcile an UNKNOWN submit.
- `fast_order_submitter.py` â€” **signed-body validation boundary**. `prepare_template()` canonicalizes order params (Decimal-first, no float churn), signs via the V2 SDK, then validates the serialized signed body before returning: maker amount â‰¤2dp, taker amount â‰¤4dp, implied price tick-aligned, implied price equals canonical price. No SDK global `ROUNDING_CONFIG` monkeypatch. Rejects invalid bodies locally before HTTP submit.
- `user_channel_ws.py` â€” app-level 10s PING (protocol pings disabled per official docs). Sparse lifecycle logs (`user_ws_connecting`, `user_ws_connected`, `user_ws_auth_sent`, `user_ws_disconnected`). Non-trade payloads logged as `user_ws_control_payload`. Credentials accepted via explicit parameters; independent credential derivation only used as fallback.

## Signed-Body Validation (Order Construction Boundary)

All orders flow through `fast_order_submitter.prepare_template()` which enforces:

1. **Input canonicalization** via `canonical_order_params()`:
   - BUY: price ceil-to-tick, size floor-to-4dp then adjusted via gcd-based lattice math so `price Ã— size` is 2dp-aligned
   - SELL: price floor-to-tick, size floor-to-2dp

2. **Serialized signed-body inspection** after SDK signing:
   - `makerAmount` aligned to `MAKER_AMOUNT_STEP = 0.01` (2dp)
   - `takerAmount` aligned to `TAKER_AMOUNT_STEP = 0.0001` (4dp)
   - Implied price (`maker/taker` for BUY, `taker/maker` for SELL) aligned to `PRICE_TICK = 0.01`
   - Implied price equals canonical input price

3. **Rejection before HTTP**: any violation raises `ValueError` locally. No invalid body reaches `FastOrderSubmitter.submit()`.

The V2 SDK default `ROUNDING_CONFIG` has `amount=5,6` for tick sizes 0.001/0.0001 (GitHub #253). This code does NOT mutate SDK globals â€” it canonicalizes inputs and validates outputs instead.

## Entry BUY Sizing with Notional Bounds

`TemplateArmory` delegates to `canonical_buy_target_for_notional()` which computes floor and ceil lattice sizes, then chooses:

- **Prefer ceil** when `ceil_maker â‰¤ target_usdc + max(max_notional_overrun, bps_overrun)`
- **Fall back to floor** when ceil exceeds the tolerance
- **Reject locally** when neither satisfies both `min_maker_amount` (venue minimum USDC) and `max_allowed_maker` (notional cap)

Default tolerance: `max_notional_overrun = $0.01`, `max_notional_overrun_bps = 0`.

Key examples under default tolerance:

| Price | Target | Floor (maker) | Ceil (maker) | Chosen |
|-------|--------|---------------|--------------|--------|
| 0.48 | $10 | 20.8125 ($9.99) | 20.8750 ($10.02 > $10.01) | floor $9.99 |
| 0.51 | $10 | 19.0000 ($9.69) | 20.0000 ($10.20) | floor $9.69 |
| 0.50 | $10 | 20.0000 ($10.00) | 20.0000 ($10.00) | ceil $10.00 |
| 0.67 | $1.01 | 1.0000 ($0.67 < $1.01) | 2.0000 ($1.34) | reject |
| 0.51 | $1.01 | 1.0000 ($0.51 < $1.01) | 2.0000 ($1.02 â‰¤ $1.02) | ceil $1.02 |

Precision canonicalization is not allowed to silently redefine trading risk.

## Exit Observability

Exit diagnostics are sampled (one line per 5s max) via `bot_orchestrator._maybe_log_exit_diag()`. Silent gates in `evaluate_exit()` now emit `exit_diag` with reason, asset, owned, sellable, bid, ask, and quote age. Idle `no_owned_assets` is suppressed unless an unconfirmed entry submit exists (`no_owned_assets_after_entry_submit`).

`exit_armory.py` logs failures that were previously swallowed: `exit_armory_prepare_failed` (build template exception), `exit_armory_not_armed` (post-await template mismatch). Failures are deduplicated â€” `prepare_failed` flag suppresses `not_armed` when the build already failed.

## User WSS Lifecycle

`user_channel_ws.py` uses app-level 10s PING (protocol pings disabled). Lifecycle logs at WARNING: `user_ws_connecting`, `user_ws_connected`, `user_ws_auth_sent`, `user_ws_disconnected` (with exception traceback). Non-trade payloads logged as `user_ws_control_payload`. API credentials are injected from `LiveBot` (already-derived), removing the second independent credential-derivation path from the production runtime.

## Offline Live Log Analyzer

`docs/analyze_bot_logs.py` is offline analysis tooling, not a runtime component. The live bot does not import it, and `.graphifyignore` excludes `docs/` so analyzer code does not inflate the runtime architecture graph. It parses both HTTP hot-path matched responses and raw `user_ws_raw` `TRADE` messages. WSS trades are first-class fills and are deduplicated by trade id across `MATCHED`, `MINED`, and `CONFIRMED` lifecycle updates. When an HTTP matched response and WSS trade share the same taker order id, analyzer aggregates with the WSS fill and reports the HTTP fill as shadowed instead of double-counting PnL.

## Runtime Knobs

- `MINIMAL_UNKNOWN_SUBMIT_INTERVAL_S`: UNKNOWN-submit expiry scan interval, default `0.05`.
- `MINIMAL_PENDING_UNKNOWN_TIMEOUT_S`: age after which an unbound UNKNOWN submit stops blocking local duplicate-entry checks, default `2.0`.
- `MINIMAL_MAX_NOTIONAL_OVERRUN`: max USD over target notional for BUY ceil sizing, default `0.01`.
- `MINIMAL_MAX_NOTIONAL_OVERRUN_BPS`: max bps over target notional for BUY ceil sizing, default `0`.

## Strike Anchoring

For each market rotation, the orchestrator chooses the signal-engine strike
in this order:

1. **Polymarket Gamma explicit strike** (e.g. "BTC above $X" markets) â€” used
   verbatim when present (`event.strike > 0`).
2. **Slug-time Binance microprice anchor** for direction-only ("Up or Down")
   markets where Gamma carries no strike. The orchestrator buffers Binance
   microprice ticks and computes the median over the window
   `[slug_ts, slug_ts + 0.3 s]`. Strike is then immutable for the market.
3. **Fail closed** if the anchor window has already passed without any
   buffered ticks, or if `slug_ts` is missing. The orchestrator marks the
   market inactive (`anchor_unavailable`) and waits for the next rotation.

There is no basis-estimator feedback path. The signal engine strike is anchored
from Polymarket explicit strike or the slug-time Binance microprice window and
then remains fixed until market rotation.

## Probabilistic Signal Decision

`signal_decision.decide_buy` uses a Brownian barrier-cross probability:

```
P_yes = Phi((microprice - strike + gamma*move)
            / (sigma_scale * sigma_px * sqrt(tte_s)))
```

`sigma_px` is the realized volatility (stddev of consecutive microprice returns
normalized to per-second, with a floor). `move` is the microprice change over
the engine's window â€” it projects the current trend forward. The signal side
(YES/NO) is determined by momentum direction alone, not by whether microprice
currently sits above/below strike. This allows the engine to fire when tokens
are cheap (below 0.50) and momentum projects a crossing.

The decision computes `edge = side_prob - ask` and gates on a **spread-aware effective minimum edge**:
`effective_min_edge = max(cfg.min_edge, ask - bid)`.
There is no historical spread table and no second absolute probability gate.
The path fails closed (`prob_unavailable`) when strike or microprice is unset, tte is
out of bounds, or the implied sigma_eff is degenerate. There is no legacy fair-value
fallback in the live runtime.


| Var | Default |
| --- | --- |
| `MINIMAL_PROB_GAMMA_MOVE` | `0.5` |
| `MINIMAL_PROB_SIGMA_SCALE` | `1.5` |
| `MINIMAL_PROB_SIGMA_FLOOR_USD` | `2.0` |
| `MINIMAL_PROB_FLOOR` | `0.02` |
| `MINIMAL_PROB_CEIL` | `0.98` |
| `MINIMAL_PROB_MAX_TTE_US` | `600000000` |

## FAK Latency

`FastOrderSubmitter.submit` enforces a per-request `aiohttp.ClientTimeout`
of `total=2.0s, sock_connect=0.5s, sock_read=2.0s`. This accommodates
eu-west-1 â†’ Polymarket US RTT (~300-400ms base + p95 jitter). Previously
1.0s which timed out every submit from eu-west-1. Session keepalive is
75 s with persistent connection pool (`POLY_HTTP_CONN_LIMIT=8`).

The single biggest remaining latency win â€” moving the bot to `us-east-1`
near the Polymarket origin â€” is an infrastructure change tracked outside
this code.

## EC2 Startup

- Fill `minimal/.env.poly` on EC2 from `minimal/docs/ec2.env.example`.
- Use `minimal/docs/EC2_STANDALONE_RUNBOOK.md` for the `systemd` unit and startup commands.
- The filled env file is expected to live only on the protected EC2 host.

## Debug-Only Files

- `docs/userchannel.log` and `docs/binanceseb.log` are replay/reference captures.
- Historical `_tmp_*` API snapshots were removed from active docs because they were stale, large, and not runtime inputs. Use live official docs for final decisions when behavior may have changed.

Use `graphify-out/GRAPH_REPORT.md` or `graphify-out/graph.json` before changing `/minimal`, then run `/graphify . --update` after changes. Run `graphify hook install` for auto-rebuild on git commits (AST-only, free). See `.claude/CLAUDE.md` for full graphify v6 command reference.
