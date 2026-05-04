  Polymarket Microstructure Mechanisms — Actionable Report for Minimal Bot

  Source: Dubach (2026), "The Anatomy of a Decentralized Prediction Market." 30B WebSocket events, 255M on-chain fills, 600-market pre-registered panel. Window: 2026-02-28 to 2026-03-27. This is the
  authoritative empirical picture of the venue the bot trades on.

  ---
  Mechanism 1: The Spread Surface (SF1, Table 1)

  Finding: Quoted half-spread varies by 34× across the price range — from 53 bps at 0.90+ to 1,818 bps below 0.10.

  Precision data (median quoted spread in bps, by mid-price decile):

  ┌────────┬───────────┬─────────────────────┬────────────────────────┐
  │ Decile │ Mid Range │ Median Spread (bps) │ Half-Spread Cost on $1 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 0      │ 0.00–0.10 │ 1,818               │ $0.091                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 1      │ 0.10–0.20 │ 1,339               │ $0.067                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 2      │ 0.20–0.30 │ 755                 │ $0.038                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 3      │ 0.30–0.40 │ 2,581               │ $0.129                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 4      │ 0.40–0.50 │ 400                 │ $0.020                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 5      │ 0.50–0.60 │ 400                 │ $0.020                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 6      │ 0.60–0.70 │ 444                 │ $0.022                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 7      │ 0.70–0.80 │ 425                 │ $0.021                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 8      │ 0.80–0.90 │ 222                 │ $0.011                 │
  ├────────┼───────────┼─────────────────────┼────────────────────────┤
  │ 9      │ 0.90–1.00 │ 53                  │ $0.003                 │
  └────────┴───────────┴─────────────────────┴────────────────────────┘

  Bot implications:

  - MINIMAL_MIN_BUY_LIMIT=0.10 is at the edge of a cliff. At 0.10–0.20, the half-spread alone costs $0.067 per $1 trade. The signal edge must exceed 6.7% just to break even on spread. Below 0.10, it's 9.1%.
  The current min_edge=0.05 is below the median spread cost for all deciles below 0.40. The bot cannot be profitable buying tokens below ~0.35 unless it captures extraordinary edge.
  - Decile 3 (0.30–0.40) is anomalous — 2,581 bps, worse than 0.20–0.30. The paper attributes this to inventory-risk premia for makers. Avoid this zone.
  - The sweet spot is 0.40–0.60: 400 bps median spread, tight IQR. This is the "even odds" range. Half-spread cost is 2% — the 0.05 edge floor clears this with 3% net margin.
  - Above 0.80, spreads collapse. But these are "near-certainty" tokens where the directional signal has almost no edge (small move_from_strike).

  Actionable rule: The dual gating at 0.50 (cheap vs expensive) should be re-centered. The real spread discontinuity is at 0.30, not 0.50. Tokens below 0.30 face spreads that exceed the current min_edge. The
  bot should either raise min_edge dynamically with the spread surface or hard-floor entries below 0.30.

  ---
  Mechanism 2: Depth Is NOT Concentrated at Top-of-Book (SF2)

  Finding: Median L1/L10 depth-concentration ratio = 0.137. The uniform-grid benchmark is 0.10. Depth is layered evenly across levels, not piled at the inside quote.

  Precision data: p10 = 0.033, p90 = 0.428. Only a small right tail of markets has top-of-book concentration approaching 1.0.

  Bot implications:

  - This directly explains the bot's low FAK fill rate. FAK demands immediate match against resting orders. If only ~14% of top-10 depth sits at the inside, and the bot's $1 size may need to eat multiple
  levels, FAK will frequently return "no orders found to match."
  - The bot's FAK fill problem is structural, not parametric. Raising slippage from 0.05 to 0.10 won't fix it — the depth simply isn't there at any single level.
  - Recommendation: Switch entries from FAK to GTC that rest at the inside bid, joining the layered depth profile rather than fighting it. The paper shows depth is evenly distributed — a resting limit order at
   the inside has a reasonable probability of filling as the queue rotates.
  - Alternatively: keep FAK but accept a 14%-ish fill rate as the structural ceiling for single-level takers.

  ---
  Mechanism 3: Adverse Selection Is Zero on Polymarket (Section 6)

  Finding: Glosten-Harris decomposition on the top-100 panel shows median adverse-selection component φ = 0.0 probability points. Median transitory component c = 0.00001. Median effective half-spread =
  -0.0003.

  Precision data (Table 4, first 10 top-100 markets): 8 of 10 have φ = 0.0000 to four decimal places. The two non-zero values are -0.0004 and -0.0077. This is effectively zero adverse selection.

  Bot implications:

  - This validates the bot's core thesis. The spread on Polymarket is a pure transaction cost — inventory compensation for makers, not an information rent extracted from informed traders. You are not trading
  against someone who knows more than you.
  - The decide_buy() gating logic (edge > min_edge) is the correct framework. There is no hidden information-leakage tax to model on top of the spread.
  - Binance-leading-Polymarket is architecturally sound: the Binance signal carries information the Polymarket book hasn't yet absorbed, and there's no adverse-selection penalty for acting on it.
  - The paper's decomposition uses on-chain trade direction (not feed-inferred), so the zero-adverse-selection result is trustworthy, not a measurement artifact.

  ---
  Mechanism 4: Trade Direction From the Feed Is Noise (Section 7)

  Finding: Feed-inferred trade direction matches on-chain ground truth only ~59% of the time. Effective half-spread sign-flips on 67% of top-100 markets when using feed-inferred direction vs on-chain.

  Precision data:
  - Volume-weighted sign agreement: 59.2%, bootstrap 95% CI [54.2%, 65.9%]
  - Panel mean: 61.5%, IQR [52.6%, 68.1%]
  - "Barely above the 50% chance baseline"
  - Mechanism: change_side in the WebSocket feed marks which book side was updated, not which side initiated the trade

  Bot implications:

  - The bot is architecturally immune. It sources signals from Binance tick data, not from Polymarket trade-direction inference. It uses the Polymarket feed only for best bid/ask quotes, which the feed reports
   reliably (post-match book state, not direction-dependent).
  - This is a design validation: the bot's Binance→signal→Polymarket-execution pipeline avoids the central measurement pitfall the paper documents.
  - Any future feature that tries to extract signal from Polymarket trade flow (e.g., "whale alerts," volume imbalance from the feed) would be built on a 59%-accurate foundation — effectively noise. Do not
  build Polymarket-feed-based trade direction features.

  ---
  Mechanism 5: Depth Decay Near Resolution Is Modest (SF8)

  Finding: Within-category slope of log depth on log seconds-to-close = 0.55 (t = 3.85, R² = 0.22). Adding volume as a control drops the slope to 0.305 (R² = 0.49). Volume, not time, is the dominant predictor
  of depth.

  Precision data:
  - Bivariate: slope 0.818 (raw, no controls)
    - Category FE: slope 0.550
    - Category FE + log volume: slope 0.305
  - A 10× reduction in time-to-close → ~6% less mean depth (conservative specification)

  Bot implications:

  - For 5-min BTC markets: going from 300s to 30s (10×) loses ~6% depth. Going from 300s to 45s (6.7×) loses ~4%. This is negligible.
  - The 45s no-entry window is conservative. Depth at 45s is ~96% of depth at 300s. The bot is not missing profitable entries due to depth evaporation.
  - Volume is the real constraint — high-volume markets maintain depth regardless of time-to-close. BTC 5-min is the highest-volume recurring market on Polymarket, so the bot trades the most depth-resilient
  market type.

  ---
  Mechanism 6: Maker Landscape Is Decentralized (SF4)

  Finding: Median maker HHI = 0.031 (~32 effective makers per market). p90 = 0.119 (~8 makers). Max = 0.40 (~3 makers on the thinnest markets).

  Bot implications:

  - On typical 5-min BTC markets, there are dozens of competing makers. No single counterparty controls the book.
  - This supports GTC exit orders: with 32 makers, a resting GTC has multiple potential counterparties, reducing the risk of being picked off or ignored.
  - Thin-tail markets (HHI > 0.2, ~3 makers) should be avoided — those makers have pricing power. BTC 5-min is almost certainly in the dense regime.

  ---
  Mechanism 7: Block Timing Has No Exploitable Pattern (SF3)

  Finding: Quote update timing does not cluster on Polygon block boundaries to any meaningful degree. Median block-alignment share = 0.102, null = 0.10.

  Bot implications: No block-timing arbitrage opportunity exists. The bot should not attempt to synchronize signal timing with Polygon block production. The current architecture (signal fires on Binance tick,
  independent of Polygon blocks) is correct.

  ---
  Mechanism 8: Wash Trading Floor Is Low (SF7)

  Finding: Median self-counterparty wash share = 0.97% (direct-detection lower bound). p90 = 4.5%, max = 22.2%.

  Bot implications: Volume on most Polymarket markets is genuine. The bot does not need wash-trade filters for the markets it trades (BTC 5-min, high-volume). If the bot ever expands to niche/long-tail
  markets, check for elevated wash share.

  ---
  Ranked Action Items

  Critical (loses money)

  1. Spread surface vs min_edge mismatch. min_edge=0.05 (5%) is below the median half-spread cost for ask < 0.30. The bot will enter positions where expected edge is negative after spread. Either raise
  min_edge dynamically using the SF1 table, or hard-floor MINIMAL_MIN_BUY_LIMIT at 0.30 (currently 0.10). Evidence: Table 1 — deciles 0-2 have median half-spread 3.8%–9.1%, all exceeding or approaching the 5%
  edge floor.

  High (structural inefficiency)

  2. FAK fill rate is structurally capped by SF2 depth distribution. Top-of-book holds ~14% of top-10 depth. FAK can only match against resting orders at the inside. The bot's sub-20% FAK fill rate is not a
  tuning problem — it's a depth-distribution problem. Switch entries to GTC that join the layered depth, or accept the 14% structural ceiling.

  Medium (design validation)

  3. Architecture validated. The Binance→signal→Polymarket pipeline avoids the paper's central finding (Section 7: feed-based trade direction is 59% accurate). The bot does not depend on Polymarket trade
  direction for decisions. This architecture should be preserved as a design invariant.
  4. Adverse selection is zero (Section 6). The edge > spread gate is the correct decision rule. No hidden information-asymmetry cost needs to be modeled. The bot's probabilistic framework (Brownian barrier
  crossing) is the right level of sophistication — don't add adverse-selection layers.
  5. 0.40–0.60 is the optimal entry zone. Tightest spreads (400 bps), deepest liquidity, highest volume. The bot's dual gate (cheap/expensive at 0.50) is well-placed but the "cheap" floor should be 0.30, not
  0.10.

  Low (no action needed)

  6. 45s no-entry window is conservative. SF8 shows ~4% depth loss at 45s vs 300s. Could be tightened to 30s or even 20s for BTC 5-min without material depth risk.
  7. Polygon block timing has no edge. SF3 null result. No synchronization needed.

  ---
  What NOT To Do

  - Do not build Polymarket-feed trade-direction features (volume imbalance, aggressor classification). The feed's change_side field carries no reliable direction signal. Section 7 proves this at scale.
  - Do not add adverse-selection models. Section 6 proves φ ≈ 0 on Polymarket.
  - Do not chase block-timing strategies. SF3 is a null result.
  - Do not trade tokens below 0.30 without a spread-aware edge model. The current min_edge=0.05 is subsumed by spread cost alone.