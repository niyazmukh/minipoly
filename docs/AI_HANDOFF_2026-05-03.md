# AI Handoff - 2026-05-03 EC2 Live Troubleshooting

## Current State

- EC2 host: `34.244.40.198`
- EC2 bot dir: `/home/ubuntu/minimal-bot/`
- Env file: `/home/ubuntu/minimal-bot/.env.poly`
- Start: `bash start_live.sh`
- Stop: `kill $(cat live.pid)`
- Current status at handoff: bot stopped; no `python3 -u minimal_live_bot.py` process.
- Remote cleanup was requested after log analysis, but the tool approval layer blocked the destructive SSH cleanup command before execution. Treat EC2 `run_logs/*.log`, `current_log`, `live.pid`, `__pycache__/`, and `.pytest_cache/` as still needing cleanup.

## Root Cause From Last Run

The bad market-close entry was not caused by missing signals or SDK order serialization. It was a live boundary configuration failure:

- EC2 `.env.poly` had `MINIMAL_DECISION_MIN_TTE_US=2000000`, which only blocks entries in the final 2 seconds.
- EC2 `.env.poly` did not define `MINIMAL_MIN_BUY_LIMIT`.
- The bot therefore allowed a BUY at `ask=0.0100` with `tte_us=24859079` (about 24.9 seconds before expiry).
- Intended no-entry policy is 45 seconds, so use `MINIMAL_DECISION_MIN_TTE_US=45000000`.

The deployed code now fails closed unless `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are explicitly present and coherent. On the current EC2 env, import-only validation fails with:

```text
RuntimeError: MINIMAL_MIN_BUY_LIMIT is required for live entry boundary enforcement.
```

Do not restart live trading until `.env.poly` is corrected.

## Key EC2 Log Evidence

Relevant logs on EC2:

- `run_logs/live_20260503_163155.log`
- `run_logs/live_20260503_165935.log`
- `run_logs/live_20260503_170459.log`
- `run_logs/live_20260503_170834.log`

Observed progression:

- V2 signing fixed `order_version_mismatch`; a YES buy matched in `live_20260503_163155.log`:
  - order `0x6b91f2ca031a714e9f5a9ae94c92b0d10cde1888ade3f4dfac21728882d65c2e`
  - `takingAmount='2.5'`, `makingAmount='2'`, status `matched`
- V2 amount precision was initially wrong for some NO entries:
  - `invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals, taker amount a max of 4 decimals`
  - fixed by patching V2 rounding before template signing.
- Reusing a pre-signed FAK template caused one duplicate-order error in `live_20260503_170459.log`:
  - `order ... is invalid. Duplicated.`
  - fixed by retiring armed templates after every actual POST attempt.
- Final bad entry in `live_20260503_170834.log`:
  - signal decision: `action=BUY reason=edge_ok side=NO ask=0.0100 tte_us=24859079 edge=0.9800`
  - matched order: `0x0993997d2877fed19ca89a1cf7fd051ee76b88d14544d4fd5c9b408cd412f590`
  - response: `takingAmount='200'`, `makingAmount='2'`, status `matched`
  - this proves the missing/unsafe boundary config was the root cause.

## Changes Made

Runtime:

- `minimal_live_bot.py`
  - uses `py_clob_client_v2` for background template signing.
  - keeps hot submission through `FastOrderSubmitter`.
  - supports dry-run mode through `MINIMAL_DRY_RUN_ORDERS=true`.
  - fails closed unless `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are explicit and sane.
  - loads `.env.poly` before logging config so `MINIMAL_LOG_LEVEL` works.
- `fast_order_submitter.py`
  - serializes V2 signed orders with `order_to_json_v2`.
  - patches V2 rounding config to venue-compatible two-decimal cash amounts before signing templates.
- `template_armory.py`
  - enforces configured min/max buy limits before signing.
  - can retire stale armed state after a submit attempt.
- `hot_path_engine.py`
  - treats signed templates as single-use and disarms after any POST attempt.
- `bot_orchestrator.py`
  - logs `binance_signal_decision`, periodic `binance_signal_status`, `entry_hot_path_result`, and `exit_hot_path_result`.
  - retires entry/exit armory state after actual submit attempts.
  - disarms entries when the market is inside the no-entry TTE window.
- `exit_armory.py`
  - can retire a prepared exit template after a submit attempt.
- `signal_decision.py`
  - supports explicit `min_ask` so below-floor entries are rejected by decision logic.

Docs/tests:

- `docs/ec2.env.example` now documents `MINIMAL_MIN_BUY_LIMIT`, `MINIMAL_MAX_BUY_LIMIT`, and 45-second `MINIMAL_DECISION_MIN_TTE_US=45000000`.
- `docs/README.md`, `docs/EC2_STANDALONE_RUNBOOK.md`, and `docs/MINIMAL_IMPLEMENTATION_AUDIT.md` were updated to reflect the current runtime.
- Focused local verification after boundary changes: `74 passed`.

## Operational Notes

- Do not use full SDK `create_and_post_order()` on signal. It adds hot-path overhead and would undo the pre-signed-template design.
- Keep `POLY_ALLOW_UNTRACKED_SELL=false`.
- Keep `MINIMAL_ALLOW_RESTING_ORDERS=false` unless deliberately testing GTC/GTD outside the autonomous runtime.
- If enabling live again, set at minimum:

```env
MINIMAL_MIN_BUY_LIMIT=0.10
MINIMAL_MAX_BUY_LIMIT=0.85
MINIMAL_DECISION_MIN_TTE_US=45000000
```

Choose the actual price floor deliberately; `0.10` above is an example matching the current docs, not a universal strategy claim.

## Pending Cleanup Command

Run only after confirming the bot is stopped:

```bash
cd /home/ubuntu/minimal-bot
ps -eo pid,cmd | grep "[p]ython3 -u minimal_live_bot.py" && exit 1
sed -i '/^MINIMAL_MIN_BUY_LIMIT=/d;/^MINIMAL_MAX_BUY_LIMIT=/d;/^MINIMAL_DECISION_MIN_TTE_US=/d;/^MINIMAL_DEBUG_USER_CHANNEL=/d;/^MINIMAL_LOG_LEVEL=/d' .env.poly
printf '\nMINIMAL_MIN_BUY_LIMIT=0.10\nMINIMAL_MAX_BUY_LIMIT=0.85\nMINIMAL_DECISION_MIN_TTE_US=45000000\nMINIMAL_LOG_LEVEL=WARNING\n' >> .env.poly
find . -type d -name __pycache__ -prune -exec rm -rf {} +
rm -rf .pytest_cache
rm -f live.pid current_log
rm -f run_logs/*.log
```
