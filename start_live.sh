#!/bin/bash
cd /home/ubuntu/minimal-bot
LOGF="run_logs/live_$(date +%Y%m%d_%H%M%S).log"
echo "$LOGF" > current_log
nohup python3 -u minimal_live_bot.py > "$LOGF" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > live.pid
echo "started pid=$BOT_PID log=$LOGF"
