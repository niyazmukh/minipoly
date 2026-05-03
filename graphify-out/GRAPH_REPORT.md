# Graph Report - .  (2026-05-03)

## Corpus Check
- 49 files · ~45,381 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 803 nodes · 2814 edges · 27 communities detected
- Extraction: 45% EXTRACTED · 55% INFERRED · 0% AMBIGUOUS · INFERRED: 1557 edges (avg confidence: 0.68)
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
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]

## God Nodes (most connected - your core abstractions)
1. `LocalOrderTracker` - 87 edges
2. `FastOrderTemplate` - 68 edges
3. `HotPathGuard` - 68 edges
4. `HotPathEngine` - 67 edges
5. `BinanceSignalConfig` - 60 edges
6. `SignalDecisionConfig` - 58 edges
7. `MinimalRuntimeState` - 55 edges
8. `MinimalBotOrchestrator` - 43 edges
9. `ExitPolicyConfig` - 41 edges
10. `BasisEstimator` - 37 edges

## Surprising Connections (you probably didn't know these)
- `LocalOrderTracker` --uses--> `Submits armed templates on signal with strict guards.      Buy-cycle locking sem`  [INFERRED]
  order_tracker.py → hot_path_engine.py
- `LocalOrderTracker` --uses--> `Clear a BUY cycle lock after its UNKNOWN submit has expired.`  [INFERRED]
  order_tracker.py → hot_path_engine.py
- `LocalOrderTracker` --uses--> `Map a submitter response to one of: accepted | rejected | unknown.      accepted`  [INFERRED]
  order_tracker.py → hot_path_engine.py
- `LocalOrderTracker` --uses--> `Submits armed templates on signal with strict guards.      Buy-cycle locking sem`  [INFERRED]
  order_tracker.py → hot_path_engine.py
- `L2Auth` --calls--> `_async_main()`  [INFERRED]
  auth.py → market_ws.py

## Hyperedges (group relationships)
- **Minimal Implementation Pipeline** — minimal_market_ws, minimal_user_channel_ws, minimal_order_tracker, minimal_order_placer, minimal_binance_sbe_listener [EXTRACTED 1.00]
- **Polymarket Authentication Flow** — auth_l1, auth_l2, auth_signature_types [EXTRACTED 1.00]

## Communities

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (73): _main(), _pct(), ProbeResult, _run_once(), _StubSubmitter, _template(), FastOrderTemplate, _accepted_submit() (+65 more)

### Community 1 - "Community 1"
Cohesion: 0.08
Nodes (82): _b64_urlsafe_decode_padded(), L2Auth, BasisEstimator, BasisEstimatorConfig, BinanceSignalConfig, _Armory, _ExitArmory, _HotPath (+74 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (73): from_env(), _apply_decision_overrides(), _apply_signal_engine_overrides(), _basis_save_loop(), _binance_args(), _binance_signal_cfg(), _bool_env(), build_live_bot() (+65 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (27): MinimalBotOrchestrator, _result_attempted_submit(), OpenPosition, MinimalMarket, _Armory, _CountingTracker, _ExitArmory, _HotPath (+19 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (37): Clear a BUY cycle lock after its UNKNOWN submit has expired., _async_main(), _consume_live(), _event_kind(), _event_ts(), _hot_print(), _is_invalid_trade_transition(), _iter_events_from_log() (+29 more)

### Community 5 - "Community 5"
Cohesion: 0.06
Nodes (46): _date_range(), _date_to_offset(), _extract_market_record(), fetch_binance(), fetch_markets(), _fetch_price_series(), fetch_prices(), _get_bytes() (+38 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (26): _Engine, ExitArmory, _PreparedExit, _same_exit(), decide_exit(), ExitDecision, _floor(), _hold() (+18 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (35): _async_main(), _coerce_float(), _discover_current_market(), _dispatch_event(), _emit_context_event(), _emit_inactive_event(), _emit_status(), _event_targets_subscribed() (+27 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (31): _async_main(), _compile_schema(), CompiledMessage, _consume_best_bid_ask(), _consume_callback_task(), _decode_symbol(), _dispatch_callback_result(), _env_bool() (+23 more)

### Community 9 - "Community 9"
Cohesion: 0.12
Nodes (19): _ArmedState, ceil_to_2dp(), ceil_to_tick(), _Engine, floor_to_2dp(), _PendingTarget, Quote-driven entry-template armory with single-flight rearming.      The market, TemplateArmory (+11 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (13): BinanceSignalEngine, BinanceSignalSnapshot, BinanceSignalStats, test_engine_rejects_non_monotonic_updates_without_changing_state(), test_engine_rejects_ticks_when_exchange_lag_is_too_high(), test_field_api_matches_tick_api_without_tick_allocation(), test_no_signal_requires_downward_move_and_negative_ofi(), test_yes_signal_requires_event_time_window_and_positive_ofi() (+5 more)

### Community 11 - "Community 11"
Cohesion: 0.21
Nodes (14): apply_market_event(), _best_book_side(), _dec(), _is_market_resolved(), _item_from_book(), _item_from_dict(), _iter_quote_items(), _QuoteItem (+6 more)

### Community 12 - "Community 12"
Cohesion: 0.25
Nodes (15): BinanceSignal, _contains_any(), decide_buy(), is_valid(), MarketSignalContract, SignalDecision, _strength_to_fair(), _signal() (+7 more)

### Community 13 - "Community 13"
Cohesion: 0.2
Nodes (10): load(), _est(), test_effective_strike_zero_when_polymarket_strike_zero(), test_ema_converges_to_steady_basis(), test_holds_when_tte_too_short(), test_holds_when_yes_mid_outside_band(), test_initialises_on_first_valid_sample(), test_load_missing_file_returns_default() (+2 more)

### Community 14 - "Community 14"
Cohesion: 0.67
Nodes (3): User Channel Documentation, Order Event, Trade Event

### Community 15 - "Community 15"
Cohesion: 1.0
Nodes (0): 

### Community 16 - "Community 16"
Cohesion: 1.0
Nodes (2): L1 Authentication, L2 Authentication

### Community 17 - "Community 17"
Cohesion: 1.0
Nodes (1): Drop tracker state for assets whose market has resolved.          Polymarket set

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): Release a provisional SELL reservation tied to an UNKNOWN submit.          Calle

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): Mark a submit as ambiguous — outcome could not be classified.          The order

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Convert UNKNOWN submits older than max_age_s to FAILED.          Caller is respo

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Reserve SELL inventory for an UNKNOWN submit without inventing an order.

### Community 22 - "Community 22"
Cohesion: 1.0
Nodes (1): Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p

### Community 23 - "Community 23"
Cohesion: 1.0
Nodes (1): Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p

### Community 24 - "Community 24"
Cohesion: 1.0
Nodes (1): Authentication Documentation

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Signature Types

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (1): Binance SBE Schema

## Knowledge Gaps
- **25 isolated node(s):** `1 = YES/Up won, 0 = NO/Down won, None = unresolved or ambiguous.`, `Estimate the Gamma series offset for `target` date.      Uses the empirical anch`, `Parse one Gamma API event object into a market record, or None if unusable.`, `Enumerate resolved btc-updown-5m markets from Gamma series API.      Strategy:`, `Attempt to fetch 1-min price history for YES/NO tokens from CLOB     prices-hist` (+20 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 15`** (2 nodes): `test_minimal_runtime_does_not_import_repo_src()`, `test_minimal_enclosure.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (2 nodes): `L1 Authentication`, `L2 Authentication`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (1 nodes): `Drop tracker state for assets whose market has resolved.          Polymarket set`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (1 nodes): `Release a provisional SELL reservation tied to an UNKNOWN submit.          Calle`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (1 nodes): `Mark a submit as ambiguous — outcome could not be classified.          The order`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `Convert UNKNOWN submits older than max_age_s to FAILED.          Caller is respo`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Reserve SELL inventory for an UNKNOWN submit without inventing an order.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 22`** (1 nodes): `Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 23`** (1 nodes): `Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 24`** (1 nodes): `Authentication Documentation`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 25`** (1 nodes): `Signature Types`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `Binance SBE Schema`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LocalOrderTracker` connect `Community 4` to `Community 0`, `Community 1`, `Community 2`, `Community 3`?**
  _High betweenness centrality (0.127) - this node is a cross-community bridge._
- **Why does `FastOrderTemplate` connect `Community 0` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 9`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `build_live_bot()` connect `Community 2` to `Community 1`, `Community 13`?**
  _High betweenness centrality (0.073) - this node is a cross-community bridge._
- **Are the 49 inferred relationships involving `LocalOrderTracker` (e.g. with `_Armory` and `_HotPath`) actually correct?**
  _`LocalOrderTracker` has 49 INFERRED edges - model-reasoned connections that need verification._
- **Are the 65 inferred relationships involving `FastOrderTemplate` (e.g. with `ProbeResult` and `_StubSubmitter`) actually correct?**
  _`FastOrderTemplate` has 65 INFERRED edges - model-reasoned connections that need verification._
- **Are the 67 inferred relationships involving `HotPathGuard` (e.g. with `ProbeResult` and `_StubSubmitter`) actually correct?**
  _`HotPathGuard` has 67 INFERRED edges - model-reasoned connections that need verification._
- **Are the 53 inferred relationships involving `HotPathEngine` (e.g. with `ProbeResult` and `_StubSubmitter`) actually correct?**
  _`HotPathEngine` has 53 INFERRED edges - model-reasoned connections that need verification._