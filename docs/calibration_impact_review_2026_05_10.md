# Calibration Impact Review — 2026-05-10 (Phase 1B Task 7)

**Charge:** answer "what changes when MLB switches from static `Team.rating` to dynamic Elo?", along the four axes the user named — probability distribution, edge distribution, recommendation count, CLV expectation — and decide whether Elo naturally fixes (a) fake giant edges, (b) short-favorite leakage, (c) stale-team problems WITHOUT additional threshold tightening.

**Approach:** combine three sources of evidence:
1. **Mathematical first principles** — what the formula change implies before any data is examined.
2. **Live shadow data** — `BettingRecommendation.shadow_alt_data` populated by Phase 1B Task 5; surfaced by `/analytics/shadow-review/`.
3. **Backtest pair** — run the existing static and Elo backtests via `/analytics/backtest/` and compare summary JSON.

This document is the **analytical framework**. The numerical answers fill in once the shadow data accumulates and the backtests run. The Phase 1B Task 8 production-readiness report consumes this document plus the actual numbers.

---

## 1. Mathematical first principles — what the formula change implies

The MLB score formula is unchanged across modes:

```
score = (rating_home − rating_away) × 0.35 + (pitcher_home − pitcher_away) × 0.65 + HFA
prob_raw = sigmoid(score / 25)              [clamped 0.01..0.99]
prob_blend = (1 − 0.40) × prob_raw + 0.40 × market_home_prob
prob_final = soft_clamp(prob_blend, [0.52, 0.85])
```

The only thing that changes is *which* `rating` is used:
- Static: `Team.rating` (FloatField default 50.0, no updater).
- Elo: `Team.elo_rating` projected via `(elo − 1500) / 13 + 50`.

A 200-point Elo gap projects to 200/13 ≈ 15.4 legacy points. So if the season-to-date Elo of two teams differs by 200 points, the model "sees" a 15.4-point rating difference instead of whatever the static field happens to hold (which has been frozen since seed for most teams).

### 1.1 Probability distribution — predicted direction

**Active (static):**
- For most teams, `rating == 50.0` (the default), or whatever value was seeded once and never updated.
- `rating_home − rating_away` is a near-deterministic function of seed values, not season performance. As a result, the *spread* of model probabilities is structurally narrow for most matchups (small rating diff → small score → final prob clusters near 0.50 → blend pulls it tighter to market).
- For the small subset of teams that did get non-default ratings at seed, the spread is wider but the values are stale.

**Alt (Elo, post-backfill):**
- After season-to-date Elo accumulates, top teams diverge to ~1600+ and bottom teams to ~1400-, producing real 200-point gaps and 15+ legacy-point swings.
- The full operating range of the legacy scale is exercised, not just the seed values.
- Blend with market still applies (40% of final), so the *clamped* probabilities don't go far past 0.85, but the *unclamped pre-blend* values vary much more.

**Predicted axis effect:** Elo widens the predicted-probability distribution. Mean shouldn't move much (sigmoid + clamp + market blend all push toward the center), but variance increases. Picks land in higher-probability bands more often.

### 1.2 Edge distribution — predicted direction

`edge = final_home_prob − fair_market_prob` (de-vigged).

**Active (static):**
- When most teams cluster near rating=50, the model produces probabilities near 0.50–0.55 even on lopsided market lines. The sigmoid + clamp is doing most of the discriminative work the rating field can't.
- Result: when a team is actually strong (and the market knows it), the model says "around 50%" and the market says "65%" → model "edge" against the picked team is 0 to negative.
- When a team is actually weak, same dynamic in reverse — the model says "around 50%" and the market says "30%" → the model "edge" on the team is 20pp, which looks giant but is *entirely an artifact of the model not knowing the team is weak*.
- This is the **fake giant edge** failure mode the user named. It's structurally most common for short-favorite / underdog matchups where one or both teams have stale ratings.

**Alt (Elo):**
- Real season-to-date strength is in the rating, so the score reflects the matchup. The model probability for a strong team is genuinely above 0.50.
- The edge against the market reflects only true model-vs-market disagreement, not the model's blindness to team strength.
- Predicted: **edges shrink in mean and variance**. The 8%+ "elite" bucket (`EDGE_BUCKETS`) thins; the 4–6% bucket thickens.

**Predicted axis effect:** Elo compresses the edge distribution toward smaller, more credible values. Specifically, the top edge bucket should drain into the middle.

### 1.3 Recommendation count — predicted direction

The decision gates (`compute_status`):
- `MIN_EDGE = 6.0` — minimum edge in pp.
- `MIN_PROBABILITY_FOR_RECOMMENDED = 0.60` — final probability ≥ 60%.
- `MAX_ABS_ODDS_FOR_RECOMMENDED = 300` — no longshots.
- Heavy-favorite juice gate at `HEAVY_FAVORITE_ODDS = -150`.

**Static:** the model produces many edges due to staleness (per 1.2), but probabilities cluster near 0.50–0.60 due to weak rating differentiation. Result: a meaningful share of edges fail the probability gate; another share fire `value` (high edge, low prob) and surface in their own UI track. Net recommendation count is moderate.

**Elo:** edges shrink (per 1.2) but probabilities sharpen — top picks land at 0.60+ confidently. Result: edges that pass `MIN_EDGE` are also more likely to pass the probability gate. Net effect is ambiguous on count direction:
- Fewer "giant edges" surviving (fewer in elite tier).
- More "real edges" surviving the joint gate.

**Predicted axis effect:** total recommended count shouldn't change much, but composition shifts — fewer elite, more strong/standard. Lane breakdown: more rows reach `core` (because fewer get the `short_fav_thin` risk flag and fewer get sanity_mismatch, both of which are sensitive to probability quality).

### 1.4 CLV expectation — predicted direction

CLV = `bet_decimal_odds − closing_decimal_odds` for the picked side, computed only from `odds_api`-sourced opening + closing snapshots.

**Why static produces poor CLV:**
- Stale ratings → fake-edge picks → the model recommends a side the market wasn't going to move toward (because the market is tracking *current* form, which the model isn't seeing).
- When the market doesn't move toward the picked side, CLV is near zero or negative.
- This was the original signal that triggered the engineering report's "weak CLV" finding.

**Why Elo should produce better CLV:**
- Real ratings → real edges → picks correlate with actual sharp money flow, because both we and the sharp money are reading the same underlying signal (current team strength).
- Mean CLV trends positive when the model is genuinely picking sides the market is also moving toward.

**Predicted axis effect:** mean CLV improves under Elo. The `positive_clv_rate` should rise from its current ~31% (per the calibration second-pass commentary) toward 50%+. The system_verdict (`STRONG` / `NEUTRAL` / `WEAK` in `backtesting_service._system_verdict`) should shift from `WEAK`/`NEUTRAL` toward `STRONG`.

---

## 2. Direct answers to the user's questions

### 2.1 Will Elo naturally reduce fake giant edges?

**Mathematically, yes.** The fake-giant-edge mechanism IS staleness — the model can't see that one team is strong because the rating is frozen. Elo replaces the frozen rating with a season-to-date rating. The mechanism that produces fake edges is removed.

**Confirmation needed empirically:** the `EDGE_BUCKETS` (`0-4`, `4-6`, `6-8`, `8+`) breakdown in the backtest summary JSON. Static run should have a thicker `8+` tail than Elo. This is observable on the analytics backtest page after running both modes.

### 2.2 Will Elo naturally reduce short-favorite leakage?

**Partially.** The short-favorite band (-149..+99) underperforms because:
1. **Sigmoid is most linear in the middle.** Tiny score differences become tiny probability differences. *This is independent of the rating system.* Elo doesn't fix it.
2. **Stale ratings amplify noise in the danger band.** When two teams' ratings are at default (50.0), the score is near zero, and the band sits exactly on the model's least confident region. *This Elo fixes.*
3. **The market is sharpest in this band specifically.** Even with real ratings, beating the market here is hard. *Elo doesn't change this.*

**Net:** Elo reduces leakage from causes (2) but not (1) or (3). The `short_fav_thin` risk flag (added 2026-05-06) is still useful, and the `MIN_EDGE = 6.0` floor still does its job. **Threshold tightening is NOT required by the Elo cutover, but the existing thresholds remain load-bearing.**

### 2.3 Will Elo naturally reduce stale-team problems?

**This is what Elo IS.** Yes, by definition.

The remaining staleness is at season start (every team is at 1500 = 50, so the first slate looks identical to static). After 2-3 weeks of games, the ratings spread out to reflect early-season performance. Phase 1B's recommendation is to backfill from the season's first day, so today's slate already has a meaningful Elo state.

### 2.4 Without additional threshold tightening?

**Yes — no threshold change is required for the cutover.** The Phase 1A Feature Truth Audit + the engineering report both treat threshold tightening as a separate phase (1D in the report's roadmap). The Elo cutover is a clean single-axis change: rating source. Every gate, every constant, every blend weight, every clamp stays put.

If post-cutover the empirics show that probabilities cluster too tightly (e.g., the soft clamp of [0.52, 0.85] is hit too often and recommendations become a sea of 0.85 picks), THAT would justify a calibration retune — but it's a separate decision based on observed data, not a prerequisite for the cutover.

---

## 3. Empirical questions for the backtest + shadow review

### 3.1 Backtest (static vs Elo, full historical replay)

Run from `/analytics/backtest/`:
- `Run Static Backtest` → produces a `BacktestRun` row with `rating_mode='static'`.
- `Run Elo Backtest` → produces a `BacktestRun` row with `rating_mode='elo'`.

Compare the `summary` JSON across the two:

| Metric path | Direction expected if hypothesis holds |
|---|---|
| `overall.roi_pct` | Elo > static |
| `overall.win_rate` | Elo ≥ static (probability is sharper) |
| `overall.avg_clv` | Elo > static |
| `overall.positive_clv_rate` | Elo > static |
| `system_verdict` | Static = `WEAK` or `NEUTRAL`, Elo ≥ `NEUTRAL` (ideally `STRONG`) |
| `by_edge_bucket['8+'].sample` | Static > Elo (fewer giant fake edges in Elo) |
| `by_edge_bucket['4-6'].sample` | Static < Elo (more credible mid-edges in Elo) |
| `by_fav_size['short_fav'].roi_pct` | Elo > static (Phase 1A new bucket; staleness fix lifts this band) |
| `by_pitcher_completeness['both_real'].roi_pct` | Both modes positive; Elo lifts the `both_default` bucket |
| `calibration_curve` | Elo bins closer to identity (predicted ≈ actual) than static |

The validation block (`validation.evaluated`, `validation.approximate_games`) should be very similar between modes — same set of games, same odds snapshots; only the predicted probability differs.

### 3.2 Shadow review (live recommendations, both modes side-by-side)

Read at `/analytics/shadow-review/` (Phase 1B Task 7 build, this commit).

Key signals:
- `pick_agreement_rate` — modes pick the same side. Expectation: ~85–95%. If <80%, the modes are genuinely different worldviews (interesting); if >97%, Elo is barely changing anything (suspect insufficient backfill or rating divergence).
- `status_recommended_active_only` vs `_alt_only` — net flow direction. Expectation under Elo cutover: more rows move into `recommended` than out.
- `lane_core_active_only` vs `_alt_only` — same for `core` lane.
- `active_edge_pp.mean` vs `alt_edge_pp.mean` — corroborates the edge-compression prediction (1.2 above). Expectation: alt mean is lower than active mean when active is static.

---

## 4. Decision criteria for Task 8 (cutover go/no-go)

The production-readiness recommendation (Task 8) needs a yes/no answer based on evidence. Criteria:

**Hard floor (any single failure → NO-GO):**
- Elo backtest `validation.evaluated` is materially smaller than static (would mean games are silently dropping; bug somewhere).
- Elo `overall.roi_pct` < static `overall.roi_pct` AND `overall.avg_clv` < static `overall.avg_clv`. (i.e., Elo is worse on both money signals — would invalidate the entire premise.)
- `pick_agreement_rate` from shadow review < 60%. (Modes shouldn't disagree this aggressively unless one is broken.)

**Strong support (3+ → GO with confidence):**
- Elo backtest `overall.roi_pct` > static by ≥1 pp.
- Elo `positive_clv_rate` > static by ≥5 pp.
- Elo `system_verdict` is `STRONG`; static is `WEAK` or `NEUTRAL`.
- `by_fav_size['short_fav'].roi_pct` improves under Elo.
- `by_edge_bucket` distribution shifts as predicted (8+ thins; 4-6 thickens).

**Soft support (lean GO):**
- Mixed signals but overall direction is correct, and the tooling is in place to monitor post-cutover.

---

## 5. Pre-cutover checklist

The actual cutover (Phase 1B Task 8) requires this checklist to be green:

- [ ] Backfill ran cleanly (`rebuild_elo_ratings --sport mlb`).
- [ ] `update_elo_ratings` is wired into `refresh_data` cron (✅ done in this commit).
- [ ] Shadow data is being captured on every MLB recommendation (✅ done in Task 5).
- [ ] Static backtest run is recorded in the analytics page.
- [ ] Elo backtest run is recorded in the analytics page.
- [ ] Hard-floor criteria all pass.
- [ ] At least 3 strong-support criteria pass.
- [ ] Rollback procedure verified (toggle `USE_DYNAMIC_RATINGS=False` in Railway env).

---

## 6. What this review does NOT do

- It does NOT propose new gates, new constants, or new weights. Threshold tightening was deliberately deferred per the engineering report's roadmap (Phase 1D).
- It does NOT modify the calibration constants (blend weight 0.40, sigmoid divisor 25, clamp [0.52, 0.85]). Those are what they are; if the Elo cutover changes the empirical distribution enough to require retuning, that's a separate Phase 1D pass with its own evidence + decision.
- It does NOT speak to non-moneyline markets. MONEYLINE_ONLY_MODE is intact; spread/total picks are gated off and not part of this review.
- It does NOT cover non-MLB sports. Phase 1B is MLB-only by spec; CFB/CBB/CB Elo cutovers each get their own calibration review.

---

## 7. Summary

**The math says Elo should:** widen the probability distribution, compress the edge distribution, slightly improve recommendation composition (fewer fake elites, more credible standards), and meaningfully improve CLV.

**The empirical answer comes from:** running both backtests via the analytics page + monitoring the live shadow review for at least one full slate.

**Threshold tightening is not part of the cutover.** The existing gates remain load-bearing; Elo's job is to make the inputs to those gates more honest.

**The Phase 1B Task 8 production-readiness report will:** consume the actual numbers from §3.1 and §3.2, evaluate against §4's criteria, and emit a single GO / NO-GO with monitoring plan.
