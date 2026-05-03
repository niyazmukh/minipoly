# Signal-Model Calibration Protocol

**Status:** Required before any live deployment of the minimal bot.
**Audience:** anyone fitting, evaluating, or signing off on a calibrated
`signal_model.json` artifact.

> **2026-05-04 update.** The runtime now uses a Brownian barrier-cross
> probability inside `signal_decision.decide_buy`:
> `P_yes = Phi((microprice − strike + alpha·OFI + beta·imbalance·sigma)
>              / (sigma_scale · sigma_px · sqrt(tte_s)))`.
> The strike is anchored once per market rotation from the median Binance
> microprice over `[slug_ts, slug_ts + 0.3 s]`, immutable until the next
> rotation. `BasisEstimator` is telemetry-only and no longer adjusts the
> threshold. Tunables are exposed as `MINIMAL_PROB_*` env vars (see
> `docs/README.md`). Defaults are intentionally conservative: an unfitted
> deployment will trade *less* often than the legacy heuristic. Calibration
> below should fit `prob_alpha_ofi`, `prob_beta_imb`, `prob_sigma_scale` from
> logged `binance_signal_decision` events plus settled outcomes.

This document defines the protocol that turns the heuristic signal stack in
`binance_signal_engine.py` + `signal_decision.py` into a measured, audited
parameter set. The runtime consumes the resulting JSON via
`signal_model.load_calibrated_model`. A model that has not passed every gate
in §6 must not be deployed.

The protocol is deliberately conservative. We are reading a leveraged,
mean-reverting cross-venue signal in a low-margin market. Marginally
positive expected value disappears under realistic execution friction. The
default assumption is that any candidate model fails until proven otherwise.

---

## 1. What we are predicting

Polymarket's *btc-updown-5m* market settles at end-of-window from
Polymarket's reference feed. The protocol's question is binary: did
BTC-USD's reference price at `end_ts` exceed `strike`? Each market has two
tradeable outcome tokens whose prices are bounded `[0, 1]` and sum to ≈1.

We are not modelling BTC's true future price. We are modelling the
**signed expected difference between the YES token's mid-price now and its
settlement value at `end_ts`**, conditioned on instantaneous Binance
microstructure features. Equivalently:

```
y_t = settle_t  -  yes_mid_t        (when our trade-side is YES)
y_t = (1 - settle_t) - no_mid_t     (when our trade-side is NO)
```

with `settle_t ∈ {0, 1}` and `mid_t ∈ [0, 1]`. Denominate edge in
*Polymarket cents per share*, then convert to bps of capital deployed for
P&L accounting.

Crucially: Polymarket settles from its reference feed, not Binance.
Binance is a leading indicator, not the truth. Any model that ignores
basis risk between Binance microprice and the Polymarket reference is
silently betting on near-perfect cointegration; do not assume that.

---

## 2. Data requirements

A calibration run must consume the following aligned streams over an
interval ≥ **30 days** of continuous market activity. The minimum
sample count for the runtime accept gate is **20,000 in-scope decision
events** (see §4 for the definition of an event).

| Stream | Source | Cadence | Notes |
|---|---|---|---|
| Polymarket book updates | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | event-driven | full L1; both YES and NO tokens |
| Polymarket trades / matched orders | same WS | event-driven | for slippage measurement |
| Polymarket settlement outcomes | reference feed published by Polymarket per market | once per market | 0/1 label |
| Binance SBE BBO | `wss://stream.binance.com/.../sbe` `BestBidAskStreamEvent` | event-driven | top-of-book; ≥1 µs precision |
| Process clock | local `monotonic_ns()` | per event | used to measure WS-to-engine latency |

**Capture method — live (preferred for production re-fits).** Run
`binance_sbe_listener` and `market_ws.listen_forever` with raw frames
written to disk (parquet preferred; JSONL acceptable). Do not rely on the
existing `docs/userchannel.log` for calibration — it is sparse and not
aligned to a single market epoch.

**Capture method — retrospective (acceptable for initial calibration and
regime studies).** It is not necessary to wait 30 days for fresh live data.
High-quality historical sources exist for both venues; using them correctly
yields a calibrated model that is statistically stronger than one built on
30 days of live ticks because the sample span and regime diversity are much
larger. The hygiene gates and acceptance gates in §2 and §6 apply unchanged.

*Binance historical data* (see
`https://github.com/binance/binance-spot-api-docs` for the full REST
reference):

- `data.binance.vision` is Binance's public S3 archive. It provides
  compressed daily and monthly files for every spot pair going back to
  listing. The relevant paths for BTCUSDT are:
  - Book-ticker snapshots: `data/spot/daily/bookTicker/BTCUSDT/`
    — one row per best-bid/ask update, microsecond timestamps, equivalent
    to the `BestBidAskStreamEvent` produced by the live SBE stream.
  - Aggregate trades: `data/spot/daily/aggTrades/BTCUSDT/`
    — trade-level flow if you later add aggressor features.
  Download with `wget` or `boto3` (no auth required). Decompress `.zip`
  to CSV; convert timestamps (milliseconds since epoch) to UTC microseconds.
- REST fallback: `/api/v3/ticker/bookTicker` (snapshot, not history) and
  `/api/v3/aggTrades?symbol=BTCUSDT&startTime=...&endTime=...` (paginated,
  1000 rows per call, up to ~4 days of 5-min windows per hour of fetch time).
  Use the archive above; REST pagination is rate-limited and slow at scale.
- **Precision note.** The archive bookTicker files carry millisecond-level
  `transactTime`; the live SBE stream carries microsecond `eventTime`. For
  the feature computation in §3 (`move`, `ofi_sum`, `imbalance`) millisecond
  precision is sufficient — the look-back window is 220 ms–1800 ms. The
  latency penalty in §4 must add an extra 1 ms bias term when using archive
  data to account for the coarser clock.

*Polymarket historical data*:

- **Gamma API** (`https://gamma-api.polymarket.com/markets`): returns all
  resolved markets with `slug`, `condition_id`, `question`, `startDate`,
  `endDate`, and `outcomePrices` (final settlement). Filter by
  `slug LIKE 'btc-up-down-5m%'` or equivalent; paginate with `offset` /
  `limit`. This gives the full population of resolved btc-updown-5m markets
  and their settlement outcomes (the `y_t` label).
- **CLOB data API** (`https://data-api.polymarket.com`): provides historical
  order-book snapshots and matched-trade records by `condition_id` and time
  range. Use `/book?token_id=...&timestamp=...` for book snapshots and
  `/trades?taker_order_id=...` or `/trades?market=...&startTs=...&endTs=...`
  for matched trades.
- **Alignment.** Join Polymarket events to Binance archive rows by UTC
  timestamp; the Polymarket WS frame's `timestamp` field is Unix seconds
  (integer); convert to microseconds before aligning with the Binance
  bookTicker `transactTime`. Expect up to ~200 ms of clock drift between
  venues — model the drift as a zero-mean Gaussian nuisance and do not
  attempt to correct it with interpolation.
- **Settlement outcome source.** Do not derive outcomes from `outcomePrices`
  alone; cross-check against the `resolved` field and the Polymarket
  reference feed. Markets occasionally list `outcomePrices=[1,0]` before
  official settlement; use only markets where `closed=true` AND
  `resolutionSource` points to the Polymarket reference oracle.

**Recommended retrospective workflow**:

1. Download Gamma API index → parse condition_ids and settlement labels for
   all resolved btc-updown-5m markets in the target date range.
2. Download Binance bookTicker archive for the same date range.
3. Replay each 5-minute market window: align Polymarket CLOB snapshots to
   Binance ticks, compute feature vectors at each candidate decision point,
   label with settlement outcome.
4. Apply data hygiene gates. A dataset built this way can cover 6–12 months
   of market history in an afternoon of compute, yielding 100 k+ decision
   events against the 20 k minimum — a substantial statistical improvement.
5. Record `provenance.dataset_hash` over the union of Binance archive files
   and Gamma API market ids used; this makes the dataset reproducible without
   re-downloading.

**Data hygiene gates** (any failure invalidates the dataset):

- DST and leap-second handling: store all timestamps as UTC microseconds
  since epoch.
- Gap detection: contiguous 5-minute markets in `MARKET_SLUG_FMT` with no
  gap longer than 30 s of book updates per side.
- Resolution outcomes recorded for ≥99% of in-scope markets.
- A reproducible content hash (SHA-256 over the sorted set of
  `(market_id, last_update_id, ts_us)` tuples) recorded as
  `provenance.dataset_hash` in the model file.

**Forbidden practices:** patching missing book updates by interpolation,
forward-filling settlement outcomes, dropping markets that "look weird".
If a market's data is broken, exclude it whole and record the exclusion
reason in the run log.

---

## 3. Feature design

The runtime computes three features in `_maybe_signal`:

- `move = microprice_now - microprice_oldest_in_window`
- `ofi_sum` = order-flow imbalance summed over the look-back window,
  using the per-tick OFI defined in `_compute_ofi`.
- `imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty)` at the latest
  tick.

We deliberately do **not** add trade-flow / aggressor classification on
the calibration loop — those are confounded with the very BTC price
moves we are trying to predict and give a model with sky-high in-sample
fit and zero out-of-sample edge. Stick to the three book features above
unless a feature passes its own ablation gate (§6.4).

**Feature snapshotting.** Each candidate decision event freezes a feature
vector at the *exact moment* the engine would call `decide_buy`. This must
include:

- `event_time_us`, `update_id` (from Binance)
- `microprice`, `bid`, `ask`, `bid_qty`, `ask_qty`, `move`, `ofi_sum`,
  `imbalance`, `spread`
- Polymarket: `yes_bid`, `yes_ask`, `yes_mid`, `no_bid`, `no_ask`, `tick`,
  `quote_age_us`, `tte_us`, `polymarket_strike`, `condition_id`
- Latency: `binance_event_to_engine_us`, `polymarket_event_to_engine_us`

The training pipeline must drop any event whose Polymarket
`quote_age_us > MINIMAL_DECISION_MAX_QUOTE_AGE_US` — the live runtime
already filters those, and including them inflates measured edge.

---

## 4. Decision-event sampling

A decision event is a hypothetical buy decision: at time `t`, the
production code paths produce a `BinanceSignal` with `side ∈ {YES, NO}`
and `signal.strength` above some threshold. We sample with the following
rules to avoid look-ahead and selection bias:

1. **No future leakage.** A decision event uses only Binance data with
   `event_time_us ≤ t`, plus the *latest* Polymarket quote with
   `quote.ts_ns ≤ t`. Never use a quote that arrives later, even by
   microseconds.
2. **Independence within market.** Sample at most one decision event per
   market per side. The 5-minute window contains autocorrelated
   features; multiple events bias variance estimates downward.
3. **Conservative trigger.** Sample any event the engine would consider
   firing (i.e. before the `min_strength` and `min_edge` gates). The
   calibration job is precisely to learn the right cut-points.
4. **Realistic fills.** When labelling P&L, simulate execution using the
   Polymarket FAK semantics:
   - Limit price = `ceil_to_tick(ask + entry_slippage)` (entry).
   - Fill = the depth available at-or-better at the moment of submit,
     capped by `usdc_per_trade / limit_price`.
   - Latency penalty = `binance_event_to_engine_us` + assumed network
     RTT (default 25 ms) + assumed venue ack 10 ms; if the simulated
     submit lands after `quote.ts_ns + max_quote_age_us`, the order is
     rejected by `quote_stale` and that event contributes zero to P&L.

---

## 5. Cost model

A model that ignores trading costs will overstate edge by 10× or more on
this venue. The protocol mandates accounting for **every** of the
following before computing realized edge:

| Cost item | Default | How to measure |
|---|---|---|
| Polymarket fees | Verify the current schedule at `https://docs.polymarket.com/trading/fees` before each calibration run. Binary 5-minute markets have historically carried 0 bps maker fees; taker fees and any volume-tier rebates must be re-checked per run because the schedule changes without notice. | Per-trade |
| Effective spread | Top-of-book ask minus mid at execution | Per-event from book |
| Tick rounding loss | `ceil(ask + slippage, tick)` minus `ask + slippage` | Per-event |
| Adverse selection | Mean P&L of *queued, missed* fills (orders that cleared the queue) | Bootstrap from fill simulator |
| Inventory carry | If we hold past 1 minute, expected mark-to-market drift | Per-event |
| Settlement basis risk | `polymarket_settle - binance_microprice_at_settle` | Per-market — capture |

Net edge is `(realized_pnl_per_share - all_costs_per_share) / cost_basis`,
expressed in bps. Report **net edge**, never gross. The runtime guard
`provenance.holdout_net_edge_bps` is a *net* number.

---

## 6. Acceptance gates

The calibrated model file must satisfy **all** of:

### 6.1 Sample count

`provenance.sample_count ≥ 20,000` decision events (after the
quote-staleness filter).

### 6.2 Hold-out split

A purged, embargoed, time-ordered split:

- Train: calendar weeks 1..N-2 of the dataset.
- Embargo: 24 hours.
- Hold-out: weeks N-1..N (≥ 7 days, ≥ 5,000 events).

Selecting tuning hyperparameters on the hold-out is forbidden. Use a
nested CV (k=5) on the train fold for hyperparameter selection.

### 6.3 Calibration

Reliability diagram on the hold-out, bucketed by predicted probability in
deciles. Reject the model if any decile with ≥30 events deviates from the
realised rate by more than 5 percentage points (95% bootstrap CI from the
empirical rate).

### 6.4 Discrimination

`provenance.holdout_auc ≥ 0.55` and `provenance.holdout_brier ≤ 0.245`
(uniform baseline for a `p=0.5` market is `Brier=0.25`; we require a
measurable improvement on the hold-out fold).

### 6.5 Net edge after costs

`provenance.holdout_net_edge_bps ≥ 5` on the hold-out fold, computed per
the cost model in §5. The 5 bps floor is a margin against unmodeled
costs. Models that show 1–4 bps net edge are *not* deployable; they will
not survive a real-time fee schedule change or a single bad week.

### 6.6 Stability across regimes

Split the hold-out by realised volatility quartile of the underlying
(Binance 5-minute realised vol). The model must show non-negative net
edge in *each* of the lowest three quartiles. The top-vol quartile is
allowed to be negative — markets disagree wildly during shocks and our
exit policy is the right defence.

### 6.7 Latency robustness

Re-run the simulator with the assumed engine-to-venue latency increased
by +50 ms. The model must still produce non-negative net edge. If a 50
ms latency hit collapses edge, the model is too dependent on speed to be
operated honestly outside a colocated environment.

### 6.8 Reverse-side sanity

The model must show positive edge when the buy decision side is *the
opposite* of the produced signal only by chance. A two-sided binomial
test for `n_signals > 100` with realised buy-side outperforming flipped
side at p < 0.05 is required. This catches mismatched contracts (YES/NO
swapped), which are the most expensive bug we can ship.

---

## 7. Output schema

The pipeline writes `signal_model.json`. Format (`schema_version=1`):

```json
{
  "schema_version": 1,
  "provenance": {
    "fit_at": "2026-05-01T12:34:56Z",
    "dataset_hash": "sha256:<64-hex>",
    "sample_count": 24380,
    "holdout_auc": 0.612,
    "holdout_brier": 0.221,
    "holdout_net_edge_bps": 7.4,
    "notes": "purged k=5; embargo 24h; vol-bucket all bottom 3 positive"
  },
  "decision": {
    "min_strength": 4.8,
    "min_edge": 0.012,
    "strength_price_scale": 0.024,
    "max_quote_age_us": 220000,
    "min_tte_us": 2000000,
    "max_ask": 0.55
  },
  "signal_engine": {
    "min_abs_move": 0.65,
    "min_abs_ofi": 1.2,
    "min_imbalance": 0.10,
    "max_spread": 1.5,
    "min_window_us": 220000,
    "max_window_us": 1800000,
    "signal_cooldown_us": 1500000,
    "cooldown_side_agnostic": true
  }
}
```

`signal_model.load_calibrated_model` parses this strictly. Missing fields
in `decision` and `signal_engine` mean *no override* (env defaults stand);
either block must contain at least one override or the file is rejected.

---

## 8. Deployment kill-switches

These are enforced by the runtime, not by the calibration pipeline. They
exist because every fitted model has a non-zero probability of being
silently wrong about live conditions.

1. **Daily realised-loss cap.** If realised P&L breaches `-X bps` of
   capital deployed since the start of the trading day, the bot must
   refuse new BUYs and only allow exits. (Implementation: TODO; not yet
   in the runtime — track in a follow-up.)
2. **Modeled-vs-realized drawdown gate.** If the realised hit-rate over
   the last `n=200` decisions deviates from `holdout_auc`-implied
   expectations by >2σ, refuse new BUYs.
3. **Stale-model gate.** If `provenance.fit_at` is older than 21 days,
   the runtime warns; older than 60 days, it refuses to start unless
   `MINIMAL_ALLOW_STALE_MODEL=true`.
4. **Always-on**: `MINIMAL_REQUIRE_CALIBRATED_MODEL=true` in any live
   deployment. Setting it to `false` outside cold plumbing tests is a
   policy violation.

These switches are deliberately blunt. The point of a kill-switch is not
to optimise; it is to stop us from compounding a bad assumption.

---

## 9. Operating cadence

- Re-fit weekly. Diff the new `provenance` block against the previous
  one and require a code-owner sign-off if any of `holdout_auc`,
  `holdout_brier`, or `holdout_net_edge_bps` regresses by more than 1%.
- Quarterly: run a full feature ablation on the production dataset.
  Drop features whose contribution to net edge is below 1 bp.
- Annually: re-derive the cost model. Polymarket fee schedules and the
  underlying queue dynamics evolve.

---

## 10. What we are not claiming

- We are not claiming the bot is profitable. We are claiming that we
  measured a positive net edge on a reproducible hold-out fold and put
  the bot live with kill-switches that bound how wrong we can be.
- We are not claiming the calibrated model generalises beyond
  *btc-updown-5m* on Polymarket. Other markets, slugs, or settlement
  windows require their own calibration runs.
- We are not claiming the Binance microprice is the true BTC price. It
  is one of several venues; cross-venue basis is a real risk and the
  basis estimator is an audit input, not a model parameter.

The protocol exists to make it expensive to ship a bad model and cheap
to keep shipping good ones.
