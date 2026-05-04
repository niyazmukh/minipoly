# Graph Report - C:/Users/niyaz/.repos/poly-buy-sell/minimal  (2026-05-04)

## Corpus Check
- Corpus is ~24,459 words - fits in a single context window. You may not need a graph.

## Summary
- 464 nodes · 1239 edges · 14 communities detected
- Extraction: 66% EXTRACTED · 34% INFERRED · 0% AMBIGUOUS · INFERRED: 427 edges (avg confidence: 0.61)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Basis & Signal Config|Basis & Signal Config]]
- [[_COMMUNITY_HTTP Client & Order Ops|HTTP Client & Order Ops]]
- [[_COMMUNITY_Order Tracking & Hot Path|Order Tracking & Hot Path]]
- [[_COMMUNITY_Cold Latency Probe|Cold Latency Probe]]
- [[_COMMUNITY_Auth & L2 Signing|Auth & L2 Signing]]
- [[_COMMUNITY_Binance SBE Listener|Binance SBE Listener]]
- [[_COMMUNITY_Orchestrator & Engine Wiring|Orchestrator & Engine Wiring]]
- [[_COMMUNITY_Calibration Data Fetch|Calibration Data Fetch]]
- [[_COMMUNITY_Signal Engine & Runtime State|Signal Engine & Runtime State]]
- [[_COMMUNITY_Exit Armory|Exit Armory]]
- [[_COMMUNITY_Fast Order Submit|Fast Order Submit]]
- [[_COMMUNITY_Signal Model Loading|Signal Model Loading]]
- [[_COMMUNITY_User Channel WebSocket|User Channel WebSocket]]
- [[_COMMUNITY_Basis Estimator State|Basis Estimator State]]

## God Nodes (most connected - your core abstractions)
1. `LocalOrderTracker` - 61 edges
2. `MinimalBotOrchestrator` - 37 edges
3. `build_live_bot()` - 32 edges
4. `MinimalRuntimeState` - 29 edges
5. `FastOrderTemplate` - 27 edges
6. `SignalDecisionConfig` - 25 edges
7. `BinanceSignalConfig` - 24 edges
8. `HotPathEngine` - 23 edges
9. `BasisEstimator` - 20 edges
10. `BinanceSignalEngine` - 20 edges

## Surprising Connections (you probably didn't know these)
- `_Runtime` --uses--> `L2Auth`  [INFERRED]
  minimal_live_bot.py → auth.py
- `LiveBot` --uses--> `L2Auth`  [INFERRED]
  minimal_live_bot.py → auth.py
- `Load and validate the calibrated signal model file.      Returns None when MINIM` --uses--> `L2Auth`  [INFERRED]
  minimal_live_bot.py → auth.py
- `Backwards-compatible wrapper used by tests; raises if required & missing.` --uses--> `L2Auth`  [INFERRED]
  minimal_live_bot.py → auth.py
- `Expire stale UNKNOWN submits and release their local safeguards.` --uses--> `L2Auth`  [INFERRED]
  minimal_live_bot.py → auth.py

## Communities

### Community 0 - "Basis & Signal Config"
Cohesion: 0.08
Nodes (45): BasisEstimator, BasisEstimatorConfig, BinanceSignalConfig, _Armory, _ExitArmory, _HotPath, MinimalBotOrchestrator, Seed the signal engine's strike for the new market.          Preference order: (+37 more)

### Community 1 - "HTTP Client & Order Ops"
Cohesion: 0.07
Nodes (41): _apply_decision_overrides(), _apply_signal_engine_overrides(), _basis_save_loop(), _binance_args(), _binance_signal_cfg(), _bool_env(), build_live_bot(), _cancel_accepted() (+33 more)

### Community 2 - "Order Tracking & Hot Path"
Cohesion: 0.08
Nodes (26): _async_main(), _consume_live(), _event_kind(), _event_ts(), _floor_size_to_quantum(), _hot_print(), _is_invalid_trade_transition(), _iter_events_from_log() (+18 more)

### Community 3 - "Cold Latency Probe"
Cohesion: 0.08
Nodes (25): _main(), _pct(), ProbeResult, _run_once(), _StubSubmitter, _template(), FastOrderTemplate, _accepted_submit() (+17 more)

### Community 4 - "Auth & L2 Signing"
Cohesion: 0.08
Nodes (33): _b64_urlsafe_decode_padded(), L2Auth, from_env(), CLOBHttpClient, _async_main(), _coerce_float(), _discover_current_market(), _dispatch_event() (+25 more)

### Community 5 - "Binance SBE Listener"
Cohesion: 0.08
Nodes (24): _async_main(), _compile_schema(), CompiledMessage, _consume_best_bid_ask(), _consume_callback_task(), _decode_symbol(), _dispatch_callback_result(), _env_bool() (+16 more)

### Community 6 - "Orchestrator & Engine Wiring"
Cohesion: 0.12
Nodes (9): Drop tracker state for assets whose market has resolved.          Polymarket set, apply_market_event(), _best_book_side(), _dec(), _is_market_resolved(), _item_from_book(), _item_from_dict(), _iter_quote_items() (+1 more)

### Community 7 - "Calibration Data Fetch"
Cohesion: 0.15
Nodes (22): _date_range(), _date_to_offset(), _extract_market_record(), fetch_binance(), fetch_markets(), _fetch_price_series(), fetch_prices(), _get_bytes() (+14 more)

### Community 8 - "Signal Engine & Runtime State"
Cohesion: 0.18
Nodes (11): BinanceSignal, _bs_prob_yes(), _contains_any(), decide_buy(), is_valid(), MarketSignalContract, _phi(), Standard normal CDF using math.erf — no scipy dependency. (+3 more)

### Community 9 - "Exit Armory"
Cohesion: 0.25
Nodes (4): _Engine, ExitArmory, _PreparedExit, _same_exit()

### Community 10 - "Fast Order Submit"
Cohesion: 0.23
Nodes (8): build_order_body(), _decode_secret(), extract_order_id(), _extract_order_id_deep(), _is_v2_signed_order(), _patch_v2_rounding_for_venue(), prepare_template(), _uses_v2_orders()

### Community 11 - "Signal Model Loading"
Cohesion: 0.44
Nodes (12): CalibratedModelError, CalibrationProvenance, _cast(), DecisionOverrides, load_calibrated_model(), _parse_decision(), _parse_provenance(), _parse_signal_engine() (+4 more)

### Community 12 - "User Channel WebSocket"
Cohesion: 0.62
Nodes (6): _dispatch_user_event(), _emit_status(), listen_forever(), _resolve_api_creds(), _safe_print(), _to_events()

### Community 13 - "Basis Estimator State"
Cohesion: 0.33
Nodes (1): load()

## Knowledge Gaps
- **11 isolated node(s):** `1 = YES/Up won, 0 = NO/Down won, None = unresolved or ambiguous.`, `Estimate the Gamma series offset for `target` date.      Uses the empirical anch`, `Parse one Gamma API event object into a market record, or None if unusable.`, `Enumerate resolved btc-updown-5m markets from Gamma series API.      Strategy:`, `Attempt to fetch 1-min price history for YES/NO tokens from CLOB     prices-hist` (+6 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LocalOrderTracker` connect `Order Tracking & Hot Path` to `Basis & Signal Config`, `HTTP Client & Order Ops`, `Cold Latency Probe`, `Orchestrator & Engine Wiring`?**
  _High betweenness centrality (0.211) - this node is a cross-community bridge._
- **Why does `build_live_bot()` connect `HTTP Client & Order Ops` to `Basis & Signal Config`, `Auth & L2 Signing`, `Basis Estimator State`?**
  _High betweenness centrality (0.123) - this node is a cross-community bridge._
- **Why does `MinimalBotOrchestrator` connect `Basis & Signal Config` to `Signal Engine & Runtime State`, `Order Tracking & Hot Path`, `Binance SBE Listener`, `Orchestrator & Engine Wiring`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Are the 21 inferred relationships involving `LocalOrderTracker` (e.g. with `_Armory` and `_HotPath`) actually correct?**
  _`LocalOrderTracker` has 21 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `MinimalBotOrchestrator` (e.g. with `BasisEstimator` and `BinanceSignalConfig`) actually correct?**
  _`MinimalBotOrchestrator` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `RuntimeError` (e.g. with `_fetch_schema_xml()` and `_resolve_fixed_type()`) actually correct?**
  _`RuntimeError` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `build_live_bot()` (e.g. with `from_env()` and `RuntimeError`) actually correct?**
  _`build_live_bot()` has 16 INFERRED edges - model-reasoned connections that need verification._