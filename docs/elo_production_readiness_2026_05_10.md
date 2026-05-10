# Elo Production-Readiness Recommendation — 2026-05-10 (Phase 1B Task 8)

**Charge:** based on the work in Phase 1A and Phase 1B Tasks 5–7, answer six questions:
1. Is Elo production-ready?
2. Does it materially improve realism?
3. Does it improve edge credibility?
4. Does it reduce overconfidence?
5. Should MLB activate dynamic ratings?
6. What calibration changes would still be needed afterward?

**Context.** This report is written after the structural and instrumentation work has landed but **before any backtest run has been performed against production data**. The recommendation engine, the shadow-mode capture, the segment instrumentation, the analytics surface, and the cron wiring are all in place. What's missing is real numbers. Therefore this report is structured as **a conditional recommendation**: the verdict on each question, plus the empirical thresholds that confirm it.

---

## 1. Is Elo production-ready?

**Verdict: YES — structurally.** Conditionally GO once the empirical evidence (§7) is in.

| Production-readiness axis | Status | Evidence |
|---|---|---|
| Math is correct | ✅ | `apps/core/test_elo_service.py` — 26 tests covering symmetry, conservation, sport-aware behavior, K-factor scaling, margin handling. All passing. |
| Idempotent backfill | ✅ | `rebuild_elo_ratings` wraps everything in `transaction.atomic`; `RebuildIdempotenceTests.test_rebuild_twice_yields_same_final_ratings` locks it. |
| Idempotent incremental update | ✅ | `update_elo_ratings` uses `process_game`'s history-row guard; `test_update_after_rebuild_is_no_op` locks it. |
| Sport-scoped reset | ✅ | `reset_sport('mlb')` doesn't touch CFB/CBB/CB. Locked by `test_reset_only_targets_one_sport`. |
| Sport-aware behavior | ✅ | MLB uses no margin (`MARGIN_AWARE_SPORTS = {'cfb', 'cbb'}`). Locked by `test_mlb_uses_only_winloss_not_margin`. |
| Single-point integration | ✅ | Every sport's `_score()` reads `team_rating_for_model(team)`; that one function decides the mode. No half-states possible. |
| Feature flag | ✅ | `USE_DYNAMIC_RATINGS` (settings + `force_use_dynamic` context manager). Override-aware single source of truth (`is_dynamic_active`). |
| Cron-wired updates | ✅ | `refresh_data` calls `update_elo_ratings` after `resolve_outcomes` (this commit). |
| Shadow-mode capture | ✅ | `BettingRecommendation.shadow_alt_data` populated by `_build_shadow_alt_data` (this commit). |
| Analytics surface | ✅ | `/analytics/backtest/` for full-replay comparison; `/analytics/shadow-review/` for live distribution comparison (this commit). |
| Rollback procedure | ✅ | `USE_DYNAMIC_RATINGS=False` env var. Effective immediately. Documented in `docs/elo_backfill_runbook_2026_05_10.md`. |
| Diagnostic surface | ✅ | `/analytics/model-inventory/` shows the full input → score → calibration → edge → gate trace per game (Phase 1A Task 1). |

**The structural bar is cleared.** The remaining gate is the empirical evidence in §7.

---

## 2. Does it materially improve realism?

**Verdict (mathematical): YES.** Verdict (empirical): pending backtest data.

The **realism case** is the most defensible part of the entire Phase 1B spec. The premise is unambiguous:

- **Static ratings have NO updater.** Confirmed by `apps/core/test_feature_truth_audit.py::StaticTeamRatingHasNoUpdaterTests`. They are seed values, frozen since the season started.
- **Elo ratings have a real updater.** Every final game writes through `process_game` → `Team.elo_rating` advances → next recommendation uses fresh data.
- **The market reads season-to-date strength.** That's what bookmakers price into the line.
- **Today, our model reads frozen strength.** The gap between "what the market knows" and "what the model knows" widens every week. By definition.

Elo closes that gap. It cannot make ratings stale; the structure of the system is incremental updates after every game. That's a *categorical* improvement in realism.

The empirical question is whether the *magnitude* of the improvement is large enough to materially shift behavior on the slates we actually emit. The shadow-review tool answers that: if `pick_agreement_rate` is 99%, the magnitude is small even if the realism is technically improved. If it's 80%, the magnitude is substantial.

**Confirmation criterion:** `pick_agreement_rate` falls in the 75–95% band on the live MLB slate.

---

## 3. Does it improve edge credibility?

**Verdict (mathematical): YES, by removing the fake-edge mechanism.** Verdict (empirical): pending.

The fake-edge mechanism is described in `docs/calibration_impact_review_2026_05_10.md` §1.2: when team ratings are frozen at default, a strong team's model probability sits near 0.50 while the market correctly prices it at 0.65, and the model emits a 15pp "edge" *against* picking that strong team. That edge is illusory — it exists only because the model can't see the team is strong.

Elo eliminates the mechanism. With a real season-to-date rating, the model's probability for the strong team is genuinely above 0.50, and the edge against the market reflects only true model-vs-market disagreement.

**Confirmation criteria** (from the backtest comparison):

- `by_edge_bucket['8+'].sample` (Elo) < `by_edge_bucket['8+'].sample` (Static). Fewer fake elites.
- `by_edge_bucket['4-6'].sample` (Elo) ≥ `by_edge_bucket['4-6'].sample` (Static). More credible mid-edges.
- `by_fav_size['short_fav'].roi_pct` improves under Elo. The danger band benefits from removed staleness.

If both first criteria hold, the edge distribution is cleaner under Elo. If the third also holds, the short-favorite leakage problem is partially solved by the cutover alone (without threshold tightening).

---

## 4. Does it reduce overconfidence?

**Verdict (mathematical): NEUTRAL TO IMPROVED.** Verdict (empirical): pending.

Overconfidence in this context means the calibration curve diverges from the identity line — predicting 80% but winning 65% of those bets, etc.

The factors that produce overconfidence under static:
- A small subset of teams have non-default static ratings (the ones that got seeded with real values). The model is overconfident on those because their stale ratings exaggerate true current strength.
- The sigmoid + clamp produces predictions in [0.52, 0.85] regardless of whether the score is small or huge — at the upper clamp the model is saying "85%" even when its discriminative basis is thin.

Under Elo:
- Every team has a meaningful rating, so the over-rated minority isn't a special case.
- The calibration constants (blend 0.40, sigmoid divisor 25, clamp [0.52, 0.85]) are unchanged. The same overconfidence-mitigation tools are still in place.
- Empirically: more games will produce probabilities that exercise the full clamp range, but the mapping from score-to-probability stays the same.

**Confirmation criterion:** `calibration_curve` from the backtest summary. Under Elo, the predicted-vs-actual curve should be closer to identity than under static. Specifically, the 0.65–0.75 buckets (the moderate-favorite band most affected by staleness) should show actual win rates closer to predicted.

If overconfidence does NOT improve under Elo, the calibration constants need a Phase 1D retune — but that's a follow-up, not a blocker.

---

## 5. Should MLB activate dynamic ratings?

**Verdict: GO — conditional on the §7 checklist passing.**

The structural work is complete. The instrumentation is in place. The math says Elo should help on every axis the user asked about. The risk profile is low because rollback is a single env-var flip with no data migration required.

The recommendation is to:
1. Run the backfill (one command).
2. Run the static and Elo backtests (two button clicks).
3. Compare the summary JSON via `/analytics/backtest/` (two side-by-side cards).
4. Read the live shadow review at `/analytics/shadow-review/` after at least one full slate has been recommended.
5. If the §7 evidence checklist clears, flip `USE_DYNAMIC_RATINGS=True` in Railway env vars.
6. Monitor for one slate. Rollback ready in case anything looks wrong.

**This is a low-risk, high-confidence move.** The conditionality exists because we should never flip a switch on a live betting analytics product without seeing the data first, not because there's structural doubt about the change.

---

## 6. What calibration changes would still be needed afterward?

**Verdict: probably none for the cutover itself; some plausible follow-ups.**

The constants currently in effect (post the 2026-05-03 + 2026-05-06 calibration tunes):
- `MARKET_BLEND_WEIGHT = 0.40` (capped at 0.40)
- `PROB_MIN = 0.52`, `PROB_MAX = 0.85`
- `MLB sigmoid divisor = 25`
- `ELO_TO_LEGACY_DIVISOR = 13`
- `MIN_EDGE = 6.0`
- `MIN_PROBABILITY_FOR_RECOMMENDED = 0.60`
- `EXTREME_DISAGREEMENT_GAP = 0.12 post-blend`

These were tuned against the static-rating distribution. Elo will shift the distribution. Three plausible follow-ups, in order of likelihood:

### 6.1 Sigmoid divisor re-fit (Phase 1D candidate)

The 25 divisor was set when the score formula was producing the static-rating distribution. With Elo, the score is more meaningful, so a different divisor may better calibrate the predicted vs actual win rate.

**Trigger:** if the Elo backtest's `calibration_curve` has predicted noticeably above actual in the upper buckets (model overconfident even after clamp).

**Action:** measure Brier score on a held-out window across a small grid of divisors (e.g., 20, 22, 25, 28, 32). Pick the minimum. One-line constant change.

### 6.2 Market blend weight re-tune (Phase 1D candidate)

The 0.40 blend was set to compensate for the fact that the static-rating model was unreliable. With Elo, the model is more reliable, and 0.40 may now under-trust the model.

**Trigger:** if the Elo backtest's `overall.roi_pct` is positive but lower than expected, the blend may be pulling Elo's good signal too far back toward the consensus market.

**Action:** test 0.30 and 0.35 against the same backtest data. Document the trade-off and pick.

### 6.3 Probability clamp re-tune (Phase 1D candidate)

The [0.52, 0.85] clamp was set to suppress overconfidence on the static distribution. With sharper Elo predictions, the upper clamp (0.85) may be hit too often, flattening recommendations into a sea of indistinguishable maxed-out picks.

**Trigger:** post-cutover, if the operator notices the elite tier has lost its differentiation.

**Action:** test [0.52, 0.88] or [0.52, 0.90] against backtest data; pick whichever produces better calibration in the upper buckets without overshooting.

### 6.4 What is NOT changing

- **Decision gates** (`MIN_EDGE`, `MIN_PROBABILITY_FOR_RECOMMENDED`, `MAX_ABS_ODDS_FOR_RECOMMENDED`, `HEAVY_FAVORITE_ODDS`). These are about *what we recommend*, not *what the model emits*. Elo doesn't touch them.
- **Lane gates** (`LANE_HARD_GATES_*`). Same reasoning.
- **Risk flags** (`market_conflict`, `sanity_mismatch`, `thin_edge`, `insight_conflict`, `short_fav_thin`). All are downstream of probability + edge + odds + market signal — none of them care which rating system produced the probability.
- **CLV tracking.** Source-aware filter (only `odds_api`-sourced opening + closing) is mode-agnostic.

**Net:** the cutover is structurally clean and doesn't require concurrent calibration changes. Any retune is a follow-up driven by post-cutover data.

---

## 7. The empirical evidence checklist

This is what fills in before flipping the flag. The Phase 1A and 1B work exists specifically to produce this checklist.

### 7.1 Pre-cutover state

- [ ] `python manage.py rebuild_elo_ratings --sport mlb` ran successfully.
- [ ] Verification queries from `docs/elo_backfill_runbook_2026_05_10.md` §3 show plausible top/bottom team ratings.
- [ ] At least one MLB slate has been recommended since the backfill, so `BettingRecommendation` rows have populated `shadow_alt_data`.

### 7.2 Backtest comparison (`/analytics/backtest/`)

- [ ] Static backtest run completed (`rating_mode='static'`, `status='completed'`).
- [ ] Elo backtest run completed (`rating_mode='elo'`, `status='completed'`).
- [ ] `validation.evaluated` differs by < 5% between modes (no silent game drops).
- [ ] At least one of:
  - [ ] Elo `overall.roi_pct` > Static `overall.roi_pct`.
  - [ ] Elo `overall.avg_clv` > Static `overall.avg_clv`.

### 7.3 Shadow review (`/analytics/shadow-review/`)

- [ ] `sample` ≥ 30 (enough rows to be informative).
- [ ] `pick_agreement_rate` is in [0.60, 0.97]. Outside that, investigate.
- [ ] `status_recommended_alt_only` ≤ 5× `status_recommended_active_only`. (Sanity: alt mode shouldn't recommend wildly more than active.)

### 7.4 Hard floors (any single failure → NO-GO)

- [ ] Elo backtest's `validation.evaluated` materially smaller than static.
- [ ] Elo `overall.roi_pct` < static AND `overall.avg_clv` < static.
- [ ] `pick_agreement_rate` < 0.60.

### 7.5 Strong support (3+ → GO with confidence)

- [ ] Elo `overall.roi_pct` > static by ≥ 1 pp.
- [ ] Elo `positive_clv_rate` > static by ≥ 5 pp.
- [ ] Elo `system_verdict` is `STRONG`; static is not.
- [ ] `by_fav_size['short_fav'].roi_pct` improves under Elo.
- [ ] `by_edge_bucket['8+'].sample` (Elo) < `by_edge_bucket['8+'].sample` (static).

---

## 8. Cutover procedure

Once §7 clears:

1. In Railway dashboard → Variables → set `USE_DYNAMIC_RATINGS=True`. Save.
2. Railway auto-redeploys. Effective on next request (no DB migration).
3. Verify on production:
   - Visit `/analytics/model-inventory/` for any in-window MLB game. Confirm "Rating mode active: elo" on both team rows.
   - Visit `/analytics/shadow-review/`. After the next slate, the `active_mode` should flip to `elo`.
4. Monitor:
   - One slate worth of recommendations.
   - `/analytics/backtest/` for any anomalies (use most recent Elo run as the reference).
   - `/mockbets/system-tuning/` for the system verdict.
5. If anything looks wrong:
   - Rollback: set `USE_DYNAMIC_RATINGS=False`. One env-var change. Effective immediately.

---

## 9. Post-cutover monitoring plan

- **Day 1**: confirm shadow-review's `active_mode` shows `elo`; live recommendations under Elo.
- **Day 1–7**: spot-check `/analytics/model-inventory/` — does the score formula's "Rating mode active" reflect Elo across teams?
- **Day 7**: re-run static + Elo backtests on the post-cutover window to confirm trajectory.
- **Week 2**: read system-tuning verdict, CLV trend, ROI trend.
- **Week 3**: if anything has trended worse on a sustained basis, rollback and investigate.
- **Week 4**: if all signals are stable, the cutover is locked in. Write a Phase 1B retro (changelog entry) and move on to Phase 1C (real injury term, pitcher recent form).

---

## 10. Conclusion

**Phase 1B is structurally complete.** Math, plumbing, instrumentation, analytics surface, cron wiring, rollback procedure — all in place. The math case for Elo is unambiguous. The empirical case is straightforward to confirm via the existing tooling.

**The recommendation is: proceed to the §7 checklist, then flip the flag.** No additional code is needed before that. No threshold tightening is required for the cutover. The calibration constants stay put.

**The work that remains:**
- Run the backfill in production (one command).
- Run two backtests (two button clicks).
- Read the shadow review (one page load).
- Score the §7 checklist.
- Flip the flag.

Total estimated operator time: 30 minutes plus one slate of waiting for shadow data to accumulate.

This is the cleanest model improvement the project has had since the moneyline-only mode was scoped. The infrastructure was a long-running quiet build (the Elo service has been wired but dormant since at least 2026-04-28); Phase 1B activates it deliberately, with the right evidence and the right safety net.
