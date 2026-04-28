# Moneyline Engine — Future Improvements (Phases 3 & 4)

This document records the next two phases of moneyline-engine work. **No code from these phases has been implemented.** They are deliberately deferred — see "Gating Rule" below — and exist here as a planning artifact.

> **GATING RULE — DO NOT START THESE PHASES UNTIL ALL OF THE FOLLOWING ARE TRUE:**
>
> 1. The Phase 1 backtesting framework (`apps/core/services/backtesting_service.py`, `BacktestRun`, `/backtest/`) is in place and operational on production data.
> 2. The Phase 2 intelligence layer (decision quality, where-is-my-edge buckets, system verdict) is in place and operational on production data.
> 3. Dynamic Elo ratings (the *separate* Phase 2 in the original plan — auto-updating team ratings replacing the static `team.rating`) are implemented, with `USE_DYNAMIC_RATINGS=True` flipped after backtest validation.
> 4. We have **at minimum** a multi-week production backtest run that demonstrates the calibration framework is producing trustworthy, non-approximate results — i.e. the run is not flagged `is_approximate=True` because most games have stored `ModelResultSnapshot` history.
> 5. Calibration and ROI improvements from the static-vs-dynamic-rating swap have been validated through the backtest harness (a STRONG or NEUTRAL verdict, not WEAK).
>
> Until those five conditions are met, every change below is speculation. Adding more inputs to a model that hasn't been calibrated yet is just adding more variables to chase — it doesn't tell you whether your edge is real.

---

## Phase 3 — Use Existing Signals We Already Collect

These are wins we can capture without adding any new external data sources. Every signal listed already lives in the database; we're just not feeding it into the recommendation engine.

### 3.1 Line Movement (OddsSnapshot history)

**Why it matters.** Line movement against the public is one of the most-cited public-edge signals. When the moneyline drifts toward the away team while 70% of tickets are on the home team, sharp money is on the away side. We already capture multiple `OddsSnapshot` rows per game (CLV pipeline depends on this), but the recommendation engine reads only the latest snapshot — the historical movement is invisible to the model.

**How it would integrate.**
- Add a `MovementFeatures` helper in `apps/core/services/` that, given a game, computes:
  - opening-to-closing moneyline delta in cents
  - sign-aware direction (line moved with vs against the implied home prob)
  - significance threshold cross (already partially tracked via `OddsSnapshot.movement_class` from the existing odds_movement service)
- Surface `movement_score` as a soft probability nudge in `_compute_win_prob` (CFB/CBB/MLB/CB), gated by a `LINE_MOVEMENT_WEIGHT` config. Default the weight to 0 until backtest validates a positive contribution.
- Stays in the existing model-service layer; no new dependencies; one new helper module.

**Estimated complexity.** Small — ~1 day. The data plumbing is already there from CLV.

**Expected impact.** Moderate. Most useful in markets with high public-vs-sharp split (CFB primetime, MLB primetime). Likely 1-2pp lift in moneyline ROI in the affected subset; smaller across the full slate.

---

### 3.2 Multi-Book Consensus & Line Dispersion

**Why it matters.** Today we persist one `OddsSnapshot` per game per provider pull, with `sportsbook` set to whichever book happened to be picked. Variance across books is itself a signal — when DraftKings has -150 and FanDuel has -130 for the same side, the wider dispersion implies less efficient pricing and more model edge to capture by line-shopping or trusting the soft-side book.

**How it would integrate.**
- Extend the odds providers to write *all* bookmakers seen in a single Odds API response (currently only the first/preferred is kept per market).
- Add a `consensus_market_prob` helper that averages de-vigged probs across bookmakers, weighted by liquidity ranking.
- Add a `line_dispersion` field to `OddsSnapshot` (or a new `MarketSummary` row) capturing the spread of moneylines across books for the picked side.
- Recommendation engine treats high dispersion as a *confidence* multiplier, not an edge multiplier — it makes existing edges more trustworthy rather than fabricating new ones.

**Estimated complexity.** Medium — ~2-3 days. The provider rewrite touches all 5 odds providers (cfb, cbb, mlb, mlb-espn, college_baseball) and requires a migration to allow multiple OddsSnapshots per (game, captured_at).

**Expected impact.** Moderate-to-high. Multi-book consensus is more reliable than single-book; dispersion as a confidence signal would reduce false positives on thin-market games where the single book happens to misprice.

---

### 3.3 Position-Weighted Injury Impact

**Why it matters.** `InjuryImpact.notes` already stores the player name and status verbatim ("Patrick Mahomes: Out (concussion)"). The recommendation engine bucketizes that into low/med/high before reading it, throwing away the position. A QB-out is a vastly larger impact than a 3rd-string LB-out, but they hit the same `'high'` bucket today.

**How it would integrate.**
- Parse the position out of `notes` text using a small extraction helper (regex + position dictionary). Persist parsed position as a new field on `InjuryImpact`.
- Build a `POSITION_WEIGHTS` config dict per sport:
  - CFB: QB=3.0×, RB1=1.5×, edge rusher=1.4×, etc.
  - CBB: PG=2.0×, C=1.7×, role players ~1.0×
  - MLB: closer=1.8×, ace starter=2.5×, bench bat=1.0×
- Multiply the existing `impact_values` lookup by the position weight in `_injury_adjustment`.
- Required minimal data; degrade gracefully when position unparseable (fall back to current bucket math).

**Estimated complexity.** Medium — ~2 days. The parsing is the time sink; the integration is one line in each model_service.

**Expected impact.** High in CFB/CBB where star-player concentration is extreme; lower in MLB where the impact is mostly through pitcher (already weighted).

---

### 3.4 Mock Bet Feedback Loop (CLV + Outcomes)

**Why it matters.** We have a labeled dataset of every recommendation the system has emitted (`MockBet` + `BettingRecommendation`), with CLV signal and outcome. The new backtesting framework consumes this data for measurement, but the recommendation engine itself does not — it has no way to learn from its own track record. A category that consistently loses (e.g. "heavy road dogs in CBB") should be downweighted automatically.

**How it would integrate.**
- Extend the backtest service to expose per-category aggregates (sport × tier × edge bucket × fav-vs-dog) as a queryable cache.
- Add a `recommendation_trust_modifier` step in `_moneyline_candidate` that, for the picked side, looks up the historical performance of comparable bets and adjusts the displayed tier or edge accordingly.
- Critically, this must be guarded with a minimum-sample threshold (e.g. 50+ historical bets in the category) and must NOT modify the underlying probability — only the surfaced confidence/tier label. Otherwise we'd compound noise.

**Estimated complexity.** Medium — ~3 days. The bigger risk is correctness (sample-size guarding, no double-counting against itself).

**Expected impact.** High over time, low in the near term. Becomes more powerful as the dataset grows; under ~1000 settled bets the modifier likely defaults to "no change" for most categories.

---

## Phase 4 — New Data Sources

External data we don't ingest today. Higher payoff but higher build cost.

### 4.1 Rest / Travel / Schedule Density

**Why it matters.** Days since last game, second night of a back-to-back (CBB), short-week games (Tue/Thu CFB), time-zone crossings — these are all documented edges. CBB second night on the road is a ~3pp win-rate hit. None of this is currently fed into the model.

**How it would integrate.**
- New service `apps/core/services/schedule_features.py` with `compute_rest_features(game)` returning `{home_days_rest, away_days_rest, home_b2b_road, away_b2b_road, home_tz_delta, away_tz_delta}`.
- Reads existing `Game` rows ordered by start time per team — no external API needed.
- Multiply edge by `(1 + REST_WEIGHT × delta)` in the model service. Per-sport weight defaults to 0 until backtest validates.

**Estimated complexity.** Small-medium — ~2 days. All data is local; the math is simple date arithmetic.

**Expected impact.** Moderate. Most pronounced in CBB (B2B effect), CFB (short weeks), MLB (long road trips and getaway days).

---

### 4.2 Bullpen + Lineup Quality (MLB / College Baseball)

**Why it matters.** The MLB house model weights starters at 65% — but a great start gets erased by a shaky bullpen. We have starter ratings but no view into the bullpen behind them, and no view into batting order quality vs the opposing handedness.

**How it would integrate.**
- New `BullpenStats` model in `apps/mlb/models.py` (and similar for college_baseball when data becomes available) — team-level rolling 30-day bullpen ERA, WHIP, save conversion rate.
- New `LineupSplit` table — team-level OPS vs LHP, vs RHP.
- Provider: MLB Stats API (free, already in our stack via `apps/datahub/providers/mlb/pitcher_stats_provider.py`). Endpoints: `/teams/<id>/stats` filtered to `relievers` group; `/teams/<id>/stats/splits`.
- Model service integration: redistribute the 65% starter weight to 50% starter + 15% bullpen; add a 5-10% lineup-vs-handedness term.

**Estimated complexity.** Medium — ~3-4 days. The provider work is the bulk; the model integration is a one-line change per file.

**Expected impact.** High for MLB. The current 65% starter weight is overstated relative to bullpen contribution; correcting this should improve both calibration and ROI.

---

### 4.3 Sharp vs Public Betting Splits

**Why it matters.** When 70% of tickets are on the home team but the line moves *toward* the away team, the sharp money is on the away side. This is the single most-cited public-edge signal in sports betting and we have no view into it today.

**How it would integrate.**
- Vendor: ActionNetwork API or Pregame.com (paid, ~$50-100/mo). Both expose handle vs ticket percentages per game per market.
- New model `BettingSplit` with `(game, market, handle_pct_home, ticket_pct_home, captured_at)`.
- New provider in `apps/datahub/providers/<sport>/betting_splits_provider.py`.
- New helper `compute_rlm_signal(game)` that detects reverse line movement: line moves opposite the public ticket %.
- Surface as a confidence multiplier in the recommendation engine, similar to line movement.

**Estimated complexity.** Medium-large — ~4-5 days. Vendor onboarding + provider work + model integration + new billing relationship.

**Expected impact.** High. Decades of bettor literature suggest this is the strongest public signal. Likely 2-4pp lift in moneyline ROI on the sharp-flagged subset.

---

### 4.4 Weather (CFB primarily)

**Why it matters.** Wind > 15mph and rain meaningfully affect CFB outcomes — totals collapse, and the run-heavy team gains a small moneyline edge. Free data via NWS or Open-Meteo. Not used today.

**How it would integrate.**
- New `GameWeather` model with `(game, temperature_f, wind_mph, wind_dir, precipitation_pct, conditions)`.
- New provider via Open-Meteo (free, no API key) keyed by stadium lat/lon.
- Stadium coordinates table seeded once.
- Model adjustment: small probability nudge for weather-favored team in CFB; minor effect in MLB; no effect for CBB or indoor venues.

**Estimated complexity.** Medium — ~3 days. Stadium coordinate seeding is the time sink; the API integration is straightforward.

**Expected impact.** Low-moderate overall, high in the affected subset (CFB outdoor games with extreme weather, ~5-10% of slate).

---

### 4.5 Park Factors (MLB)

**Why it matters.** Coors Field vs Petco Park changes the run environment by ~1.5 runs/game. A high-run park favors the offensive team; a low-run park favors the better pitcher. The MLB house model treats every park identically today.

**How it would integrate.**
- Static data table `ParkFactor` seeded once per season from Baseball Reference or FanGraphs (both publish).
- Single-field lookup on `Game.home_team` → park factor.
- Model: redistribute small portion of pitcher/lineup weight based on park-relative run environment.

**Estimated complexity.** Small — ~1 day. One seed file, one helper, three lines in the MLB model service.

**Expected impact.** Low overall, moderate in extreme-park games (Coors, Fenway, Marlins Park).

---

### 4.6 Tempo / Efficiency Metrics (CBB)

**Why it matters.** A single team rating compresses too much. KenPom-style adjusted offensive and defensive efficiency, plus pace, captures the "slow defensive team beats fast offensive team" matchups that the current model misses.

**How it would integrate.**
- Vendor: KenPom (paid, ~$25/yr) or BartTorvik (free, scrapeable).
- New `TempoRating` table per team per season: `adj_oe`, `adj_de`, `adjusted_tempo`.
- Replace current single-rating CBB calculation with a four-factors-derived expected score: `home_oe × away_de × shared_tempo`.
- Substantial rewrite of `apps/cbb/services/model_service.py::_compute_win_prob`.

**Estimated complexity.** Medium-large — ~4-5 days. Provider work plus model rewrite plus regression testing against the existing simpler model.

**Expected impact.** High for CBB. The current single-rating model is the weakest link in the CBB pipeline; tempo-adjusted efficiency is the established standard.

---

## Recommended Sequencing After Gating Conditions Are Met

If the gating rule clears, I'd build in roughly this order — fastest-payoff first, biggest-builds last:

1. **3.3 Position-weighted injuries** — high impact, small build, no new dependencies.
2. **3.1 Line movement** — already-collected data, small build.
3. **4.1 Rest/travel** — local data, simple math, broad sport applicability.
4. **4.2 Bullpen/lineup quality (MLB)** — fixes the most-overstated weight in the system.
5. **3.2 Multi-book consensus** — provider rewrite is the cost; payoff scales with slate size.
6. **4.5 Park factors (MLB)** — tiny build, completes the MLB picture.
7. **4.6 Tempo metrics (CBB)** — biggest CBB win but largest model rewrite.
8. **4.3 Sharp/public splits** — highest pure-edge potential but requires vendor relationship.
9. **4.4 Weather** — modest impact, save for when the weather-affected slate is the bottleneck.
10. **3.4 Mock bet feedback loop** — works best with the most data, naturally last.

Phase 3 items can run in parallel with Phase 4 items since they touch different parts of the codebase. The order above is a single-track suggestion — actual sequencing should be revisited after Phase 1+2+Elo are validated and we know which subset of the slate the current model is weakest on.
