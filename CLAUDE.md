# Minimal Polymarket Bot — Agent Instructions

## SSH to EC2

```
ssh -i ".ssh_tmp\poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198
```

Key is at `.ssh_tmp/poly-buy-sell.pem` (repo-relative). EC2 host: `34.244.40.198`. Bot dir: `/home/ubuntu/minimal-bot/`. Env: `/home/ubuntu/minimal-bot/.env.poly`.

**Never use `timeout` on SSH commands** — it kills the SSH session and drops output. For foreground captures use Bash with `run_in_background: true` and a generous timeout (900s for a 15-min window). For file-based logging, `tee` buffers aggressively; prefer foreground SSH capture.

## Deploy → Run → Inspect Cycle

```
1. scp changed .py files to ubuntu@34.244.40.198:/home/ubuntu/minimal-bot/
2. If env changed: edit .env.poly directly on EC2
3. Verify: grep MINIMAL_PROB_* .env.poly
4. Kill old bot: pkill -f 'python3 -u minimal_live_bot.py'
5. Clean: rm -f run_logs/*.log live.pid; find . -type d -name __pycache__ -exec rm -rf {} +
6. Start: python3 -u minimal_live_bot.py 2>&1 (captured via foreground SSH)
7. Run 10-15 min bounded window
8. Kill: pkill -f 'python3 -u minimal_live_bot.py'
9. Analyze log: grep binance_signal_decision, grep entry_hot_path, grep exit_hot_path
```

## Architecture (Hot Path)

- `minimal_live_bot.py` — entrypoint, config wiring, async supervision
- `bot_orchestrator.py` — routes market/user/binance events, anchors strike, logs decisions
- `binance_signal_engine.py` — converts Binance tick stream into directional signals + sigma_px
- `signal_decision.py` — Brownian barrier-cross probability → BUY/NO_BUY decision
- `template_armory.py` — pre-signs entry templates off hot path (single-flight)
- `hot_path_engine.py` — guards (quote age, exposure, one-cycle) then submits
- `fast_order_submitter.py` — raw HTTP POST/DELETE with L2 auth headers
- `order_tracker.py` — user-WSS confirmed inventory, sellable vs owned, exposure tracking
- `exit_policy.py` / `exit_armory.py` — take-profit / stop-loss / expiry exits
- `runtime_state.py` — active market, quotes, position state
- `runtime_wiring.py` — builds and connects runtime objects

## Key Design Rules

- **No full SDK `create_and_post_order()` on signal** — keep pre-signed templates + hot submit
- **No subprocess wrappers, no JSON log writes on hot path, no raw event pretty-printing**
- **Sell inventory = CONFIRMED only** (not just MATCHED), floored to 0.01 share quantum
- **Buy cycle locked until exposure flat** (one unsold position at a time)
- **FAK orders only** unless `MINIMAL_ALLOW_RESTING_ORDERS=true`
- **`MINIMAL_USDC_PER_TRADE >= 1.01`** — 1.00 serializes below venue $1 minimum
- **Startup fails closed** unless `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are set

## Key Env Vars

| Var | Current | Purpose |
|-----|---------|---------|
| `MINIMAL_USDC_PER_TRADE` | 1.01 | Marketable BUY budget |
| `MINIMAL_MIN_BUY_LIMIT` | 0.10 | Min entry price floor |
| `MINIMAL_MAX_BUY_LIMIT` | 0.85 | Max entry price (max_ask) |
| `MINIMAL_DECISION_MIN_TTE_US` | 45000000 | No-entry window (45s) |
| `MINIMAL_PROB_SIGMA_FLOOR_USD` | 2.0 | Volatility floor for prob model |
| `MINIMAL_PROB_SIGMA_SCALE` | 1.5 | Volatility scale multiplier |
| `MINIMAL_PROB_GAMMA_MOVE` | 0.5 | Weight of momentum in drift |
| `MINIMAL_PROB_MIN_PROB` | 0.55 | Hard probability floor |
| `MINIMAL_PROB_USE_LEGACY` | false | Use Brownian model (not legacy heuristic) |
| `POLY_ALLOW_LIVE_ORDERS` | true | Required for live trading |
| `MINIMAL_REQUIRE_CALIBRATED_MODEL` | false | Set true when model is fitted |

## Testing

```bash
# Full suite (exclude import-failing files)
python -m pytest tests/ --ignore=tests/test_minimal_live_bot.py --ignore=tests/test_fast_order_submitter.py -q

# Targeted
python -m pytest tests/test_binance_signal_engine.py tests/test_signal_decision.py tests/test_bot_orchestrator.py -v
```

Expect `test_fast_order_submitter.py::test_build_order_body` to fail locally (needs `py_clob_client_v2` which is only on EC2).

## Subagent Usage

**Do NOT spawn subagents for:**
- Editing code (single-file changes are trivial)
- Running tests (single bash command)
- SSH/EC2 operations
- Reading known file paths

**DO spawn `general-purpose` subagents with model `haiku` for:**
- Searching across many files for a pattern when you're unsure where it lives
- Grep across 10+ files in parallel
- Finding where a symbol is defined across the codebase (use `Explore` agent)

**DO spawn `Explore` subagents (`model: haiku`) for:**
- "Where is X defined?"
- "Which files reference Y?"
- Broad codebase exploration

**Rule of thumb:** If it takes more than 3 sequential Grep/Glob calls, spawn an Explore agent. If you know the file path, read it directly. Never spawn a subagent to do work you already know how to do.

## Git

- Commit messages: concise, focus on WHY. Use `Co-Authored-By: Claude Code <noreply@anthropic.com>` trailer.
- Never amend commits unless explicitly asked.
- Never force push to main.
- Prefer `git add <specific files>` over `git add -A`.
