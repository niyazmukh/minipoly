# EC2 Standalone Runbook

Target: one Python asyncio process on Ubuntu EC2.

## SSH Access

From Windows PowerShell in repo root:

```powershell
$KEY = "C:\Users\niyaz\.repos\poly-buy-sell\minimal\.ssh_tmp\poly-buy-sell.pem"
$HOST = "ubuntu@34.244.40.198"
$REMOTE = "/home/ubuntu/minimal-bot"
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "pwd"
```

- EC2 host: `34.244.40.198` in `eu-west-1`.
- EC2 user: `ubuntu`.
- Bot dir: `/home/ubuntu/minimal-bot/`.
- Env file: `/home/ubuntu/minimal-bot/.env.poly`.
- Key: `C:\Users\niyaz\.repos\poly-buy-sell\minimal\.ssh_tmp\poly-buy-sell.pem`.
- Never use `timeout` on SSH commands. It kills the SSH session and can drop output.
- Do not kill every `python3` on EC2. Stop only PIDs whose args contain `minimal_live_bot.py`.
- `pkill -f 'python3 -u minimal_live_bot.py'` can misparse because of `-u`; use explicit PID extraction below.
- File-backed logging to `run_logs/*.log` is the preferred live-run capture. Avoid `tee` for long captures because it buffers.

If OpenSSH rejects the key with `UNPROTECTED PRIVATE KEY FILE`, `bad permissions`, or `Load key ... Permission denied`, fix Windows ACLs for the account that will run `ssh`:

```powershell
icacls ".ssh_tmp\poly-buy-sell.pem" /inheritance:r /grant:r "$env:USERNAME:R"
```

Codex note: in the 2026-05-06 run, escalated SSH worked with owner-only `niyaz:R`. Sandbox-local SSH used a different account and was not the working path.

## Deploy And Run

PowerShell variables:

```powershell
$KEY = "C:\Users\niyaz\.repos\poly-buy-sell\minimal\.ssh_tmp\poly-buy-sell.pem"
$HOST = "ubuntu@34.244.40.198"
$REMOTE = "/home/ubuntu/minimal-bot"
```

1. Inspect process state:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "ps -eo pid,ppid,etime,stat,args | grep '[m]inimal_live_bot.py' || true"
```

2. Stop only the minimal bot:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "pids=\$(ps -eo pid=,args= | awk '/minimal_live_bot[.]py/{print \$1}'); if [ -n \"\$pids\" ]; then kill -9 \$pids; fi"
```

3. Wipe only runtime bot files. Keep `venv/`. Do not deploy docs, tests, or artifacts:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "cd $REMOTE && find . -maxdepth 1 -type f -name '*.py' -delete && rm -f .env.poly live.pid && rm -rf __pycache__ && mkdir -p run_logs && rm -f run_logs/*.log"
```

4. Deploy root bot files and env only:

```powershell
scp -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL *.py .env.poly "${HOST}:${REMOTE}/"
```

5. Verify deployed file count and env:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "cd $REMOTE && printf 'py_count=' && find . -maxdepth 1 -type f -name '*.py' | wc -l && grep -E 'MINIMAL_MIN_BUY_LIMIT|MINIMAL_MAX_BUY_LIMIT|MINIMAL_ENTRY_SLIPPAGE|MINIMAL_TAKE_PROFIT_BPS|MINIMAL_EXIT_FAK_ATTEMPTS' .env.poly && sha256sum .env.poly"
Get-FileHash -Algorithm SHA256 .env.poly
```

6. Start with full EC2 log capture and write `live.pid`:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST 'cd /home/ubuntu/minimal-bot; mkdir -p run_logs; log="run_logs/bot_live_$(date -u +%Y%m%d_%H%M%S).log"; setsid python3 -u minimal_live_bot.py > "$log" 2>&1 < /dev/null & pid=$!; echo $pid > live.pid; echo LOG=$log PID=$pid'
```

7. Confirm it is running:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "cd $REMOTE && cat live.pid && ps -p \$(cat live.pid) -o pid,ppid,etime,stat,args"
```

If a previous start command timed out but the bot is running, repair `live.pid` from the actual Python process:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST 'cd /home/ubuntu/minimal-bot; pid=$(ps -C python3 -o pid= | tail -n 1); echo $pid > live.pid; echo PIDFILE=$(cat live.pid); latest=$(ls -t run_logs/*.log | head -1); echo LOG=$latest; tail -n 80 $latest'
```

8. Monitor without killing the bot:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "cd $REMOTE && tail -n 120 -f \$(ls -t run_logs/*.log | head -1)"
```

9. Stop only when explicitly requested:

```powershell
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "cd $REMOTE && if [ -f live.pid ]; then kill -9 \$(cat live.pid) 2>/dev/null || true; fi; pids=\$(ps -eo pid=,args= | awk '/minimal_live_bot[.]py/{print \$1}'); if [ -n \"\$pids\" ]; then kill -9 \$pids; fi"
```

10. Fetch full latest log:

```powershell
$LOCAL_LOG = "docs\bot_live_$(Get-Date -Format yyyyMMdd_HHmmss)_ec2.log"
$REMOTE_LOG = ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL $HOST "cd $REMOTE && ls -t run_logs/*.log | head -1"
scp -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL "${HOST}:${REMOTE_LOG}" $LOCAL_LOG
python docs\analyze_bot_logs.py $LOCAL_LOG --context 3
```

## Current EC2 Env Baseline

```env
MINIMAL_MIN_BUY_LIMIT=0.25
MINIMAL_MAX_BUY_LIMIT=0.70
MINIMAL_DECISION_MIN_TTE_US=45000000
MINIMAL_USDC_PER_TRADE=1.01
MINIMAL_ENTRY_SLIPPAGE=0.03
MINIMAL_TAKE_PROFIT_BPS=1000
MINIMAL_DECISION_MIN_EDGE=0.05
MINIMAL_PROB_GAMMA_MOVE=0.5
MINIMAL_PROB_SIGMA_FLOOR_USD=2.0
MINIMAL_PROB_SIGMA_SCALE=1.5
MINIMAL_LOG_LEVEL=INFO
MINIMAL_EXIT_FAK_ATTEMPTS=3
MINIMAL_MAX_CONCURRENT_POSITIONS=3
MINIMAL_MAX_NOTIONAL_OVERRUN=0.01
MINIMAL_MAX_NOTIONAL_OVERRUN_BPS=0
```

## Operating Model

- Entry orders are FAK.
- Exit orders are FAK with multi-attempt bursts (`MINIMAL_EXIT_FAK_ATTEMPTS=3`).
- No resting orders in the autonomous runtime.
- `deferExec: false` is set on every order body.
- Max positions means concurrent asset positions, not dollars or shares.
- Sell inventory is WSS/user-channel tracker inventory floored to the tradable 0.01 share quantum.
- Immediate take-profit burst uses the matched BUY HTTP response amount first, then WSS inventory updates normal exit sizing.
- `MINIMAL_USDC_PER_TRADE` must stay at least `1.01`; `1.00` can serialize below venue minimum after rounding.
- BUY size selection must remain notional-aware. It must not silently round up beyond `target_usdc + max_notional_overrun`.
- Signed-body validation in `prepare_template()` is the final safety net for maker/taker precision and implied price tick/equality.
- No SDK-global monkeypatching.
- No docs/tests/artifacts on EC2 for live runs.
