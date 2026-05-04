# Fellow AI Pickup Prompt

You are taking over `C:\Users\niyaz\.repos\poly-buy-sell\minimal` — a low-overhead Polymarket trading bot on EC2 `34.244.40.198`.

Read first:
1. `docs/AI_HANDOFF_2026-05-03.md` (current state, env, known issues)
2. `docs/README.md` (architecture, env vars, hot path rules)
3. `docs/EC2_STANDALONE_RUNBOOK.md` (SSH, deploy cycle, operating model)
4. `.claude/CLAUDE.md` (agent instructions, repo conventions)

## SSH

```bash
ssh -i ".ssh_tmp/poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198
```
Key at `.ssh_tmp/poly-buy-sell.pem`. Bot dir: `/home/ubuntu/minimal-bot/`. Env: `.env.poly`.

**Never use `timeout`** on SSH. Use foreground capture for logs (`python3 -u minimal_live_bot.py 2>&1`). File-based `tee` buffers and loses data. Stop with `pkill -9`.

## Mission

Disciplined live-debug cycles:
1. Deploy current code to EC2, verify env
2. Run bounded live window (10-30 min)
3. Fetch and analyze logs deeply
4. Identify ONE root cause only
5. Implement minimal precise fix
6. Update docs/handoff
7. Rerun

Do not batch speculative fixes. Do not paper over symptoms.

## Non-Negotiables

- No full SDK `create_and_post_order()` on signal — keep pre-signed templates + hot submit
- No subprocess wrappers, no JSON log writes on hot path, no raw event pretty-printing
- SELL inventory = CONFIRMED only, floored to 0.01 quantum
- `POLY_ALLOW_UNTRACKED_SELL=false`
- Treat signal logic as scientific problem, not vibe-based heuristic.
- Root cause first, fix second. Keep fixes narrow.

## Current State Summary

- **10 fixes deployed** (see AI_HANDOFF for details)
- **FAK entries + GTC exits** with `deferExec: false`
- **Max 3 concurrent positions**, multi-position sequential exit
- **Signal model**: momentum-based side, realized vol, gamma*move drift, dual gating, min_edge=0.05
- **Stop-loss disabled** (0 bps = always triggered from spread)
- **"not enough balance" cooldown** for unsettled tokens

## Primary Known Issues

1. **FAK fill rate** on 5-min Polymarket markets is low — venue liquidity constraint
2. **Signal model uncalibrated** — defaults produce conservative estimates
3. **CONFIRMED ≠ Wallet Settlement** — Polymarket timing gap causes exit balance errors

## Output Expected Per Cycle

1. Files changed, env changes on EC2
2. Run log filename
3. Root-cause statement with evidence
4. Why the fix addresses root cause, not symptom
5. Verification results
6. Remaining uncertainty

If the signal model is not scientifically defensible, say so plainly and fail closed.
