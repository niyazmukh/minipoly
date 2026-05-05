# Minimal Polymarket Bot — Agent Instructions

## graphify Knowledge Graph (v6)

**Before answering codebase questions, consult the graph:**
- `graphify-out/GRAPH_REPORT.md` — god nodes, surprising connections, community map, suggested questions
- `graphify-out/graph.html` — interactive visualization (open in browser, no server needed)
- `graphify-out/graph.json` — raw graph data (queryable, GraphRAG-ready)

### Common Commands

| Command | Purpose |
|---------|---------|
| `/graphify .` | Full rebuild (all files) |
| `/graphify . --update` | Incremental — only changed files |
| `/graphify . --mode deep` | More aggressive relationship extraction |
| `/graphify . --watch` | Auto-sync as files change (background) |
| `/graphify query "how does X connect to Y?"` | Natural language graph query |
| `/graphify path "NodeA" "NodeB"` | Shortest path between two concepts |
| `/graphify explain "ConceptName"` | Node details + all connections |
| `/graphify add <url>` | Fetch and index a paper, doc, or video |
| `/graphify . --cluster-only` | Rerun clustering on existing graph |

### CLI (outside assistant)

```bash
graphify query "show the auth flow"
graphify query "..." --graph graphify-out/graph.json
graphify path "UserService" "DatabasePool"
graphify explain "RateLimiter"
graphify hook install       # auto-rebuild graph after git commits (AST-only, free)
graphify hook status
graphify watch ./src        # watch directory for changes
graphify update ./src       # incremental update from CLI
graphify merge-graphs a.json b.json --out merged.json
```

### Setup & Team Workflow

```bash
# Install (once per machine)
pip install graphifyy

# One person builds and commits
/graphify .
git add graphify-out/ && git commit -m "Add knowledge graph"

# Everyone else — graph is ready immediately after pull
```

### .gitignore

```
graphify-out/manifest.json
graphify-out/cost.json
```

### .graphifyignore

Same syntax as `.gitignore`, supports `!` negation:
```
node_modules/
*.generated.py
*
!src/
!src/**
```

Code (25 languages) extracted locally via tree-sitter — no API calls, no telemetry. Docs/PDFs/images go through the assistant's model API using your own key.

## SSH to EC2

```
ssh -i ".ssh_tmp\poly-buy-sell.pem" -o StrictHostKeyChecking=no ubuntu@34.244.40.198
```

Key is at `.ssh_tmp/poly-buy-sell.pem` (repo-relative). EC2 host: `34.244.40.198`. Bot dir: `/home/ubuntu/minimal-bot/`. Env: `/home/ubuntu/minimal-bot/.env.poly`.

**Never use `timeout` on SSH commands** — it kills the SSH session and drops output. For foreground captures use Bash with `run_in_background: true` and a generous timeout (900s for a 15-min window). For file-based logging, `tee` buffers aggressively; prefer foreground SSH capture. Kill bot with `pkill -9`.

## Deploy → Run → Inspect Cycle

```
1. scp changed .py files to ubuntu@34.244.40.198:/home/ubuntu/minimal-bot/
2. If env changed: edit .env.poly directly on EC2 (sed or echo >>)
3. Verify: grep MINIMAL_* .env.poly
4. Kill old bot: pkill -9 -f 'python3 -u minimal_live_bot.py'
5. Clean: rm -f run_logs/*.log live.pid
6. Start: python3 -u minimal_live_bot.py 2>&1 (captured via foreground SSH)
7. Run 10-15 min bounded window
8. Kill: pkill -9 -f 'python3 -u minimal_live_bot.py'
9. Analyze log: grep binance_signal_decision, grep entry_hot_path, grep exit_hot_path
```

## Architecture (Hot Path)

- `minimal_live_bot.py` — entrypoint, config wiring, async supervision
- `bot_orchestrator.py` — routes events, anchors strike, logs decisions, evaluate_exit (iterates all positions)
- `binance_signal_engine.py` — tick→signal conversion, sigma_px (realized vol), momentum-based side
- `signal_decision.py` — Brownian barrier-cross probability + spread-aware edge floor + dual gating
- `template_armory.py` — pre-signs entry templates off hot path (single-flight)
- `hot_path_engine.py` — guard checks, submit, multi-position, max_concurrent_positions=3
- `fast_order_submitter.py` — raw POST /order with L2 auth, V2 rounding patch, deferExec: false
- `order_tracker.py` — user-WSS confirmed inventory, sellable vs owned, exposure tracking
- `exit_policy.py` / `exit_armory.py` — take-profit / expiry exits (stop-loss disabled)
- `runtime_state.py` — active market, quotes, position state
- `runtime_wiring.py` — builds and connects runtime objects

## Key Design Rules

- **No full SDK `create_and_post_order()` on signal** — pre-signed templates + fresh L2 headers
- **No subprocess wrappers, no JSON log writes on hot path, no raw event pretty-printing**
- **Sell inventory = MATCHED** (immediately sellable, no CONFIRMED wait), floored to 0.01 share quantum. Evidence: `order_tracker.py` `sellable()` uses MATCHED.
- **FAK entries, FAK exits** — exits use multi-attempt FAK burst (`MINIMAL_EXIT_FAK_ATTEMPTS=3`), no resting orders. Evidence: `minimal_live_bot.py:303`, `bot_orchestrator.py:385-401`.
- **Multi-position**: max 3 concurrent (all scopes), exit loop iterates all positions
- **`MINIMAL_USDC_PER_TRADE >= 1.01`** — 1.00 serializes below venue $1 minimum
- **Startup fails closed** unless `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are set
- **deferExec: false** on every order body
- **No local SELL reservation or balance cooldown** — FAK exits fire burst immediately; venue balance is the authoritative gate. Evidence: `bot_orchestrator.py` zero balance-cooldown code.

## Key Env Vars

| Var | Current | Purpose |
|-----|---------|---------|
| `MINIMAL_USDC_PER_TRADE` | 1.01 | Marketable BUY budget |
| `MINIMAL_MIN_BUY_LIMIT` | 0.10 | Min entry price floor |
| `MINIMAL_MAX_BUY_LIMIT` | 0.85 | Max entry price (max_ask) |
| `MINIMAL_DECISION_MIN_TTE_US` | 45000000 | No-entry window (45s) |
| `MINIMAL_DECISION_MIN_EDGE` | 0.05 | Universal edge floor |
| `MINIMAL_ENTRY_SLIPPAGE` | 0.05 | Spread crossing for FAK fills |
| `MINIMAL_STOP_LOSS_BPS` | 0 | Disabled (0 bps = always triggers from spread) |
| `MINIMAL_PROB_SIGMA_FLOOR_USD` | 2.0 | Volatility floor for prob model |
| `MINIMAL_PROB_SIGMA_SCALE` | 1.5 | Volatility scale multiplier |
| `MINIMAL_PROB_GAMMA_MOVE` | 0.5 | Weight of momentum in drift |
| `MINIMAL_PROB_MIN_PROB` | 0.55 | Probability floor (expensive tokens only) |
| `MINIMAL_PROB_USE_LEGACY` | false | Brownian model (not legacy heuristic) |
| `POLY_ALLOW_LIVE_ORDERS` | true | Required for live trading |
| `MINIMAL_REQUIRE_CALIBRATED_MODEL` | false | Set true when model is fitted |
| `MINIMAL_ENTRY_ORDER_TYPE` | FAK | Only FAK supported; `_order_type_env` rejects non-FAK. Evidence: `minimal_live_bot.py:128-129` |
| `MINIMAL_EXIT_ORDER_TYPE` | FAK | Hardcoded; env var ignored. Evidence: `minimal_live_bot.py:303` |
| `MINIMAL_EXIT_FAK_ATTEMPTS` | 3 | FAK exit burst count. Evidence: `exit_policy.py:32`, `minimal_live_bot.py:314` |
| `MINIMAL_SIGNAL_COOLDOWN_US` | 1000000 | 1s debounce between same-side BUY signals. Evidence: `binance_signal_engine.py:319` |
| `MINIMAL_ALLOW_RESTING_ORDERS` | false | Not used for exits (always FAK). `_order_type_env` rejects resting types. |
| `MINIMAL_MAX_CONCURRENT_POSITIONS` | 3 | Cap concurrent entries |

## Testing

```bash
# Full suite (2 expected failures: py_clob_client_v2 + live bot imports unavailable locally)
python -m pytest tests/ --ignore=tests/test_minimal_live_bot.py --ignore=tests/test_fast_order_submitter.py -q

# Targeted
python -m pytest tests/test_signal_decision.py tests/test_binance_signal_engine.py tests/test_hot_path_engine.py tests/test_bot_orchestrator.py -v
```

176 pass (expected: `py_clob_client_v2` not installed locally prevents 2 test files).

## Subagent Usage

**Do NOT spawn subagents for:**
- Editing code (single-file changes are trivial)
- Running tests (single bash command)
- SSH/EC2 operations
- Reading known file paths
- Questions answerable from `graphify-out/GRAPH_REPORT.md` or god nodes

**DO spawn `Explore` subagents (`model: haiku`) for:**
- "Where is X defined?"
- "Which files reference Y?"
- Broad codebase exploration
- Use `/graphify query "..."` first — it's faster than subagents for dependency questions

**Rule of thumb:** If it takes more than 3 sequential Grep/Glob calls, spawn an Explore agent or query the graph. If you know the file path, read it directly. Never spawn a subagent to do work you already know how to do.

## Git

- Commit messages: concise, focus on WHY. Use `Co-Authored-By: Codex <noreply@anthropic.com>` trailer.
- Never amend commits unless explicitly asked.
- Never force push to main.
- Prefer `git add <specific files>` over `git add -A`.
