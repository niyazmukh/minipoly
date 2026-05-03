# EC2 Standalone Runbook

Target: one Python asyncio process on Ubuntu EC2.

## Files

- Runtime entrypoint: `minimal/minimal_live_bot.py`
- Environment file on EC2: `minimal/.env.poly`
- Example environment template: `minimal/docs/ec2.env.example`

## Deploy

From your machine, copy the repo and a filled env file to the instance. Keep the filled env file only on the EC2 host.

```bash
scp minimal/docs/ec2.env.example ubuntu@EC2_HOST:/home/ubuntu/poly-buy-sell/minimal/.env.poly
```

Then edit `/home/ubuntu/poly-buy-sell/minimal/.env.poly` on EC2 and replace placeholders.

## Run

```bash
cd /home/ubuntu/poly-buy-sell
python -m venv .venv
. .venv/bin/activate
python -m pip install -r minimal/requirements.txt
python minimal/minimal_live_bot.py
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
WorkingDirectory=/home/ubuntu/poly-buy-sell
EnvironmentFile=/home/ubuntu/poly-buy-sell/minimal/.env.poly
ExecStart=/home/ubuntu/poly-buy-sell/.venv/bin/python minimal/minimal_live_bot.py
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

- The bot starts only when `POLY_ALLOW_LIVE_ORDERS=true`.
- Historical startup positions are intentionally ignored. The bot only tracks positions/orders it created in the current process run.
- The hot path enforces one current-market buy cycle at a time: a filled buy blocks further same-market buys until the bot's own current-run position is sold and local exposure is flat.
- Entry/exit order types default to `FAK`. Resting `GTC/GTD` orders require `MINIMAL_ALLOW_RESTING_ORDERS=true`.
- Live startup fails closed by default unless `MINIMAL_SIGNAL_MODEL_PATH` points to a valid calibrated signal model file. The file is **parsed and applied** to `SignalDecisionConfig` and `BinanceSignalConfig`; the bot refuses to start if the schema is wrong, the file is malformed, or the file specifies no overrides. See `minimal/docs/CALIBRATION.md` for the calibration protocol the file must come out of. Set `MINIMAL_REQUIRE_CALIBRATED_MODEL=false` only for cold plumbing tests.
- A POST `/order` transport failure (timeout, reset, 5xx without a definitive error body) is treated as `submit_unknown` rather than `failed`. The bot keeps the pending submit eligible for WSS reconciliation so a server-accepted order does not become invisible inventory. UNKNOWN BUYs hold the buy-cycle lock; UNKNOWN SELLs provisionally reserve inventory until WSS confirms or rejects.
- A `market_resolved` event for the active market drops local tracker entries for that market's tokens — Polymarket settles the position; the bot must not later try to liquidate it.
- Stale order cancellation and shutdown order cancellation run outside the signal hot path.
