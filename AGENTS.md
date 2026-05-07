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
- `exit_policy.py` / `exit_armory.py` — take-profit / expiry FAK exits only
- `runtime_state.py` — active market and quotes only; inventory/positions come from `order_tracker.py`
- `runtime_wiring.py` — builds and connects runtime objects

## Key Design Rules

- **Evidence first**: source, live logs, Graphify, and actual runtime behavior beat prior AI summaries. Treat Graphify inferred edges as hypotheses until source proves them.
- **Occam's razor**: delete complexity unless it protects against a real live failure. Prefer deletion over abstraction and one source of truth over reconciliation logic.
- **No monkey job**: no monkey patches, no hidden SDK global mutation, no broad rewrites that disguise clutter.
- **Hot path discipline**: Binance tick to BUY submit must cross the fewest possible functions and gates.
- **FAK rejection is cheap**: harmless BUY/SELL no-match or SELL balance rejection should not be locally over-protected.
- **WSS authority**: user WSS trades are inventory truth. HTTP matched response is useful for immediate exit only, not final inventory.
- **UNKNOWN submit stays**: HTTP timeout can later bind through user WSS, so pending UNKNOWN matching is real risk control.
- **BUY duplicate protection stays only where it prevents real exposure**: `buy_in_flight`, same-token/pending exposure cap, and max concurrent exposure.
- **SELL must not be locally over-gated**: no SELL reservations, balance locks, cooldowns, or in-flight blockers.
- **Decimal precision is non-negotiable**: venue-facing signed bodies must be validated locally before submit.
- **Analyzer is offline**: useful for logs, not bot core, not runtime graph. Keep `docs/` excluded unless explicitly graphing docs.
- **Grep after every refactor**: propagate removals through source, docs, env, tests, and graph; stale symbols are bugs.
- **Docs must not lie**: active docs describe actual runtime, not old intended architecture.
- **EC2 deploys are runtime-only**: no tests, no docs, no generated artifacts unless explicitly requested.
- **Runtime changes require validation**: `py_compile`, focused tests where possible, stale-symbol grep, Graphify update, and live log evidence when running EC2.
- **No full SDK `create_and_post_order()` on signal** — pre-signed templates + fresh L2 headers
- **No subprocess wrappers, no JSON log writes on hot path, no raw event pretty-printing**
- **Sell inventory = MATCHED** (immediately sellable, no CONFIRMED wait), floored to 0.01 share quantum. Evidence: `order_tracker.py` `sellable()` uses MATCHED.
- **FAK entries, FAK exits** — exits use multi-attempt FAK burst (`MINIMAL_EXIT_FAK_ATTEMPTS=3`), no resting orders. Evidence: `minimal_live_bot.py`, `bot_orchestrator.py`.
- **Multi-position**: max 3 concurrent (all scopes), exit loop iterates all positions
- **`MINIMAL_USDC_PER_TRADE >= 1.01`** — 1.00 serializes below venue $1 minimum
- **Startup fails closed** unless `MINIMAL_MIN_BUY_LIMIT` and `MINIMAL_DECISION_MIN_TTE_US` are set
- **deferExec: false** on every order body
- **No Binance signal debounce** — every valid tick-level signal reaches decision/submit; duplicate exposure is handled by `HotPathEngine` pending-submit and inventory guards.
- **No local SELL balance lock or cooldown** — FAK exits fire burst immediately; venue balance is the authoritative gate. Evidence: `bot_orchestrator.py` zero balance-cooldown code.

## Key Env Vars

| Var | Current | Purpose |
|-----|---------|---------|
| `MINIMAL_USDC_PER_TRADE` | 1.01 | Marketable BUY budget |
| `MINIMAL_MIN_BUY_LIMIT` | 0.35 | Min entry executable price floor |
| `MINIMAL_MAX_BUY_LIMIT` | 0.65 | Max entry executable price |
| `MINIMAL_DECISION_MIN_TTE_US` | 45000000 | No-entry window (45s) |
| `MINIMAL_DECISION_MIN_EDGE` | 0.05 | Universal edge floor |
| `MINIMAL_ENTRY_SLIPPAGE` | 0.03 | Added to entry edge math and BUY limit |
| `MINIMAL_TAKE_PROFIT_BPS` | 1000 | FAK SELL target over entry price |
| `MINIMAL_PROB_SIGMA_FLOOR_USD` | 2.0 | Volatility floor for prob model |
| `MINIMAL_PROB_SIGMA_SCALE` | 1.5 | Volatility scale multiplier |
| `MINIMAL_PROB_GAMMA_MOVE` | 0.5 | Weight of momentum in drift |
| `POLY_ALLOW_LIVE_ORDERS` | true | Required for live trading |
| `MINIMAL_EXIT_FAK_ATTEMPTS` | 3 | FAK exit burst count. Evidence: `exit_policy.py:32`, `minimal_live_bot.py:314` |
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
