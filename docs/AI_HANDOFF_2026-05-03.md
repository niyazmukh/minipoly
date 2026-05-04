# AI Handoff — 2026-05-04 Live Debug Session

**Final state**: Bot stopped. All changes committed. Ready for next cycle.

## Quick Start

Read these in order:
1. This file (current state)
2. `docs/README.md` (architecture, env vars, hot path rules)
3. `docs/EC2_STANDALONE_RUNBOOK.md` (SSH, deploy, operating model)
4. `.claude/CLAUDE.md` (agent instructions, repo conventions)

## SSH

```bash
ssh -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198
```
Key: `.ssh_tmp/poly-buy-sell.pem` (repo-relative). Host: `34.244.40.198`. Bot dir: `/home/ubuntu/minimal-bot/`. Env: `/home/ubuntu/minimal-bot/.env.poly`.

**Never use `timeout`** on SSH commands — it kills sessions and drops logs. Use foreground SSH capture for logs. File-based logging with `tee` buffers heavily and loses data. Use `pkill -9` (SIGKILL) to stop the bot.

## Current EC2 Env

```env
MINIMAL_MIN_BUY_LIMIT=0.10
MINIMAL_MAX_BUY_LIMIT=0.85
MINIMAL_DECISION_MIN_TTE_US=45000000   # 45s no-entry window
MINIMAL_USDC_PER_TRADE=1.01
MINIMAL_ENTRY_SLIPPAGE=0.05
MINIMAL_STOP_LOSS_BPS=0                # disabled (0 bps = "stop at entry" = always triggered from spread)
MINIMAL_DECISION_MIN_EDGE=0.05         # universal edge floor
MINIMAL_PROB_GAMMA_MOVE=0.5
MINIMAL_PROB_SIGMA_FLOOR_USD=2.0
MINIMAL_PROB_SIGMA_SCALE=1.5
MINIMAL_PROB_USE_LEGACY=false
MINIMAL_LOG_LEVEL=INFO
MINIMAL_ENTRY_ORDER_TYPE=FAK           # entries need speed
MINIMAL_EXIT_ORDER_TYPE=GTC            # exits can rest on book
MINIMAL_ALLOW_RESTING_ORDERS=true      # required for GTC exits
MINIMAL_MAX_CONCURRENT_POSITIONS=3
```

## Architecture Summary (Hot Path)

```
Binance SBE tick → BinanceSignalEngine.on_tick_fields() → signal (YES/NO)
  → MinimalBotOrchestrator.on_binance_tick_fields()
    → signal_decision.decide_buy() → BUY/NO_BUY
      → HotPathEngine.on_signal() → FastOrderSubmitter.submit() → POST /order

Exit loop (50ms):
  → MinimalBotOrchestrator.evaluate_exit() → iterates ALL tracker positions
    → exit_policy.decide_exit() → take_profit / expiry_ripcord / hold
      → ExitArmory.arm_exit() → HotPathEngine.on_signal("EXIT") → POST /order
```

Key files:
- `minimal_live_bot.py` — entrypoint, config wiring, asyncio supervision
- `bot_orchestrator.py` — routes events, anchors strike, logs decisions, evaluate_exit
- `binance_signal_engine.py` — tick→signal conversion, sigma_px (realized vol), _maybe_signal
- `signal_decision.py` — Brownian barrier-cross probability + dual gating
- `template_armory.py` — pre-signs entry templates (single-flight, off hot path)
- `hot_path_engine.py` — guard checks, submit, multi-position, max_concurrent
- `fast_order_submitter.py` — raw POST /order with L2 auth, V2 rounding patch, deferExec
- `exit_policy.py` — take_profit/stop_loss/expiry decisions
- `exit_armory.py` — pre-signs exit templates
- `order_tracker.py` — user-WSS confirmed inventory, sellable, exposure, positions
- `runtime_state.py` — active market, quotes, position state
- `polymarket_market_feed.py` — book event parsing (best bid/ask extraction)

## Signal Pipeline (All Fixes Applied)

### Signal Generation (`binance_signal_engine.py`)
- **Momentum-based side**: Side determined by `move` direction only, NOT by whether microprice is above/below strike. Allows signals when tokens are cheap (<0.50).
- **Realized volatility**: `sigma_px = sqrt(Σ dp² / total_dt_s)` — stddev of consecutive microprice returns, not levels. Fixes P→0.5 collapse during trending.
- **OFI/imbalance filters**: YES requires positive OFI and imbalance; NO requires negative.

### Decision (`signal_decision.py`)
- **Brownian drift**: `drift = (microprice - strike) + gamma*move + alpha*OFI + beta*imbalance*sigma`
- **Dual gating**: Cheap tokens (ask < 0.50) use edge-only gate. Expensive tokens (ask ≥ 0.50) also require P > min_prob (0.55).
- **Universal min_edge=0.05**: No trade fires with less than 5% expected profit per share.
- **sigma_floor=2.0**: Per-second vol floor. Lower than before (was 5.0).

### Execution
- **FAK entries**: Pre-signed templates, 280-400ms submit latency, low fill rate on 5-min markets.
- **GTC exits**: Rest on book, `deferExec: false`, cancelled after 2s staleness.
- **Slippage=0.05**: `buy_limit = ceil_to_tick(ask + 0.05, tick)`. Crosses spread for better fills.
- **Multi-position**: Max 3 concurrent across all scopes. Exit loop iterates all positions.
- **"not enough balance" cooldown**: 2s suppression per asset when venue rejects due to unsettled tokens.

## Known Issues (Unresolved)

1. **FAK fill rate on 5-min markets**: Polymarket 5-minute BTC markets have thin resting order books. FAK demands immediate match — most attempts get "no orders found to match." This is a venue liquidity constraint, not a code bug.

2. **Signal model uncalibrated**: `sigma_floor=2.0`, `sigma_scale=1.5`, `alpha=0`, `beta=0`, `gamma=0.5` are defaults. Without fitting against historical settlement data (CALIBRATION.md: 30 days, 20k events), we can't claim positive expected edge.

3. **Sequential exit bottleneck**: `evaluate_exit` submits one sell at a time (single-flight). Position B waits ~300ms behind position A. For $1 positions this is negligible.

4. **HTTP flakiness at startup**: `template_armory_rearm_failed ... Server disconnected` from `py_clob_client_v2`. Doesn't cascade into bad orders.

5. **Polymarket CONFIRMED ≠ Wallet Settlement**: User channel confirms trades before tokens settle. Exit "not enough balance" errors happen during this gap. The 2s cooldown mitigates but doesn't eliminate.

## Test Suite

```bash
# Full (2 expected import failures — py_clob_client_v2 not installed locally)
python -m pytest tests/ --ignore=tests/test_minimal_live_bot.py --ignore=tests/test_fast_order_submitter.py -q

# Targeted
python -m pytest tests/test_signal_decision.py tests/test_binance_signal_engine.py tests/test_hot_path_engine.py -v
```
154 pass, 2 fail (expected: V2 SDK + live bot imports unavailable locally).

## Non-Negotiables (Preserved From Original)

- No full SDK `create_and_post_order()` on signal hot path.
- Pre-signed templates + raw body bytes + fresh L2 headers.
- No subprocess wrappers, no JSON log writes on hot path, no raw event pretty-printing.
- SELL inventory = CONFIRMED only, floored to 0.01 quantum.
- `POLY_ALLOW_UNTRACKED_SELL=false`.
- Treat signal logic as scientific problem, not vibe-based heuristic.
- Root cause first, fix second. Keep fixes narrow.
