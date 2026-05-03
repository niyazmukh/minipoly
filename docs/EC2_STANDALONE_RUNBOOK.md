# EC2 Standalone Runbook

Target: one Python asyncio process on Ubuntu EC2.

## Files

- Runtime entrypoint: `minimal_live_bot.py`
- Current EC2 bot directory used in the 2026-05-03 live run: `/home/ubuntu/minimal-bot/`
- Environment file on EC2: `/home/ubuntu/minimal-bot/.env.poly`
- Example environment template: `docs/ec2.env.example`

## Deploy

From your machine, copy changed runtime files to the instance. Keep the filled env file only on the EC2 host.

```bash
scp docs/ec2.env.example ubuntu@EC2_HOST:/home/ubuntu/minimal-bot/.env.poly
```

Then edit `/home/ubuntu/minimal-bot/.env.poly` on EC2 and replace placeholders.

## Run

```bash
cd /home/ubuntu/minimal-bot
python3 -m pip install --user --break-system-packages -r requirements.txt
bash start_live.sh
```

## systemd

Create `/etc/systemd/system/minimal-poly-bot.service`:

```ini
[Unit]
Description=Minimal Polymarket Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/minimal-bot
EnvironmentFile=/home/ubuntu/minimal-bot/.env.poly
ExecStart=/usr/bin/python3 -u minimal_live_bot.py
Restart=always
RestartSec=2
KillSignal=SIGINT
TimeoutStopSec=15
User=ubuntu

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable minimal-poly-bot
sudo systemctl start minimal-poly-bot
sudo journalctl -u minimal-poly-bot -f
```

## Operating Model

- Production live trading starts only when `POLY_ALLOW_LIVE_ORDERS=true`.
- Non-transactional smoke tests use `MINIMAL_DRY_RUN_ORDERS=true`; dry-run mode builds/signs templates and consumes live feeds, but the submitter never POSTs `/order` or DELETEs `/orders`.
- Live startup fails closed unless `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are explicitly set and coherent. For the intended no-entry window, set `MINIMAL_DECISION_MIN_TTE_US=45000000` (45 seconds).
- Historical startup positions are intentionally ignored. The bot only tracks positions/orders it created in the current process run.
- Startup fails closed on existing open CLOB orders unless `MINIMAL_ALLOW_DIRTY_START=true`; an old resting order can still fill during the new process and create inventory the current-run tracker did not buy.
- The hot path enforces one current-market buy cycle at a time: a filled buy blocks further same-market buys until the bot's own current-run position is sold and local exposure is flat.
- Entry/exit order types default to `FAK`. Resting `GTC/GTD` orders require `MINIMAL_ALLOW_RESTING_ORDERS=true`.
- Order templates are signed with `py_clob_client_v2` off the hot path. Do not replace the hot path with SDK `create_and_post_order()`; keep prebuilt body bytes plus fresh L2 headers.
- Live startup fails closed by default unless `MINIMAL_SIGNAL_MODEL_PATH` points to a valid calibrated signal model file. The file is **parsed and applied** to `SignalDecisionConfig` and `BinanceSignalConfig`; the bot refuses to start if the schema is wrong, the file is malformed, or the file specifies no overrides. See `minimal/docs/CALIBRATION.md` for the calibration protocol the file must come out of. Set `MINIMAL_REQUIRE_CALIBRATED_MODEL=false` only for cold plumbing tests.
- A POST `/order` transport failure (timeout, reset, 5xx without a definitive error body) is treated as `submit_unknown` rather than `failed`. The bot keeps the pending submit eligible for WSS reconciliation so a server-accepted order does not become invisible inventory. UNKNOWN BUYs hold the buy-cycle lock; UNKNOWN SELLs provisionally reserve inventory until WSS confirms or rejects.
- A `market_resolved` event for the active market drops local tracker entries for that market's tokens — Polymarket settles the position; the bot must not later try to liquidate it.
- Stale order cancellation and shutdown order cancellation run outside the signal hot path.
