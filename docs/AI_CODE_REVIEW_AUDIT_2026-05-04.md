# Minimal Bot Code Review Audit - 2026-05-04

Role: senior HFT systems engineer review of the live Polymarket minimal bot after the 2026-05-04 debug session.

Primary navigation source: `graphify-out/GRAPH_REPORT.md` and Graphify queries. Findings below were source-verified with file and line references.

## 1. Graph Topology Review

The graph shows a thin hot-path spine with heavy cross-community coupling: `LocalOrderTracker` is the largest god node at 61 edges, `MinimalBotOrchestrator` has 37, `build_live_bot()` has 32, and `HotPathEngine` has 23. Evidence: `graphify-out/GRAPH_REPORT.md:15`.

The graph's community split is useful but noisy. `Basis & Signal Config`, `HTTP Client & Order Ops`, and `Order Tracking & Hot Path` all have very low cohesion, which matches the source: runtime wiring, config, tracker state, and execution guards are interleaved rather than isolated.

Graphify also produced bad inferred shortcuts such as `BinanceSignalEngine -> binance_signal_engine.py -> BinanceSignalConfig -> _Runtime -> FastOrderSubmitter`, so source verification is mandatory. The graph is good for navigation and bridge discovery; it is not reliable enough for final call-path proof without source reads.

Missing edge: graph exposes `HotPathEngine.release_expired_unknown_buy_lock()` and buy-cycle methods, but the current submit path no longer calls them. That is not just graph noise; source confirms stale lock code remains. Evidence: `hot_path_engine.py:308`, `hot_path_engine.py:345`.

## 2. Changed Functions - End-to-End Trace

| Change | Trace | Verdict |
| --- | --- | --- |
| `_window_baseline` | `on_tick_fields()` appends samples, then `_maybe_signal()` calls `_window_baseline()` and forwards `sigma_px` into `BinanceSignal`. Evidence: `binance_signal_engine.py:249`, `binance_signal_engine.py:291`, `binance_signal_engine.py:347`. | Correct structurally. No bad-trade path found. |
| `_maybe_signal` | Momentum side is derived from `move`, then OFI/imbalance filters gate, then `MinimalBotOrchestrator.on_binance_tick_fields()` calls `decide_buy()`. Evidence: `binance_signal_engine.py:298`, `bot_orchestrator.py:220`. | Correct structurally. |
| `_bs_prob_yes` | `decide_buy()` calls `_bs_prob_yes()`, which uses `gamma*move` and clamps probability. Evidence: `signal_decision.py:109`, `signal_decision.py:169`. | Correct structurally. |
| `SignalDecisionConfig` | `minimal_live_bot._entry_decision_cfg()` passes `min_edge_cheap`, but `SignalDecisionConfig` does not define it. Evidence: `minimal_live_bot.py:365`, `minimal_live_bot.py:381`, `signal_decision.py:40`. | Critical startup break. |
| `decide_buy` | TTE, quote age, ask bounds, strength, edge, and expensive-token probability floor all gate before BUY. Evidence: `signal_decision.py:89`, `signal_decision.py:117`. | Good gate order; no bad decision reaches venue by this function alone. |
| `template_armory.entry_slippage` | `buy_limit = ceil_to_tick(ask + entry_slippage)`, then template and guard are armed. Evidence: `template_armory.py:136`, `template_armory.py:177`. | Correct, but guard uses original `ask`, so a price move within slippage can reject instead of use the slippage budget. Latency/opportunity issue, not wrong trade. |
| `decide_exit` stop-loss | Stop-loss requires `cfg.stop_loss_bps > 0`. Evidence: `exit_policy.py:119`. | Correct. |
| `evaluate_exit` | It checks `_in_flight_sell`, loops tracker assets, uses `sellable()`, arms exit, submits, then returns immediately after the first SELL decision. Evidence: `bot_orchestrator.py:300`, `bot_orchestrator.py:307`, `bot_orchestrator.py:357`. | Safe, but sequential. |
| `HotPathEngine.on_signal` | BUY cap counts only `owned_by_asset`; pending accepted entries are not counted. Evidence: `hot_path_engine.py:166`, `order_tracker.py:244`. | Critical duplicate/over-cap path. |
| `fast_order_submitter` | Hot path uses prebuilt body bytes, fresh L2 headers, aiohttp POST, and timeout. Evidence: `fast_order_submitter.py:237`. V2 rounding patch happens off hot path in `prepare_template()`. Evidence: `fast_order_submitter.py:202`. | Correct. Network dominates. |
| `minimal_live_bot` wiring | Env wires max concurrent, entry slippage, FAK/GTC, stop-loss, and probability config. Evidence: `minimal_live_bot.py:280`, `minimal_live_bot.py:289`, `minimal_live_bot.py:300`. | Broken by `min_edge_cheap`. |
| Hybrid order types | Entry and exit order types are explicitly env-driven with resting-order guard. Evidence: `minimal_live_bot.py:125`, `minimal_live_bot.py:295`, `minimal_live_bot.py:307`. | Correct. |

## 3. Redundancy & Dead Code

Dead buy-cycle lock remains after multi-position change. `_engage_buy_cycle_lock()`, `_buy_blocked_by_open_exposure()`, `release_expired_unknown_buy_lock()`, and `_buy_cycle_*` fields are present, but no production caller references them. Evidence: `hot_path_engine.py:76`, `hot_path_engine.py:308`, `hot_path_engine.py:360`. `rg` found only definitions and tests.

`SignalDecisionConfig.min_edge_cheap` is a removed or never-added field still wired from env. Evidence: `minimal_live_bot.py:381`, `signal_decision.py:40`.

## 4. Hot-Path Latency Audit

Largest latency is still venue/network submit: handoff records 280-400ms FAK submit latency. Evidence: `docs/AI_HANDOFF_2026-05-03.md:85`.

Local hot-path work before POST is small: quote/guard checks, tracker `register_submit()`, HMAC header signing, then `aiohttp.post()`. Evidence: `hot_path_engine.py:154`, `fast_order_submitter.py:164`, `fast_order_submitter.py:241`.

Guard ordering issue: `HotPathEngine.on_signal()` counts all owned positions before checking quote existence/staleness. With a small cap this is minor, but it is still avoidable pre-submit work on stale/missing quotes. Evidence: `hot_path_engine.py:166`, `hot_path_engine.py:176`.

Exit latency is deliberately serial: `evaluate_exit()` returns after one SELL attempt and `_in_flight_sell` blocks overlap. Evidence: `bot_orchestrator.py:300`, `bot_orchestrator.py:357`.

## 5. Pipeline Integrity Check

Entry inside 45s no-entry window: no path found. Entry is blocked at market-event arming and at `decide_buy()` TTE. Evidence: `bot_orchestrator.py:145`, `signal_decision.py:89`.

Sell of unconfirmed inventory: no direct path found. Exit uses `sellable()`, which requires owned and settled inventory after flooring. Evidence: `bot_orchestrator.py:308`, `order_tracker.py:420`.

Duplicate submission / exceeding max concurrent: path exists. I reproduced with `max_concurrent_positions=1`: two accepted BUYs were submitted before any WSS trade updated `owned_by_asset`; tracker ended with two confirmed pending entries and `owned={}`. Source cause: cap counts only `owned_by_asset`, while accepted pending submits are tracked separately. Evidence: `hot_path_engine.py:169`, `order_tracker.py:268`, `order_tracker.py:393`.

Stop-loss disabled: no path found when bps is 0. Evidence: `exit_policy.py:119`.

## 6. Doc/Code Inconsistencies

README says "Buy submission is single-position by design" and references "one-unsold-position rule," but current code and handoff describe multi-position max 3. Evidence: `docs/README.md:13`, `docs/README.md:48`, `docs/AI_HANDOFF_2026-05-03.md:88`.

Docs say `evaluate_exit` iterates all positions independently, but code returns after the first armed SELL decision. Evidence: `docs/EC2_STANDALONE_RUNBOOK.md:67`, `bot_orchestrator.py:357`. The handoff's known issue is more accurate: sequential exit bottleneck. Evidence: `docs/AI_HANDOFF_2026-05-03.md:97`.

## 7. Ranked Findings

### Critical

1. Live bot config construction is broken by stale `min_edge_cheap`.
Evidence: `minimal_live_bot.py:381`, `signal_decision.py:40`.
Impact: `build_live_bot()` cannot construct `SignalDecisionConfig` once SDK imports are available.
Recommended fix: remove `min_edge_cheap` wiring or add the field and use it deliberately.

2. Entry concurrency cap ignores pending/accepted BUYs before WSS ownership.
Evidence: `hot_path_engine.py:166`, `order_tracker.py:393`.
Impact: duplicate entries and max-position violations can reach venue during WSS lag.
Recommended fix: count pending/confirmed entry submits and live BUY orders in the cap, or reserve entry exposure at submit acceptance.

### High

1. Exit loop is still single-submit per tick.
Evidence: `bot_orchestrator.py:343`, `bot_orchestrator.py:357`.
Impact: position B waits behind position A despite "iterate all" wording.
Recommended fix: make docs explicit or batch/parallelize only if existing design allows it.

### Medium

1. Buy-cycle lock code is dead and misleading.
Evidence: `hot_path_engine.py:54`, `hot_path_engine.py:308`.
Impact: reviewers and tests reason about a guard that no longer protects production.
Recommended fix: delete dead lock fields/methods/tests or reintroduce a real pending-entry guard.

2. Calibrated model overrides discard newer probabilistic config fields because `apply_to_decision()` reconstructs only legacy fields.
Evidence: `signal_model.py:83`.
Impact: loading a calibrated model can silently reset `min_ask`, `min_prob`, sigma/gamma settings, and legacy flag to defaults.
Recommended fix: preserve all `SignalDecisionConfig` fields via `dataclasses.replace()`.

### Low

1. Guard ordering does small avoidable work before quote checks.
Evidence: `hot_path_engine.py:166`, `hot_path_engine.py:176`.
Impact: minor local overhead only.
Recommended fix: check armed quote freshness before scanning tracker positions.

## Verification

Focused tests run:

```powershell
python -m pytest tests/test_signal_decision.py tests/test_binance_signal_engine.py tests/test_hot_path_engine.py -q
```

Result:

```text
36 passed in 0.74s
```

Manual reproduction:

An inline script using `HotPathEngine(max_concurrent_positions=1)` submitted two accepted BUYs before WSS ownership existed. Result:

```text
1 True submitted order-1
2 True submitted order-2
pending [('entry', 'CONFIRMED', 'order-1'), ('entry', 'CONFIRMED', 'order-2')]
owned {}
```
