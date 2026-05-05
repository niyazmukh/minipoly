# AI Handoff — 2026-05-05

**State**: Bot stopped. All fixes applied and tested. Ready for next live run.

## Quick Start

Read these in order:
1. This file (current state, known bugs, recent fixes)
2. `docs/README.md` (architecture, env vars, signed-body validation)
3. `docs/bot_live_20260505_070657.log` (35-min session, exit fix confirmed)
4. `docs/bot_live_20260505_054931.log` (earlier session, tick-size bug diagnosis)
5. `docs/EC2_STANDALONE_RUNBOOK.md` (SSH, deploy, operating model)
6. `.claude/CLAUDE.md` (agent instructions)

## SSH

```bash
ssh -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198
```
Key: `.ssh_tmp/poly-buy-sell.pem` (repo-relative). Host: `34.244.40.198` (eu-west-1). Bot dir: `/home/ubuntu/minimal-bot/`. Env: `/home/ubuntu/minimal-bot/.env.poly`.

**Never use `timeout`** on SSH. Start bot with `nohup` in background, tail logs separately. `pkill` on EC2 may fail with exit 255 — use `kill -9 <PID>` directly.

## Deploy Cycle

```bash
# 1. SCP all .py files (no docs/artifacts/tests on EC2)
scp -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no *.py ubuntu@34.244.40.198:/home/ubuntu/minimal-bot/

# 2. Verify env
ssh ... "grep -E 'MINIMAL_.*=|POLY_.*=' /home/ubuntu/minimal-bot/.env.poly"

# 3. Start bot in background, capture log
ssh ... "cd /home/ubuntu/minimal-bot && TS=\$(date +%Y%m%d_%H%M%S) && LOGFILE=\"bot_live_\${TS}.log\" && nohup python3 -u minimal_live_bot.py > \"\$LOGFILE\" 2>&1 & echo \"PID=\$! LOG=\$LOGFILE\""

# 4. Stream logs (separate SSH session)
ssh ... "tail -f /home/ubuntu/minimal-bot/bot_live_*.log | grep -E --line-buffered 'user_ws_|entry_hot_path|exit_hot_path|exit_diag|anchor_|binance_signal_decision|ERROR|CRITICAL|exception|Traceback'"

# 5. Kill bot
ssh ... "kill -9 <PID>"

# 6. Fetch logs to local
scp -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198:/home/ubuntu/minimal-bot/bot_live_*.log docs/
```

## Current EC2 Env

```env
MINIMAL_MIN_BUY_LIMIT=0.10
MINIMAL_MAX_BUY_LIMIT=0.85
MINIMAL_DECISION_MIN_TTE_US=45000000   # 45s no-entry window
MINIMAL_USDC_PER_TRADE=1.01
MINIMAL_ENTRY_SLIPPAGE=0.05
MINIMAL_STOP_LOSS_BPS=0                # disabled
MINIMAL_DECISION_MIN_EDGE=0.05
MINIMAL_PROB_GAMMA_MOVE=0.5
MINIMAL_PROB_SIGMA_FLOOR_USD=2.0
MINIMAL_PROB_SIGMA_SCALE=1.5
MINIMAL_PROB_USE_LEGACY=false
MINIMAL_LOG_LEVEL=INFO
MINIMAL_ENTRY_ORDER_TYPE=FAK
MINIMAL_EXIT_ORDER_TYPE=GTC
MINIMAL_ALLOW_RESTING_ORDERS=true
MINIMAL_MAX_CONCURRENT_POSITIONS=3
MINIMAL_MAX_NOTIONAL_OVERRUN=0.01
MINIMAL_MAX_NOTIONAL_OVERRUN_BPS=0
```

## Fixes Applied (2026-05-05 Session, 3 commits on dev)

### 1. Remove V2 SDK rounding monkeypatch, add signed-body validation
`fast_order_submitter.py`: Deleted `_patch_v2_rounding_for_venue()` which globally mutated SDK `ROUNDING_CONFIG` (forced `amount=2`). Replaced with:
- `canonical_order_params()` — Decimal-first input canonicalization (ceil price for BUY, floor for SELL, ceil/floor size)
- Post-signing signed-body inspection: `_assert_step_aligned(maker, 0.01)`, `_assert_step_aligned(taker, 0.0001)`, `_assert_tick_aligned(implied, 0.01)`, price match check
- Invalid bodies raise `ValueError` locally — no HTTP POST to venue

This fixed the 785 tick-size violations from the 2026-05-04 session. The root cause was the monkeypatch forcing `amount=2` which made `1.13 / 1.57 = 0.719745...` instead of `1.1304 / 1.57 = 0.72`.

### 2. BUY amount precision canonicalization
The V2 SDK default `ROUNDING_CONFIG` has `amount=5,6` for tick sizes 0.001/0.0001 (GitHub #253). This produced `0.48 * 2.10 = 1.008` (3dp maker) → venue rejected with "maker amount supports max 2 decimals."

Added `_ceil_buy_size_for_amount_precision()` (gcd-based, finds smallest size ≥ target where `price × size` is 2dp-aligned), `MAKER_AMOUNT_STEP=0.01`, `TAKER_AMOUNT_STEP=0.0001`. `TemplateArmory._armed` now stores from template actuals.

### 3. Notional-aware BUY sizing
Precision canonicalization previously always ceiled size, which could silently exceed target notional (e.g. $1.01 → $1.34 at price 0.67).

Added `canonical_buy_target_for_notional()` with explicit `max_notional_overrun` tolerance (default $0.01). Chooses between floor and ceil lattice sizes, preferring ceil only when maker ≤ target + overrun. Rejects locally when no size satisfies both venue minimum ($1.01 maker) and notional cap.

### 4. User WSS credential injection + lifecycle logging
`user_channel_ws.py`: App-level 10s PING (protocol pings disabled per official docs). API creds injected from `LiveBot` (already-derived). Sparse lifecycle logs. Non-trade control payloads logged.

### 5. Exit observability
`bot_orchestrator.py`: Sampled exit diagnostics (one line per 5s) at every silent gate. `exit_armory.py`: Logs previously-swallowed failures.

### 6. Race-safe trade-to-submit bind
`order_tracker.py`: `_match_submit_from_trade_msg()` — a WSS trade arriving before its order event can still reconcile into the tracker via `_best_pending_candidate()`.

## Known Issues

1. **Entry fill rate**: FAK fills on 5-min markets with thin books. Most FAK attempts get "no orders found to match."

2. **Signal model uncalibrated**: `sigma_floor=2.0`, `sigma_scale=1.5`, `alpha=0`, `beta=0`, `gamma=0.5` are defaults. Not fitted against historical settlement data.

3. **Sequential exit bottleneck**: `evaluate_exit` single-flight — position B waits behind position A. For $1 positions this is negligible.

4. **HTTP flakiness at startup**: `py_clob_client_v2` "Server disconnected" errors during initial connection. Doesn't cascade into bad orders.

5. **`pkill` unreliable on EC2**: Commands using `pkill` or `$()` substitution may fail with exit 255. Use `kill -9 <PID>` directly.

6. **Latency**: eu-west-1 → Polymarket US RTT ~300-400ms. Moving to us-east-1 is the biggest remaining latency win (infrastructure change, tracked outside code).

## Test Suite

```bash
# Full (1 expected failure: py_clob_client_v2 not installed locally)
python -m pytest tests/ --ignore=tests/test_minimal_live_bot.py -q

# Targeted
python -m pytest tests/test_fast_order_submitter.py tests/test_template_armory.py tests/test_template_armory_single_flight.py tests/test_bot_orchestrator.py tests/test_signal_decision.py -v
```
198 pass, 1 fail (expected: V2 SDK unavailable locally).

## Architecture (Hot Path)

```
Binance SBE tick → BinanceSignalEngine.on_tick_fields() → signal (YES/NO)
  → MinimalBotOrchestrator.on_binance_tick_fields()
    → signal_decision.decide_buy() → BUY/NO_BUY
      → HotPathEngine.on_signal() → FastOrderSubmitter.submit() → POST /order

Exit loop (50ms):
  → MinimalBotOrchestrator.evaluate_exit() → iterates tracker.owned_by_asset
    → exit_policy.decide_exit() → take_profit / expiry_ripcord / hold
      → ExitArmory.arm_exit() → HotPathEngine.on_signal("EXIT") → POST /order
```

## Non-Negotiables

- No full SDK `create_and_post_order()` on signal hot path.
- Pre-signed templates + raw body bytes + fresh L2 headers.
- No subprocess wrappers, no JSON log writes on hot path, no raw event pretty-printing.
- SELL inventory = MATCHED (immediately sellable), floored to 0.01 quantum.
- `POLY_ALLOW_UNTRACKED_SELL=false`.
- `deferExec: false` on every order body.
- `MINIMAL_USDC_PER_TRADE >= 1.01` — $1.00 serializes below venue minimum.
- **Signed-body validation**: every order body inspected for maker≤2dp, taker≤4dp, tick-aligned implied price, price match. No SDK globals mutated.
- **Notional cap**: BUY sizing will not silently exceed target + max_notional_overrun. Armory rejects locally if no valid size.
- Root cause first, fix second. Keep fixes narrow.
- **Every decision grounded in hard log/code evidence, never assumptions.**
