# Graph Report - C:\Users\niyaz\.repos\poly-buy-sell\minimal  (2026-05-04)

## Corpus Check
- 25 files · ~24,054 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 466 nodes · 1251 edges · 13 communities detected
- Extraction: 65% EXTRACTED · 35% INFERRED · 0% AMBIGUOUS · INFERRED: 443 edges (avg confidence: 0.6)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]

## God Nodes (most connected - your core abstractions)
1. `LocalOrderTracker` - 63 edges
2. `MinimalBotOrchestrator` - 37 edges
3. `build_live_bot()` - 32 edges
4. `MinimalRuntimeState` - 30 edges
5. `FastOrderTemplate` - 28 edges
6. `SignalDecisionConfig` - 27 edges
7. `BinanceSignalConfig` - 26 edges
8. `BasisEstimator` - 21 edges
9. `BinanceSignalEngine` - 20 edges
10. `LiveBot` - 20 edges

## Surprising Connections (you probably didn't know these)
- `FastOrderTemplate` --uses--> `Clear a BUY cycle lock after its UNKNOWN submit has expired.`  [INFERRED]
  fast_order_submitter.py → hot_path_engine.py
- `FastOrderTemplate` --uses--> `Map a submitter response to one of: accepted | rejected | unknown.      accepted`  [INFERRED]
  fast_order_submitter.py → hot_path_engine.py
- `L2Auth` --calls--> `_async_main()`  [INFERRED]
  auth.py → market_ws.py
- `L2Auth` --calls--> `build_live_bot()`  [INFERRED]
  auth.py → C:\Users\niyaz\.repos\poly-buy-sell\minimal\minimal_live_bot.py
- `BasisEstimatorConfig` --calls--> `build_live_bot()`  [INFERRED]
  basis_estimator.py → C:\Users\niyaz\.repos\poly-buy-sell\minimal\minimal_live_bot.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.09
Nodes (41): _b64_urlsafe_decode_padded(), L2Auth, BasisEstimator, BasisEstimatorConfig, load(), BinanceSignalConfig, _Armory, _ExitArmory (+33 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (29): Clear a BUY cycle lock after its UNKNOWN submit has expired., Map a submitter response to one of: accepted | rejected | unknown.      accepted, _Submitter, _async_main(), _consume_live(), _event_kind(), _event_ts(), _floor_size_to_quantum() (+21 more)

### Community 2 - "Community 2"
Cohesion: 0.07
Nodes (42): _apply_decision_overrides(), _apply_signal_engine_overrides(), _basis_save_loop(), _binance_args(), _binance_signal_cfg(), _bool_env(), build_live_bot(), _cancel_accepted() (+34 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (22): MinimalBotOrchestrator, _result_attempted_submit(), decide_exit(), ExitDecision, _floor(), _hold(), OpenPosition, _price_at_tick() (+14 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (27): _main(), _pct(), ProbeResult, _run_once(), _StubSubmitter, _template(), _Engine, ExitArmory (+19 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (24): _async_main(), _compile_schema(), CompiledMessage, _consume_best_bid_ask(), _consume_callback_task(), _decode_symbol(), _dispatch_callback_result(), _env_bool() (+16 more)

### Community 6 - "Community 6"
Cohesion: 0.11
Nodes (28): from_env(), _async_main(), _coerce_float(), _discover_current_market(), _dispatch_event(), _emit_context_event(), _emit_inactive_event(), _emit_status() (+20 more)

### Community 7 - "Community 7"
Cohesion: 0.15
Nodes (22): _date_range(), _date_to_offset(), _extract_market_record(), fetch_binance(), fetch_markets(), _fetch_price_series(), fetch_prices(), _get_bytes() (+14 more)

### Community 8 - "Community 8"
Cohesion: 0.44
Nodes (12): CalibratedModelError, CalibrationProvenance, _cast(), DecisionOverrides, load_calibrated_model(), _parse_decision(), _parse_provenance(), _parse_signal_engine() (+4 more)

### Community 9 - "Community 9"
Cohesion: 0.29
Nodes (8): build_order_body(), _decode_secret(), extract_order_id(), _extract_order_id_deep(), _is_v2_signed_order(), _patch_v2_rounding_for_venue(), prepare_template(), _uses_v2_orders()

### Community 10 - "Community 10"
Cohesion: 0.31
Nodes (10): BinanceSignal, _bs_prob_yes(), _contains_any(), decide_buy(), is_valid(), _phi(), Standard normal CDF using math.erf — no scipy dependency., Brownian barrier-cross probability that microprice closes >= strike at expiry. (+2 more)

### Community 11 - "Community 11"
Cohesion: 0.62
Nodes (6): _dispatch_user_event(), _emit_status(), listen_forever(), _resolve_api_creds(), _safe_print(), _to_events()

### Community 12 - "Community 12"
Cohesion: 1.0
Nodes (1): Reserve SELL inventory for an UNKNOWN submit without inventing an order.

## Knowledge Gaps
- **13 isolated node(s):** `1 = YES/Up won, 0 = NO/Down won, None = unresolved or ambiguous.`, `Estimate the Gamma series offset for `target` date.      Uses the empirical anch`, `Parse one Gamma API event object into a market record, or None if unusable.`, `Enumerate resolved btc-updown-5m markets from Gamma series API.      Strategy:`, `Attempt to fetch 1-min price history for YES/NO tokens from CLOB     prices-hist` (+8 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 12`** (1 nodes): `Reserve SELL inventory for an UNKNOWN submit without inventing an order.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LocalOrderTracker` connect `Community 1` to `Community 0`, `Community 2`, `Community 3`, `Community 4`?**
  _High betweenness centrality (0.217) - this node is a cross-community bridge._
- **Why does `build_live_bot()` connect `Community 2` to `Community 0`, `Community 3`?**
  _High betweenness centrality (0.120) - this node is a cross-community bridge._
- **Why does `MinimalBotOrchestrator` connect `Community 3` to `Community 0`, `Community 1`, `Community 10`, `Community 5`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Are the 22 inferred relationships involving `LocalOrderTracker` (e.g. with `_Submitter` and `QuoteSnapshot`) actually correct?**
  _`LocalOrderTracker` has 22 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `MinimalBotOrchestrator` (e.g. with `build_runtime()` and `BasisEstimator`) actually correct?**
  _`MinimalBotOrchestrator` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `RuntimeError` (e.g. with `_fetch_schema_xml()` and `_resolve_fixed_type()`) actually correct?**
  _`RuntimeError` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 16 inferred relationships involving `build_live_bot()` (e.g. with `from_env()` and `RuntimeError`) actually correct?**
  _`build_live_bot()` has 16 INFERRED edges - model-reasoned connections that need verification._