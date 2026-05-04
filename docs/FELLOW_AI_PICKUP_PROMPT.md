# Fellow AI Pickup Prompt

You are taking over `C:\Users\niyaz\.repos\poly-buy-sell\minimal` after live EC2 troubleshooting on `34.244.40.198`.

Read first:

1. `docs/AI_HANDOFF_2026-05-03.md`
2. `docs/README.md`
3. `docs/EC2_STANDALONE_RUNBOOK.md`
4. `docs/ec2.env.example`
5. `docs/run_logs/live_20260504_030919.log`
6. `docs/run_logs/live_20260504_034532.log`
7. `docs/run_logs/live_20260504_042013.log`

## Mission

Continue the bot in disciplined live-debug cycles:

1. deploy the current code and correct EC2 env
2. run one bounded live window
3. fetch and analyze logs deeply
4. identify one root cause only
5. implement the minimal precise fix
6. update docs/handoff
7. rerun

Do not batch speculative fixes. Do not paper over symptoms. Do not loosen safety boundaries to make the bot trade more.

## Non-negotiables

- Maintain the low-overhead architecture:
  - no full SDK `create_and_post_order()` on signal
  - keep pre-signed templates and hot submit path
  - no extra subprocess wrappers
  - no noisy stdout/event dumps in the hot path
- Maintain troubleshooting visibility:
  - preserve high-signal logs like `binance_signal_decision`, `binance_signal_status`, `entry_hot_path_result`, `exit_hot_path_result`
  - add instrumentation only when it isolates a concrete uncertainty
  - remove temporary spam once the uncertainty is resolved
- Treat signal logic as a scientific problem, not a vibe-based trading heuristic.

## What Is Already Fixed

- SELLs no longer use merely matched BUY inventory; they require confirmed liquid inventory.
- Sub-`0.01` share dust no longer triggers impossible SELL attempts or blocks exposure reset.
- Live marketable BUY budget below `1.01` USDC now fails closed.

Do not re-open these unless fresh evidence disproves the fixes.

## Current Known State

- EC2 bot dir: `/home/ubuntu/minimal-bot/`
- Env file: `/home/ubuntu/minimal-bot/.env.poly`
- Current intended env baseline:
  - `MINIMAL_MIN_BUY_LIMIT=0.10`
  - `MINIMAL_MAX_BUY_LIMIT=0.85`
  - `MINIMAL_DECISION_MIN_TTE_US=45000000`
  - `MINIMAL_USDC_PER_TRADE=1.01`
- Latest clean validation run:
  - `docs/run_logs/live_20260504_042013.log`
  - no entry submits
  - no exit submits
  - no `balance: 0`
  - no `invalid amounts`
  - no `$0.99` minimum-size rejects
- Important remaining uncertainty:
  - the corrected `1.01` budget has not yet been exercised through a real accepted BUY/SELL cycle in a clean post-fix run
  - intermittent `template_armory_rearm_failed ... Server disconnected` still appears

## Primary Focus Now

### 1. Signal decision / scientific soundness

Interrogate the signal path ruthlessly:

- Is the anchored strike definition scientifically defensible for these 5-minute BTC up/down markets?
- Is the current probability model calibrated enough to justify live trading?
- Are OFI / imbalance / sigma / time-to-expiry being used coherently?
- Is `edge = probability - ask` actually meaningful under observed execution delay and venue microstructure?
- Are the thresholds producing too many weak false positives, or too few opportunities?

Do not accept a rule because it "feels right". Demand evidence from logs and market mechanics.

### 2. End-to-end latency / bottlenecks

Measure each stage:

- Binance tick arrival / anchor buffering
- signal emission
- decision gating
- template availability
- submit latency
- WSS reconciliation
- exit evaluation latency

Look for avoidable overhead or synchronization stalls, but do not destroy observability.

### 3. Repeatable live-debug loop

Prefer bounded windows, for example 10-30 minutes, then stop and inspect:

- all BUY decisions
- all actual submits
- all matched/confirmed trades
- all no-buys by reason
- all exit attempts
- all failed HTTP/signing events

Correlate:

- signal side
- strike
- Binance microprice move
- ask at decision time
- quote age
- TTE
- decision edge / probability
- submit latency
- trade outcome

## Standards For Changes

- Root cause first, fix second.
- Test first when changing behavior.
- Keep fixes narrow.
- Keep buy and sell behavior consistent.
- Update:
  - `docs/AI_HANDOFF_2026-05-03.md`
  - `docs/README.md`
  - `docs/EC2_STANDALONE_RUNBOOK.md`
  when the operational truth changes.

## Output Expected From You

After each cycle, leave:

1. exact files changed
2. exact env changes on EC2
3. exact run log filename
4. concise root-cause statement
5. why the fix is the root-cause fix rather than a symptom patch
6. focused verification results
7. remaining uncertainty

If the signal model is still not scientifically defensible, say so plainly and fail closed rather than forcing more live trading.
