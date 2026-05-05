# EC2 Standalone Runbook

Target: one Python asyncio process on Ubuntu EC2.

## SSH Access

```bash
# Key location (repo-relative):
ssh -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198
```

- EC2 host: `34.244.40.198` (eu-west-1)
- Bot dir: `/home/ubuntu/minimal-bot/`
- Env file: `/home/ubuntu/minimal-bot/.env.poly`
- **Never use `timeout` on SSH commands** — it kills the session. Use Bash `run_in_background` with generous timeout instead.
- **Foreground SSH capture** is the only reliable way to get logs. File-based logging with `tee` buffers heavily.
- **Use `kill -9 <PID>`** to stop the bot (find PID with `ps aux | grep minimal_live`). `pkill` may fail with exit 255 on this EC2 instance even for simple patterns.

## Deploy Cycle (Working Pattern)

```bash
# 1. Deploy all changed .py files
scp -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no \
  *.py ubuntu@34.244.40.198:/home/ubuntu/minimal-bot/

# 2. Update env if needed
ssh ... "sed -i 's/OLD=.*/NEW=VALUE/' /home/ubuntu/minimal-bot/.env.poly"

# 3. Verify env
ssh ... "grep -E 'MINIMAL_.*=|POLY_.*=' /home/ubuntu/minimal-bot/.env.poly"

# 4. Kill old bot (find PID first)
ssh ... "ps aux | grep minimal_live | grep -v grep"
ssh ... "kill -9 <PID>"

# 5. Start bot in background on EC2
ssh ... "cd /home/ubuntu/minimal-bot && rm -f run_logs/*.log live.pid && \
  nohup python3 -u minimal_live_bot.py > /tmp/bot_live.log 2>&1 & echo PID=\$!"

# 6. Stream logs (separate SSH session or Monitor)
ssh ... "tail -f /tmp/bot_live.log"

# 7. Fetch logs to local
scp -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no \
  ubuntu@34.244.40.198:/tmp/bot_live.log docs/bot_live_$(date +%Y%m%d_%H%M%S).log
```

## Current EC2 Env Baseline

```env
MINIMAL_MIN_BUY_LIMIT=0.10
MINIMAL_MAX_BUY_LIMIT=0.85
MINIMAL_DECISION_MIN_TTE_US=45000000
MINIMAL_USDC_PER_TRADE=1.01
MINIMAL_ENTRY_SLIPPAGE=0.05
MINIMAL_STOP_LOSS_BPS=0
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

## Operating Model

- **Entry orders: FAK** — speed matters for capturing edge before it vanishes.
- **Exit orders: GTC** — sits on book waiting for take-profit target. Skipped taker costs.
- **`MINIMAL_ALLOW_RESTING_ORDERS=true`** required for GTC exits.
- **`deferExec: false`** set on every order body — explicit opt-out of Polymarket deferral.
- **Max 3 concurrent positions** across all market scopes — prevents runaway entries.
- **Multi-position exit**: `evaluate_exit` iterates all tracker positions, not just `state.position`. Each position is evaluated independently, but only ONE sell is submitted per tick (sequential single-flight exit).
- **Stop-loss disabled** (`MINIMAL_STOP_LOSS_BPS=0`). Code skips the check when bps ≤ 0 — previously 0 bps meant "stop at entry price" which triggered from bid-ask spread.
- **"not enough balance" cooldown**: exits that hit venue balance errors are suppressed for 2s to let Polymarket settlement complete. Polymarket confirms trades (user channel CONFIRMED) before tokens settle in wallet.
- **SELL inventory = MATCHED** (immediately sellable, no CONFIRMED wait), floored to 0.01 share quantum.
- **No full SDK `create_and_post_order()` on hot path** — pre-signed templates + fresh L2 headers.
- **45-second no-entry window** enforced at template disarm AND decision gate layers.
- **`MINIMAL_USDC_PER_TRADE >= 1.01`** — $1.00 serializes below venue minimum after rounding.
- **Notional cap** — BUY sizes chosen by `canonical_buy_target_for_notional()` will not silently exceed `target_usdc + max_notional_overrun`. If no valid size satisfies both the venue minimum ($1.01 maker) and the notional cap, the armory rejects locally. No 400 round-trip.
- **Signed-body validation** — `prepare_template()` inspects the serialized signed body for maker ≤2dp, taker ≤4dp, tick-aligned implied price, and price match before returning. No SDK rounding monkeypatch.
- Stale GTC orders cancelled after 2s via `cancel_stale_orders` loop.
- Shutdown cancels live orders before closing HTTP clients.
