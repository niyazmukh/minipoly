# Graph Report - .  (2026-05-03)

## Corpus Check
- 48 files · ~40,793 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 701 nodes · 2308 edges · 22 communities detected
- Extraction: 48% EXTRACTED · 52% INFERRED · 0% AMBIGUOUS · INFERRED: 1199 edges (avg confidence: 0.69)
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

## God Nodes (most connected - your core abstractions)
1. `LocalOrderTracker` - 83 edges
2. `HotPathEngine` - 64 edges
3. `HotPathGuard` - 61 edges
4. `FastOrderTemplate` - 59 edges
5. `BinanceSignalConfig` - 49 edges
6. `MinimalRuntimeState` - 44 edges
7. `SignalDecisionConfig` - 44 edges
8. `MinimalBotOrchestrator` - 35 edges
9. `ExitPolicyConfig` - 33 edges
10. `BinanceSignalEngine` - 32 edges

## Surprising Connections (you probably didn't know these)
- `_discover_current_market()` --calls--> `parse_gamma_iso8601_to_unix()`  [INFERRED]
  market_ws.py → utils.py
- `LocalOrderTracker` --uses--> `Submits armed templates on signal with strict guards.      Buy-cycle locking sem`  [INFERRED]
  order_tracker.py → hot_path_engine.py
- `L2Auth` --uses--> `_Runtime`  [INFERRED]
  auth.py → minimal_live_bot.py
- `L2Auth` --uses--> `LiveBot`  [INFERRED]
  auth.py → minimal_live_bot.py
- `Load and validate the calibrated signal model file.      Returns None when MINIM` --uses--> `L2Auth`  [INFERRED]
  minimal_live_bot.py → auth.py

## Hyperedges (group relationships)
- **Minimal Implementation Pipeline** — minimal_market_ws, minimal_user_channel_ws, minimal_order_tracker, minimal_order_placer, minimal_binance_sbe_listener [EXTRACTED 1.00]
- **Polymarket Authentication Flow** — auth_l1, auth_l2, auth_signature_types [EXTRACTED 1.00]

## Communities

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (83): _main(), _pct(), ProbeResult, _run_once(), _StubSubmitter, _template(), HotPathEngine, HotPathGuard (+75 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (53): BinanceSignal, _Armory, _ExitArmory, _HotPath, MinimalBotOrchestrator, ExitDecision, OpenPosition, TradeState (+45 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (42): _accepted_submit(), ArmedTemplate, _classify_submit_response(), HotPathResult, Clear a BUY cycle lock after its UNKNOWN submit has expired., Map a submitter response to one of: accepted | rejected | unknown.      accepted, _Submitter, _async_main() (+34 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (52): BasisEstimator, BasisEstimatorConfig, BinanceSignalConfig, BotConfig, ExitArmory, ExitPolicyConfig, build_order_body(), _decode_secret() (+44 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (47): _async_main(), _compile_schema(), CompiledMessage, _consume_best_bid_ask(), _decode_symbol(), _env_bool(), _env_float(), _env_int() (+39 more)

### Community 5 - "Community 5"
Cohesion: 0.07
Nodes (38): _b64_urlsafe_decode_padded(), L2Auth, CLOBHttpClient, _async_main(), _coerce_float(), _discover_current_market(), _dispatch_event(), _emit_context_event() (+30 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (41): _apply_decision_overrides(), _apply_signal_engine_overrides(), _basis_save_loop(), _binance_args(), _binance_signal_cfg(), _bool_env(), build_live_bot(), _cancel_accepted() (+33 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (19): _Engine, _ArmedState, ceil_to_tick(), _Engine, floor_to_2dp(), _PendingTarget, Quote-driven entry-template armory with single-flight rearming.      The market, TemplateArmory (+11 more)

### Community 8 - "Community 8"
Cohesion: 0.12
Nodes (10): BinanceSignalEngine, BinanceSignalSnapshot, BinanceSignalStats, test_engine_rejects_non_monotonic_updates_without_changing_state(), test_engine_rejects_ticks_when_exchange_lag_is_too_high(), test_field_api_matches_tick_api_without_tick_allocation(), test_no_signal_requires_downward_move_and_negative_ofi(), test_yes_signal_requires_event_time_window_and_positive_ofi() (+2 more)

### Community 9 - "Community 9"
Cohesion: 0.15
Nodes (22): _date_range(), _date_to_offset(), _extract_market_record(), fetch_binance(), fetch_markets(), _fetch_price_series(), fetch_prices(), _get_bytes() (+14 more)

### Community 10 - "Community 10"
Cohesion: 0.31
Nodes (13): decide_exit(), _floor(), _hold(), _price_at_tick(), _sell(), _target(), _position(), _quote() (+5 more)

### Community 11 - "Community 11"
Cohesion: 0.29
Nodes (10): load(), _est(), test_effective_strike_zero_when_polymarket_strike_zero(), test_ema_converges_to_steady_basis(), test_holds_when_tte_too_short(), test_holds_when_yes_mid_outside_band(), test_initialises_on_first_valid_sample(), test_load_missing_file_returns_default() (+2 more)

### Community 12 - "Community 12"
Cohesion: 0.44
Nodes (7): _best_book_side(), _dec(), _is_market_resolved(), _item_from_book(), _item_from_dict(), _iter_quote_items(), _QuoteItem

### Community 13 - "Community 13"
Cohesion: 0.5
Nodes (0):

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
Nodes (1): Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p

### Community 18 - "Community 18"
Cohesion: 1.0
Nodes (1): Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p

### Community 19 - "Community 19"
Cohesion: 1.0
Nodes (1): Authentication Documentation

### Community 20 - "Community 20"
Cohesion: 1.0
Nodes (1): Signature Types

### Community 21 - "Community 21"
Cohesion: 1.0
Nodes (1): Binance SBE Schema

## Knowledge Gaps
- **20 isolated node(s):** `1 = YES/Up won, 0 = NO/Down won, None = unresolved or ambiguous.`, `Estimate the Gamma series offset for `target` date.      Uses the empirical anch`, `Parse one Gamma API event object into a market record, or None if unusable.`, `Enumerate resolved btc-updown-5m markets from Gamma series API.      Strategy:`, `Attempt to fetch 1-min price history for YES/NO tokens from CLOB     prices-hist` (+15 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 15`** (2 nodes): `test_minimal_runtime_does_not_import_repo_src()`, `test_minimal_enclosure.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 16`** (2 nodes): `L1 Authentication`, `L2 Authentication`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 17`** (1 nodes): `Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 18`** (1 nodes): `Periodic reconcile + ping. Runs in parallel with the recv loop so the     recv p`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 19`** (1 nodes): `Authentication Documentation`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 20`** (1 nodes): `Signature Types`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 21`** (1 nodes): `Binance SBE Schema`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LocalOrderTracker` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 6`?**
  _High betweenness centrality (0.154) - this node is a cross-community bridge._
- **Why does `build_live_bot()` connect `Community 6` to `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 11`?**
  _High betweenness centrality (0.102) - this node is a cross-community bridge._
- **Why does `FastOrderTemplate` connect `Community 3` to `Community 0`, `Community 1`, `Community 2`, `Community 6`, `Community 7`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Are the 45 inferred relationships involving `LocalOrderTracker` (e.g. with `_Armory` and `_HotPath`) actually correct?**
  _`LocalOrderTracker` has 45 INFERRED edges - model-reasoned connections that need verification._
- **Are the 50 inferred relationships involving `HotPathEngine` (e.g. with `ProbeResult` and `_StubSubmitter`) actually correct?**
  _`HotPathEngine` has 50 INFERRED edges - model-reasoned connections that need verification._
- **Are the 60 inferred relationships involving `HotPathGuard` (e.g. with `ProbeResult` and `_StubSubmitter`) actually correct?**
  _`HotPathGuard` has 60 INFERRED edges - model-reasoned connections that need verification._
- **Are the 56 inferred relationships involving `FastOrderTemplate` (e.g. with `ProbeResult` and `_StubSubmitter`) actually correct?**
  _`FastOrderTemplate` has 56 INFERRED edges - model-reasoned connections that need verification._
