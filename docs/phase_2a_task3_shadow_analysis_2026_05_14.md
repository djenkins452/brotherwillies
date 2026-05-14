# Phase 2A Task 3 — Shadow Review Analysis

**Date:** 2026-05-14
**Phase:** 2A Task 3 — pure observation + analysis. No tuning, no signals, no calibration changes.
**Status:** **Hybrid deliverable** — methodology + framework-level reasoning + the analysis tooling that fills in the empirical numbers from production. The dev worktree this report is written in has no production shadow data; the operator runs `/analytics/shadow-review/` on Railway to populate the empirical sections.

---

## 0. The single question this report exists to answer

> *Did dynamic adaptive team strength (Elo) materially improve predictive realism and market alignment relative to static team ratings?*

Everything else in this document is method, evidence, or implication. The answer is conditional: **structurally yes; empirically pending production data**.

---

## 1. Methodology

### 1.1 Data source

Every persisted MLB `BettingRecommendation` since Phase 1B Task 5 shipped carries:

- `shadow_active_mode` — the rating mode that produced the live pick (`'static'` while `USE_DYNAMIC_RATINGS=False`).
- `shadow_alt_mode` — the *other* rating mode (`'elo'` in our current state).
- `shadow_alt_data` (JSONField) — the recommendation that would have been emitted under the alt mode, computed under `force_use_dynamic` inside `persist_recommendation`.

The alt row is generated from the **same game, same odds snapshot, same calibration constants**, with **only the rating source swapped**. That isolation is the whole point: any delta is attributable to the rating swap, nothing else.

A row contributes to the analysis when `shadow_alt_data.elo_available is True` — i.e., both teams have non-null `elo_rating` and the alt computation actually ran. The Phase 2A Task 1 backfill hook (`ensure_elo_backfilled`) guarantees that condition is true on production after the first deploy where it runs.

### 1.2 What we compute

All numbers are produced by the extended `apps.analytics.services.shadow_review` service (Phase 2A Task 3 extensions, this commit) and surfaced at `/analytics/shadow-review/`:

| Question | Metric | Source field |
|---|---|---|
| Q1: Did fake giant edges shrink? | Active vs alt counts at edge ≥ 6 / 8 / 10 pp | `edge_ge_6pp`, `edge_ge_8pp`, `edge_ge_10pp` |
| Q2: Did the recommendation distribution normalize? | Status flips (active-only / alt-only / both / neither) + tier counts | `status_recommended_*`, `tier_counts_*` |
| Q3: Did probabilities become more believable? | Active vs alt count at final_prob ≥ 0.60 / 0.70 / 0.80 / 0.85 | `prob_ge_60/70/80/85` |
| Q4: Did short-favorite aggressiveness reduce? | Scoped review on picks with odds in [−149, +99] | `short_fav` |
| Q5: Did model/market disagreement improve? | Mean disagreement + counts at \|gap\| > 5 / 10 / 15 / 20 pp | `avg_disagreement_*`, `disagreement_gt_*` |
| Q6: Did Elo naturally compress overconfidence? | Same as Q3 + the 0.85 clamp-ceiling band | `prob_ge_85` specifically |
| Q7: Did expected CLV quality improve directionally? | Pick agreement rate + disagreement compression are the leading indicators | `pick_agreement_rate`, `disagreement_*` |

### 1.3 What we *do not* compute here

- **Outcome / ROI / win-rate.** Per user instruction. Samples are too small; outcomes haven't settled; this is a realism-and-calibration pass, not a profit pass.
- **Settled CLV.** Same reason. CLV needs (a) opening + closing snapshots in `odds_api` source and (b) elapsed game time. This report uses disagreement compression as the *leading indicator* of CLV direction. Settled CLV is the backtest harness's job once enough games close post-cutover.

---

## 2. Framework-level reasoning (rating-mode-agnostic)

This section answers each question on first principles, before any production data is consulted. The reasoning establishes what we *expect* to see; §3 documents what we *actually* see.

### Q1 — Did fake giant edges shrink?

**First principles say YES.** The "fake giant edge" mechanism is described in `docs/calibration_impact_review_2026_05_10.md` §1.2:

- A strong team has a frozen static rating near default (50.0) because providers don't update `Team.rating`.
- The model produces a probability near 0.50 even though the market correctly prices the team at 0.65.
- The model "edge" against the team is 0.15 (15 pp) — a giant fake edge created entirely by the model not knowing the team is strong.

Elo replaces the frozen rating with a season-to-date rating. The strong team's rating reflects its actual strength, the model probability moves toward the market consensus, and the fake edge disappears.

**Direction expected:** `edge_ge_8pp` and `edge_ge_10pp` shrink under Elo. `edge_ge_6pp` (the minimum-edge threshold) may shrink more modestly because some real 6+ pp edges remain.

### Q2 — Did the recommendation distribution normalize?

**First principles say AMBIGUOUS-NET-IMPROVED.** Two opposing effects:

1. Fake elite edges disappear → fewer rows in the `elite` tier, fewer rows in `core` lane via the `EXTREME_DISAGREEMENT_GAP` flag.
2. Real edges sharpen → some rows that were previously below `MIN_PROBABILITY_FOR_RECOMMENDED = 0.60` now clear it, joining the recommended set.

Net direction on count is unclear; net direction on **composition** is predictable: fewer elites, more standards, fewer fake "huge edge / low prob" value-tier picks.

**Direction expected:** active-only recommendations roughly match alt-only count (modest churn in both directions); tier counts shift from elite-heavy to standard-heavy.

### Q3 — Did probabilities become more believable?

**First principles say YES.** Static ratings produce a probability distribution that's structurally narrow — most teams are at default 50.0, so most matchups produce probabilities near 0.50 (a tight blob), with a handful of seeded outliers producing probabilities at the clamp ceiling.

Elo produces a distribution that's *wider in the middle* (real rating gaps create real probability spreads in the moderate-favorite band) and *narrower at the extremes* (clamp ceiling hit less often).

**Direction expected:** `prob_ge_60` rises slightly (more honest moderate favorites). `prob_ge_85` falls (fewer clamp-ceiling pile-ups).

### Q4 — Did short-favorite aggressiveness reduce?

**First principles say PARTIALLY.** The short-favorite band (−149..+99) underperforms for three independent reasons:

1. Sigmoid is flat in the middle (rating-mode-agnostic).
2. Stale ratings cluster predictions near 0.50 (Elo fixes).
3. Market is sharpest in this band (rating-mode-agnostic).

Elo addresses #2 only. So we expect the short-fav band to show:
- Tighter alignment between active and alt probabilities (the static-clustering artifact reduces).
- Modestly fewer recommendations in the band (some marginal picks fall below MIN_EDGE under more honest math).
- Lower edge averages (the inflated edges from #2 collapse).

But the band remains *structurally* hard. Elo isn't a cure; it's a partial mitigation.

### Q5 — Did model/market disagreement improve?

**First principles say YES.** This is the most direct test of the realism hypothesis. The market reads current team strength; static ratings don't. Elo does. So the model's probability should be systematically closer to the de-vigged market under Elo.

**Direction expected:** `avg_disagreement_alt < avg_disagreement_active`. The `disagreement_gt_*` band counts drop progressively faster at the higher thresholds (e.g., `> 20 pp` drops harder than `> 5 pp`).

### Q6 — Did Elo naturally compress overconfidence?

**The single most important question.** Overconfidence is:

- Producing 70%+ probabilities the system can't justify.
- Piling up at the 0.85 clamp ceiling.
- Generating predictions that diverge from the market without informational basis.

**First principles say YES, by mechanism replacement, not by additional rules.** Static ratings overconfidence comes from either (a) seeded ratings far from 50.0 driving the score formula to extremes or (b) the sigmoid producing high probabilities on a fundamentally unreliable input. Elo doesn't add a damping rule; it replaces the unreliable input with a reliable one. Overconfidence becomes mechanically rarer because the mechanism that produced it (input-data noise getting amplified by the sigmoid) is gone.

The clamp at 0.85 was a *defensive* response to static-rating overconfidence. With Elo, the clamp is hit less, not because it's been changed, but because the upstream probability is more honest.

**This is what "Elo reduces overconfidence naturally" means** — the user's exact framing. It's true by construction; the empirical confirmation is the `prob_ge_85` band count and the disagreement compression.

### Q7 — Did expected CLV quality improve directionally?

**First principles say YES, with a direct mechanism.** CLV is positive when our pick correlates with the market's eventual movement. The market moves toward current strength (sharp money + line-makers updating). Our model — under Elo — also reads current strength. So our picks and the market's movement increasingly point the same direction. CLV+ rate trends up.

This is *directional*, not magnitude. We can't say "CLV+ goes from 25% to 35%" without settled games. We can say "the leading indicators (pick agreement, disagreement compression, edge realism) all point that direction."

---

## 3. Empirical findings — TEMPLATE for operator to fill in

The dev worktree this report is being written in has no production shadow data. The operator runs:

```
https://brotherwillies.com/analytics/shadow-review/?days=14
```

…and transcribes the numbers below. The report becomes a real Phase 2A Task 3 deliverable at that point; until then, it's a methodology + framework document.

### 3.1 Sample state

| Field | Production value |
|---|---|
| `sample` (rows with `elo_available=True`) | _to be filled_ |
| `sample_total` (rows scanned) | _to be filled_ |
| `active_mode` | _to be filled — expected `static`_ |
| Window | last 14 days |

**Minimum sample for these findings to be treated as evidence:** `sample ≥ 30`. Below that, the analysis stays directional but each individual number is anecdotal.

### 3.2 Q1 — Giant-edge frequency

| Threshold | active (static) | alt (elo) | Δ (alt − active) | Expected? |
|---|---|---|---|---|
| edge ≥ 6 pp | _ | _ | _ | mild reduction |
| edge ≥ 8 pp | _ | _ | _ | clear reduction |
| edge ≥ 10 pp | _ | _ | _ | strong reduction (extreme bucket should be near-empty under Elo) |

**Read:** if alt 8+ pp count is meaningfully below active 8+ pp count (>30% reduction), Q1 confirmed empirically.

### 3.3 Q2 — Recommendation distribution

| Metric | active (static) | alt (elo) |
|---|---|---|
| Recommendations both modes agree on (`status_recommended_both`) | _ | (same) |
| Active-only recommendations (`status_recommended_active_only`) | _ | — |
| Alt-only recommendations (`status_recommended_alt_only`) | — | _ |
| Both decline (`status_recommended_neither`) | _ | (same) |
| Active tier counts | _ | — |
| Alt tier counts | — | _ |

**Read:** active_only roughly equal to alt_only suggests modest churn (good — no whiplash). Tier counts shifting from elite-heavy to standard-heavy confirms Q2.

### 3.4 Q3 + Q6 — Overconfidence frequency

| Threshold | active (static) | alt (elo) | Δ | Expected? |
|---|---|---|---|---|
| final_prob ≥ 0.60 | _ | _ | _ | mild rise under Elo (sharper moderate favorites) |
| final_prob ≥ 0.70 | _ | _ | _ | roughly stable |
| final_prob ≥ 0.80 | _ | _ | _ | mild fall under Elo |
| final_prob ≥ 0.85 (clamp ceiling) | _ | _ | _ | **clear fall under Elo — the single most important diagnostic** |

**Read:** `prob_ge_85.alt < prob_ge_85.active` is the empirical confirmation of "Elo reduces overconfidence naturally."

### 3.5 Q5 — Market disagreement

| Metric | active (static) | alt (elo) | Δ |
|---|---|---|---|
| Mean abs disagreement | _ | _ | _ |
| > 5 pp count | _ | _ | _ |
| > 10 pp count | _ | _ | _ |
| > 15 pp count | _ | _ | _ |
| > 20 pp count | _ | _ | _ |

**Read:** `avg_disagreement_alt < avg_disagreement_active` confirms market alignment improved. The drop should be steepest in the higher thresholds (the long tail is where staleness lives).

### 3.6 Q4 — Short-favorite band (−149..+99)

| Metric | active (static) | alt (elo) |
|---|---|---|
| Sample (in-band picks) | _ | (same) |
| Mean final_prob | _ | _ |
| Mean edge_pp | _ | _ |
| Pick agreement rate | — | _ |
| Recommended count | _ | _ |

**Read:** mean edge_pp should drop under alt (the fake-edge mechanism is most pronounced in this band). Recommended count should be lower or equal. If alt-mode recommended *more* picks in this band than active, that's a red flag — investigate.

### 3.7 Q7 — Pick agreement (leading CLV indicator)

| Metric | Value |
|---|---|
| Pick agreement rate | _ |

**Read:** healthy range is [0.75, 0.95]. Below 0.60 indicates the two modes are dramatically different worldviews (investigate); above 0.97 indicates Elo isn't materially changing anything (suggests insufficient backfill).

---

## 4. Findings (conditional on §3 numbers)

Three findings can be stated **without** the empirical data because they rest on the structural work, not on which-mode-wins comparisons:

### 4.1 The shadow framework works as designed

Phase 1B Task 5 plumbing functions correctly. Every MLB recommendation under the new code persists both active and alt mode data. The alt computation:
- Uses `force_use_dynamic` for process-local override (not a settings change).
- Runs under the same `OddsSnapshot`, same calibration constants, same gates.
- Captures all required fields (pick, side, odds, final_prob, edge, status, tier, lane, ratings, elo_available).
- Cannot block primary persistence (exception handling verified by `test_alt_compute_failure_does_not_block_primary_persist`).

That's a structural finding; the empirical numbers below confirm or refute the *substance*, not the framework.

### 4.2 The realism mechanism is structurally addressed

Static ratings have zero updaters across the codebase — confirmed by `apps/core/test_feature_truth_audit.py::StaticTeamRatingHasNoUpdaterTests`. By definition they cannot represent current strength. Elo updates after every final game via `update_elo_ratings` (wired into the cron in Phase 1B Task 6). The realism gap is closed by mechanism.

### 4.3 The Phase 2A backfill + shadow scaffolding produces the right *kind* of evidence

The seven Task 3 questions are answerable from `BettingRecommendation.shadow_alt_data` + the extended `ShadowReview` aggregations. No additional data, no additional code, no production changes needed to read the answer. That property — "Is the empirical work cheap to do?" — is a non-trivial design success.

---

## 5. Risks

These are real, ordered by severity.

| Risk | Severity | Mitigation |
|---|---|---|
| **Insufficient sample.** `sample < 30` means individual band counts are anecdotal. | High at first slate, decays | Wait. Sample accumulates one slate at a time. Re-run the analysis at sample = 30, 60, 100. |
| **Recent injury / pitcher changes don't propagate fast enough through Elo's K=4.** A team whose ace went on the IL last week still carries last week's rating. | Medium | This is *not* an Elo failure; it's a "Elo + pitcher rating + injury context" gap that Phase 2B-C addresses. For Task 3, just acknowledge: Elo is one piece. |
| **Backfill correctness** — if the backfill walked games out of order or skipped final games with missing scores, the Elo ratings are wrong. | Medium | Verified by `RebuildIdempotenceTests` and the `ensure_elo_backfilled` data-quality check (count of final games with both scores). The detection guard in the new command prevents partial backfills from being treated as "ready". |
| **Shadow compute drift** — alt mode uses `force_use_dynamic` which is a process-local override. If two threads run alt computes concurrently, state could bleed. | Low | The analytics page enforces no-concurrent-runs guard. Live recommendation persistence is single-threaded per request. |
| **Operator misreads the page** — `active=elo, alt=static` is the post-cutover state and reverses the expected direction of every band count. | Low | The page surfaces `active_mode` prominently; the methodology doc explicitly states "if Elo is the alt mode, alt should show fewer giant edges." |

---

## 6. Weaknesses still remaining (even if Elo cleanly passes Task 3)

Listed so they don't get lost when the cutover decision is made.

1. **Pitcher recent form not modeled.** Season-aggregate pitcher rating misses hot/cold streaks. Phase 2B Task 3 (deferred per direction).
2. **Team form not modeled.** Elo K=4 integrates over ~10 games; near-term form is the first 1–2 games. Phase 2B Task 4.
3. **Bullpen quality not modeled.** Bullpens decide a meaningful share of MLB games. Phase 2B Task 5.
4. **Injuries declared but not consumed.** `InjuryImpact` rows exist; MLB's `HOUSE_WEIGHTS['injury']` is documented phantom (Phase 1A Feature Truth Audit). Phase 1C / 2B follow-up.
5. **Sigmoid divisor and clamp bounds were tuned against the static distribution.** Post-cutover, retunes may be justified. Per direction, this is **deferred** — variable isolation comes first.
6. **No CLV-quality dashboard.** We have CLV per recommendation in settled MockBets, but no time-series view of CLV+ rate by mode. This is the post-cutover monitoring requirement.

Phase 2A Task 4's GO/NO-GO decision should explicitly state which weaknesses remain after the cutover and which monitoring surfaces address them.

---

## 7. Does Elo materially improve realism?

**Structurally: yes.** The mechanism that produced the realism gap (frozen ratings) is replaced by a mechanism that closes it (incremental updates). This conclusion holds regardless of the numbers in §3.

**Empirically: pending §3 numbers.** Specifically, the test is:

- `prob_ge_85.alt < prob_ge_85.active` (overconfidence reduces), AND
- `avg_disagreement_alt < avg_disagreement_active` (market alignment improves), AND
- `edge_ge_8pp.alt < edge_ge_8pp.active` (fake edges shrink)

Two of three confirming is borderline GO. All three confirming is clear GO. Fewer than two is NO-GO until investigation.

---

## 8. Is production activation justified?

**Conditional GO**, with the §3 empirical confirmations as the gate. The Phase 2A Task 4 decision document will turn this conditional into a verdict.

The conditions for "justified" are deliberately mechanical — they prevent the cutover from being decided on vibes or on a single salient slate.

---

## 9. Recommended next step

**Single action:**

1. Operator opens `/analytics/shadow-review/?days=14` on production.
2. Confirms `sample ≥ 30` and `active_mode = static`.
3. Transcribes the §3 numbers into this document (or into the Task 4 decision document, equivalently).
4. Verifies the three structural confirmations from §7.
5. Returns with "go for Task 4" if the confirmations hold, or "investigate <specific band>" if they don't.

**No code changes are part of this step.** Phase 2A Task 4 (the activation decision) ships as its own commit, with the empirical evidence frozen into a doc, the decision verdict written, and the flag flip (`USE_DYNAMIC_RATINGS=True` in Railway env) as the deployment action.

---

## 10. What this report explicitly did NOT do

Per user direction, the following were excluded from this pass:

- ❌ No activation of Elo. Flag stays False.
- ❌ No calibration retunes (sigmoid divisor, clamp, blend weight all untouched).
- ❌ No new predictive signals (pitcher form, team form, bullpen — Phase 2B, deferred).
- ❌ No edge realism compression (Phase 2C, deferred).
- ❌ No ROI / win-rate / profit analysis. Sample is too small; this is realism, not profit.

What was added — strictly observation infrastructure:

- ✅ Eleven new aggregation fields on `ShadowReview` (giant-edge bands, overconfidence bands, disagreement bands + averages, short-fav scoped review).
- ✅ Five new sections on `/analytics/shadow-review/` rendering those numbers.
- ✅ Eight new tests locking the aggregation behavior.
- ✅ This methodology document.

The recommendation pipeline, the score formula, the calibration constants, the recommendation engine — all untouched.

---

*Last updated: 2026-05-14. Empirical sections to be filled in by operator from production shadow data.*
