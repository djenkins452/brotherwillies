# Brother Willies — Model Quality Diagnosis & Repair Plan

**Date:** 2026-05-16
**Trigger:** Actual-bets evaluation showing 33–31 record, −5.6% ROI, 29.7% CLV+, 71% of bets in the 8+pp edge bucket.
**Status:** **Diagnosis only.** No code changes in this commit. No threshold tunes. No Elo activation. The diagnostic ends with an "exact next implementation prompt" specifying the smallest fix needed; that fix awaits your authorization.

---

## 0. Executive Diagnosis

**One sentence.** The reported performance is consistent with the stale-rating fake-edge pathology that the entire Phase 2A architecture was built to address — but the evaluation population is almost certainly impure, and we cannot ethically diagnose the model from a bankroll-tracked dataset until we extract the "Model Performance" subset.

**Three sentences.** The 8+pp bucket holding 46 of 65 bets (71%) is structurally impossible for a calibrated engine — it is the fingerprint of stale `Team.rating` driving the sigmoid into extreme regions and generating illusory edges. The 65–70% confidence band hitting 47% (15 bets, 7–8) is consistent overconfidence in the model's lower-confidence range, also fingerprint-matching the stale-rating problem (low rating discrimination → most matchups land in the sigmoid's flat middle → predicted probabilities cluster artificially). The 29.7% CLV+ rate, while alarming, is computed over an unknown denominator that mixes system-generated bets with manual bets, and over a CLV calculation that does NOT enforce the `odds_api` source guard documented in the Recommendation Quality Framework — so the rate is both signal and artifact.

**The prescription.** Phase 2A's Elo cutover is mechanistically the right fix and the evidence base now strengthens (not weakens) the case for it. But Law 4 forbids reacting to 65-bet samples with threshold changes, and Law 3 forbids drawing model conclusions from a population that includes manual bets and incomplete snapshots. The repair is: (Phase A) clean the eval population; (Phase B) fix CLV's missing source filter and verify completeness; (Phase C) capture pre-Elo Health Score baseline; (Phase D) activate Elo with rollback monitoring; (Phase E) observe — no new signals until Elo has stabilized.

---

## 1. The evidence and what it actually says

### 1.1 The numbers, separated by signal type

| Number | Statistical content | Notes |
|---|---|---|
| 33–31 record | **Variance, not signal** | 64-bet sample has natural standard deviation of ~4 wins. 33–31 is within 1σ of breakeven. Win rate is not a tuning target per the framework. |
| −5.6% ROI (−$356.80) | **Mild evidence of negative EV** | Within variance on 64 bets but trending wrong. Not strong enough to tune from. |
| **29.7% CLV+ rate** | **Strong signal — system misaligned with market** | Even on small samples, getting beaten by the close 70% of the time is unmistakable. Caveats below. |
| **46 of 65 bets in 8+pp bucket (71%)** | **Strong structural signal — fake-edge pathology** | A healthy 8+pp bucket should be 5–15% of slate. 71% is the staleness fingerprint. |
| 18 underdog bets, 6–12, −24.8% ROI | **Suggestive of overconfidence on dogs** | At average underdog +175 prices, 6/18 = 33% win rate yields ROI ≈ −10%. Getting to −24.8% means *worse* than the implied; we're systematically picking bad dogs. |
| 35 short-fav bets, 19–16, −1.9% ROI, 22.9% CLV+ | **The danger band is doing its expected job — badly** | Short-fav band is structurally hard. CLV+ 22.9% confirms model-market misalignment in this band specifically. |
| 15 bets in 65–70% confidence, 7–8, −17.8% ROI, 13.3% CLV+ | **Calibration drift in mid-band, BUT sample too small for confident conclusion** | Predicted 67%, observed 47%. 20pp calibration error. Could be variance (n=15) or signal. 13.3% CLV+ is the more reliable read. |

### 1.2 What's signal vs noise on 64 bets

Per `docs/recommendation_quality_framework.md` §2:
- Calibration retune requires ≥ 100 settled bets + 4 weeks. **Not met.**
- Global gate change requires ≥ 100 settled bets + 4 weeks. **Not met.**
- Sub-segment gate requires ≥ 20 in segment + 4 weeks + named mechanism. Short favorites have 35 bets; underdogs only 18. **Marginal.**

Per Law 4, this sample does NOT justify threshold tunes. It DOES justify:
- Investigation of the population (was it clean?)
- Investigation of the calculation (is CLV computed correctly?)
- A structural intervention (Elo cutover) that already has independent justification

### 1.3 The 8+pp bucket is the most important number on this page

71% of bets being in the elite-edge bucket is the diagnostic. Read against current calibration constants:

- `MIN_EDGE = 6.0` pp. System-recommended bets all have edge ≥ 6pp.
- Expected distribution under a calibrated model: most picks in the 6–8 band, fewer in 8+.
- Observed: ~71% in 8+ band.

A model that produces 8+pp edges on most of its picks is one of three things:
1. Reading inputs the market doesn't (rare; possible but unlikely at this magnitude).
2. **Reading stale inputs and treating its own staleness as edge** (the fake-edge pathology, described in `docs/calibration_impact_review_2026_05_10.md` §1.2).
3. Generating recommendations on artificially-narrow probabilities where the sigmoid is flat (also a staleness consequence).

(2) and (3) are the same root cause: `Team.rating` is frozen since seed, the model can't see current strength, and the score formula's sigmoid maps the stale inputs into either flat-middle clusters or extreme tails depending on how mis-rated the teams are. The result is a *bimodal* edge distribution — most matchups produce small score differences (the flat middle) but a meaningful subset produce extreme score differences (the tails). The tail recommendations are the 8+pp bucket; they look like elites and behave like noise.

The 29.7% CLV+ rate is what happens when 71% of your picks are tail-driven by stale ratings: the market converges on truth as games approach, your stale-rating picks fail to align with that truth, you systematically get beaten by the close.

---

## 2. Root-cause hypotheses, ranked

### H1 (very likely) — Stale Team.rating is the structural cause

**Evidence.**
- `Team.rating` has zero updaters across the codebase (locked by `apps/core/test_feature_truth_audit.py::StaticTeamRatingHasNoUpdaterTests`). Frozen at seed.
- 71% of bets in 8+pp bucket is the mathematical fingerprint of frozen ratings driving sigmoid extremes.
- 29.7% CLV+ rate is the market-alignment fingerprint of frozen ratings.
- Underdog −24.8% ROI is consistent with the model overestimating teams it can't see have weakened.
- Short-fav 22.9% CLV+ is the sigmoid-flat-middle artifact specific to that band.

**Mechanistic fix.** Phase 2A Elo activation. Already designed, structurally validated, shadow-mode framework in place.

### H2 (likely) — Evaluation population is mixed

**Evidence.**
- The "Actual Bets" scope (current default, shipped 2026-05-14 to fix eval integrity) includes manual placements.
- We have no count of how many of the 65 are manual vs system-generated.
- Manual bets lack `expected_edge`, `recommendation_status`, `recommendation_tier` — they fall into "unlabeled" segment buckets but inflate the topline "65 actual bets" denominator.
- The 8+pp bucket count of 46 likely excludes manual bets (no edge → no bucket assignment), so the 71% concentration may be among the system-generated subset only.

**Impact.** When the user reads "−5.6% ROI on 65 bets," that ROI includes manual placements that the model never recommended. When they read "8+pp edge: 46 bets," that's likely model bets only. The numbers conflate different populations.

**Mechanistic fix.** Add a `model_clean` scope to the Moneyline Evaluation that requires `is_system_generated=True` AND complete decision-layer snapshot (`recommendation_status`, `recommendation_tier`, `expected_edge`, `recommendation_confidence` all populated). Surface alongside the existing scopes with full exclusion-count transparency per Law 3.

### H3 (likely) — CLV calculation lacks the source filter the framework requires

**Evidence.** Looking at `apps/mockbets/services/recommendation_performance.py::_group_stats`:

```python
# Line 54
if b.clv_cents is not None:
    clv_sum += b.clv_cents
    clv_count += 1
    if b.clv_direction == 'positive':
        clv_positive += 1
```

CLV is computed from any bet with `clv_cents` populated — **regardless of `odds_source`**. The framework (`docs/recommendation_quality_framework.md` §1.2 CLV section) explicitly states:

> CLV is only meaningful when both opening and closing snapshots are from a trustworthy primary source (`odds_api`). ESPN-source CLV is intentionally suppressed because ESPN's "movement" is mostly capture artifact, not real market.

The backtest service (`apps/core/services/backtesting_service.py`) DOES enforce this filter (lines 642-645):

```python
sources_ok = (
    getattr(opening, 'odds_source', '') == 'odds_api'
    and getattr(closing, 'odds_source', '') == 'odds_api'
)
```

But the **live-bet evaluation path (`_group_stats`) does NOT.** ESPN-source bets contribute to the CLV+ rate. ESPN CLV is dominated by capture artifact rather than real market movement, so it's structurally near-zero rather than informative.

**Impact.** The 29.7% CLV+ rate is computed over a mixed-source CLV sample. We don't know the rate among `odds_api`-only bets. It could be worse (if the model is genuinely terrible on the high-quality price stream) or considerably better (if ESPN bets are dragging the average down).

**Mechanistic fix.** Apply the same source filter in `_group_stats` that the backtest service uses. This is a calculation-correctness fix, not a tune.

### H4 (likely) — Pre-snapshot-feature bets are being evaluated against current rules

**Evidence.**
- 2026-05-03 added `raw_model_prob`, `final_model_prob`, `market_prob`, `extreme_disagreement` snapshot fields on `BettingRecommendation`. MockBet rows that predate that work are missing these.
- 2026-05-03 + 2026-05-06 changed `MIN_EDGE` (3→5→6), `MIN_PROBABILITY_FOR_RECOMMENDED` (0.55→0.60), `MARKET_BLEND_WEIGHT` (0.15→0.30→0.40), and `EXTREME_DISAGREEMENT_GAP`.
- A bet placed under MIN_EDGE=3.0 with edge=4pp is in our database as a 4pp-edge bet. Under current rules (MIN_EDGE=6.0) the engine would not have recommended it.
- Such bets appear in "system-generated" but were generated under different gates.

**Impact.** "System-generated" alone is insufficient as a model-eval scope. We need "system-generated under current rules" — which requires either a timestamp filter (only bets placed after the calibration second pass on 2026-05-06) or a snapshot-completeness filter (bets that have the 2026-05-03 fields).

**Mechanistic fix.** Belongs in `model_clean` scope: require timestamp ≥ 2026-05-06 OR `final_model_prob is not None`.

### H5 (lower likelihood) — Model overconfidence in 65–70% band is structural, not just stale-rating

**Evidence.**
- 7 wins on 15 bets at predicted 67% is a 20pp calibration error.
- Sample is small (n=15) but the direction is consistent with the broader pattern.
- The framework's calibration retune candidates (sigmoid divisor, soft clamp) target this band specifically.

**Impact.** Even after Elo activation, the sigmoid + clamp constants may need retuning. But per Law 4 we don't do this until we have ≥ 100 settled bets in the 65–70% band over ≥ 4 weeks. Defer to post-Elo data.

**Mechanistic fix.** None now. Phase 1D candidate, after Elo data accumulates.

### H6 (lower likelihood) — Recommendation tier is being misclassified

**Evidence.** The 46 bets in 8+pp bucket suggest most recommendations are getting the `elite` tier. The slate-cap of `MAX_ELITE_PER_SLATE = 2` should be holding most of those back to `strong`. Either the cap is being bypassed, or `assign_tiers` is being skipped for the user-rec path, or the cap only applies within one slate (it does), and the 46 elites are spread across many slates.

**Impact.** Likely just the per-slate cap working as designed across many slates. Not a bug, but worth a sanity-check audit.

**Mechanistic fix.** None unless audit shows the cap is being bypassed.

---

## 3. Evaluation Population Audit — Framework

This is the **first thing** to do. Without a clean population, every conclusion is suspect.

### 3.1 Categories the audit must produce

The operator runs this on production. I cannot fill in counts from this worktree. The categorization framework:

| Category | Filter | Problem? | Recommendation |
|---|---|---|---|
| **Pure model bets** | `is_system_generated=True` AND `recommendation_status` non-empty AND `recommendation_tier` non-empty AND `expected_edge` not None AND `recommendation_confidence` not None AND `placed_at >= 2026-05-06` | None — this is the clean subset | This is the population for Model Performance evaluation |
| **System-generated, incomplete snapshot** | `is_system_generated=True` AND any of (`recommendation_status` empty / `expected_edge` None / `recommendation_confidence` None) | Pre-feature rows; cannot be evaluated against current rules | Exclude from Model Performance; include in Bankroll only |
| **System-generated, pre-calibration-tightening** | `is_system_generated=True` AND `placed_at < 2026-05-06` | Generated under different gates; produces noise when bucketed with current rules | Exclude from Model Performance; can be reported as "pre-rules system bets" |
| **Manual bets** | `is_system_generated=False` | Not model recommendations | Exclude from Model Performance; include in Bankroll |
| **Low-confidence bets** | `recommendation_confidence < 60.0` | Should not have been recommended under current rules | Exclude from Model Performance — they reflect the user choosing to bet despite the model |
| **Missing edge** | `expected_edge is None` | Cannot be edge-bucketed | Exclude from Model Performance |
| **Missing market_prob** | `market_prob` field on the source `BettingRecommendation` is None | 2026-05-03 snapshot work didn't run; pre-feature row | Exclude from Model Performance |
| **Missing closing odds (CLV-blind)** | `closing_odds_american is None` AND result settled | Cannot contribute to CLV evaluation | Surface separately on the eval page; exclude from CLV math |
| **Duplicate matchup** | Multiple bets on the same `mlb_game` for the same user within a single slate | User placed multiple times on same game; inflates per-game weight | Surface as a warning; do not auto-deduplicate (the user's intent matters) |
| **Pending in window** | `result = 'pending'` AND placed within window | Can't contribute to settled-bet math yet | Surface as a count; exclude from outcome buckets |

### 3.2 What the audit's output will tell us

When the operator runs this audit on production over the same window that produced the 65-bet topline, we'll learn:
- How many of the 65 are pure model bets (the legitimate denominator for model evaluation).
- How many are manual / incomplete / pre-rules contamination.
- How many CLV samples we actually have to work with.

Until we have these counts, the topline numbers are diagnostic guidance — not evidence base for tuning.

---

## 4. CLV Integrity Audit

### 4.1 How CLV is currently computed

For each MockBet:
- `clv_cents = decimal_odds_at_placement - decimal_odds_at_close` (set at settlement time via `settle_user_pending_bets` / settlement service).
- `clv_direction = 'positive' if clv_cents > 0 else 'negative' if clv_cents < 0 else ''`.

In `_group_stats`:
- `clv_count` increments for any bet where `clv_cents is not None`.
- `positive_clv_rate = clv_positive / clv_count * 100`.

### 4.2 Where bugs / artifacts could be hiding

| Issue | Evidence | Fix |
|---|---|---|
| **No `odds_source='odds_api'` filter** | `_group_stats` lines 54-58 vs `backtesting_service.py` lines 642-645. The framework explicitly requires the source filter; the eval path doesn't apply it. | Apply the source filter in `_group_stats`. Calculation-correctness fix, not a tune. |
| **No filter on `is_derived` snapshots** | Derived odds (synthetic moneylines from one-sided ESPN data) carry no real market movement. They'd produce near-zero or random CLV. | Filter `closing_odds_source != 'derived'` — but MockBet doesn't currently snapshot the closing odds source separately. May require a new field or back-derive from the snapshot. |
| **Manual bets contribute to CLV** | Manual bets can have `closing_odds_american` populated by the settlement engine if a snapshot exists for their game. Their `clv_cents` is computed even though no model recommended them. | Filter to system-generated for the CLV-as-model-quality calculation (Model Performance scope). Manual bets still contribute to Bankroll Performance scope. |
| **Sign correctness verification** | `closing_line_value(bet_odds, closing_odds) = bet_dec - close_dec` per `apps/core/utils/odds.py:74`. The SIGN-CORRECTNESS NOTE in that file documents a previous bug-fix; the math should now be correct. | Spot-check: pick 3 bets manually. For each, verify the sign of `clv_cents` matches "did the line move toward my pick". Lock with a regression test if any sign issues found. |
| **Capture timing** | Closing odds are captured by the settlement engine. If the engine runs late (after the closing snapshot has been overwritten by a later post-game snapshot), the "closing" odds we record are actually post-game garbage. | Sanity check: for any settled bet, verify `closing_odds_american` matches the odds snapshot whose `captured_at` is immediately before `first_pitch`. If they don't match, the capture logic is broken. |

### 4.3 The most likely culprit

H3 (CLV lacks source filter) is high-confidence based on direct code inspection. The fix is one conditional in `_group_stats` to mirror the backtest service. Until that fix lands, the 29.7% CLV+ rate is a mixed-source artifact and cannot be used as evidence for or against the model.

---

## 5. Model Performance vs Bankroll Performance — the missing distinction

This is the largest single issue identified in this diagnosis. The current Moneyline Evaluation page treats "Actual Bets" as the default, which is correct for **bankroll** evaluation (the user's question: "how did MY bets perform?"). But the user is reading the same numbers as if they answer "how did the MODEL perform?" — and they don't.

### 5.1 The two questions are different

**Bankroll Performance** (what's currently the default):
- Scope: every bet the user placed, regardless of system or manual.
- Use case: did I make money? Did I bet well?
- Numerator includes user's manual decisions; ROI reflects user behavior, not just model.

**Model Performance** (what's missing):
- Scope: every bet that was officially recommended by the engine under current rules with complete decision data.
- Use case: did the model recommend well? Are model probabilities reliable?
- Numerator excludes user's manual decisions and incomplete-snapshot rows.

### 5.2 Proposed `model_clean` scope

A fifth scope, alongside the existing `actual` / `recommended` / `manual` / `all`:

```python
SCOPE_MODEL_CLEAN = 'model_clean'   # system-generated AND complete snapshots
                                     # AND placed under current rules
                                     # AND minimum-quality decision data
```

Filter logic:
- `is_system_generated = True`, AND
- `recommendation_status` non-empty, AND
- `recommendation_tier` non-empty, AND
- `expected_edge` not None, AND
- `recommendation_confidence` not None, AND
- `placed_at >= 2026-05-06` (date of calibration second pass).

The scope summary surfaces (per Law 3):
- Total placed in window: N
- Included (model_clean): M
- Excluded: N − M, with per-reason breakdown:
  - K manual bets
  - L pre-rules bets (placed before 2026-05-06)
  - P incomplete snapshot
  - Q low confidence

### 5.3 Why this changes the diagnosis

The 65-bet topline becomes two numbers:
- Bankroll Performance over 65 bets
- Model Performance over M bets (the clean subset)

If M is, say, 30 of the 65, the model's apparent badness is concentrated in a much smaller true-model sample, and the 35-bet manual+pre-rules contamination has been pulling the topline numbers around. We cannot know which direction without the actual count, but **the diagnosis is decision-blocking until we have it**.

---

## 6. Elo Activation Readiness

### 6.1 Does the current underperformance strengthen the case for Elo?

**Yes — substantially.** The fake-edge pathology is precisely what Elo addresses. Every observed pathology (71% in 8+pp bucket, 29.7% CLV+, underdog −24.8% ROI, short-fav 22.9% CLV+) is consistent with stale `Team.rating` producing model probabilities that drift from market reality. Elo removes that mechanism.

The pre-2A engineering report's mathematical case for Elo was already strong. The 2026-05-16 evidence makes it stronger:
- 71% in 8+pp is the empirical fingerprint of the mechanism the report predicted.
- 29.7% CLV+ is the market-alignment fingerprint the report predicted.

### 6.2 Why we still do NOT activate blindly

Law 4 requires evidence-based change. The evidence we need:
- ✅ Math case (strong, in the engineering report).
- ✅ Empirical fingerprint of the pathology (this diagnosis, today).
- ⏳ Pre-Elo Health Score baseline (requires Phase 2A Task 1 backfill to have run on prod, plus `capture_health_snapshot --notes "pre-elo baseline"`).
- ⏳ Shadow-mode comparison data with `sample ≥ 30` and the three structural confirmations (from `docs/phase_2a_task3_shadow_analysis_2026_05_14.md` §7).
- ⏳ Pure model-population numbers (this diagnosis's Phase A).

### 6.3 Is there enough shadow data yet?

**Unknown from this worktree.** The shadow infrastructure shipped 2026-05-10 in commit `2db719b`; the production backfill hook shipped 2026-05-14 in `0f62d09`. The Railway deploy that runs the backfill is required before shadow_alt_data populates meaningfully on new recommendations. After that deploy, at least one full MLB slate must have been recommended through the new code for shadow data to accumulate.

The operator can verify in 30 seconds:

```bash
python manage.py shell -c "
from apps.core.models import BettingRecommendation
qs = BettingRecommendation.objects.filter(sport='mlb', shadow_active_mode__in=('static','elo'))
print(f'total: {qs.count()}')
print(f'with elo_available=True: {sum(1 for r in qs if r.shadow_alt_data.get(\"elo_available\"))}')
"
```

If the `elo_available=True` count is ≥ 30, shadow review is ready. If not, we wait one or two slates.

### 6.4 Will Elo address each observed failure?

| Observed failure | Elo addresses? | Why |
|---|---|---|
| 71% in 8+pp bucket | **Yes (mechanistically)** | Stale-rating amplification of perceived edge is the cause. Elo removes the mechanism. The bucket will compress under Elo. |
| 29.7% CLV+ rate | **Yes (mechanistically)** | Model-market misalignment from frozen ratings. Elo ratings track season-to-date strength; predictions move toward where the market is moving. |
| Underdog −24.8% ROI | **Yes, partially** | Stale ratings make the model overestimate underdogs the market knows have weakened. Elo updates underdog ratings down after losses. |
| Short-fav 22.9% CLV+ | **Yes, partially** | Staleness component fixes. Sigmoid-flat-middle + market sharpness in this band do NOT change — those are structural. Phase 2C edge realism compression candidate, deferred. |
| 65–70% confidence overconfidence | **Possibly** | If overconfidence is from stale-rating noise being squashed by the sigmoid into the confident band, Elo helps. If it's a calibration issue independent of rating freshness, Phase 1D retune territory. Cannot determine without post-Elo data. |

---

## 7. Repair plan — staged, evidence-gated

### Phase A — Clean the evaluation population *(do this first)*

**Goal.** Separate Bankroll Performance from Model Performance. Make it impossible to silently judge the model from a mixed dataset.

**Concrete deliverables.**
1. Add `SCOPE_MODEL_CLEAN` to `apps/mockbets/services/moneyline_evaluation.py` with the filter logic from §5.2.
2. Add the scope to the dropdown and the scope-summary box on the eval page.
3. Add a "Population Audit" sub-section to the eval page (visible at all scopes) showing the categorical breakdown from §3.1 with counts and per-category exclusion reasons.
4. Lock the categorization with tests, including the alignment contract: My Bets count = `actual` scope `included` count = `manual` + `model_clean` + `pre-rules` + `incomplete` + `low_confidence`.

**What it produces.** The operator re-reads the eval under `model_clean` scope. The 65-bet topline becomes "X bankroll bets, M model bets." The 8+pp bucket and CLV+ rate get recomputed over the clean subset. We discover whether the pathology is the model's fault or amplified by contamination.

**No threshold tunes.** No calibration changes. No Elo activation. Purely evaluation-truth fix.

### Phase B — Fix CLV calculation correctness *(do this second)*

**Goal.** Apply the `odds_source='odds_api'` filter to live-bet CLV math, so the CLV+ rate is comparable to the backtest CLV+ rate and matches the framework's specification.

**Concrete deliverables.**
1. Add the source-filter conditional in `_group_stats` mirroring `backtesting_service._evaluate_game` lines 642-645.
2. Add a `clv_sample_filtered_reasons` dict on the evaluation summary: how many bets were excluded from CLV math, and why (manual, ESPN-source, derived, no closing snapshot).
3. Run a sanity-check: pick 3 recent settled bets manually; verify the sign and magnitude of `clv_cents` matches "did the line move toward my pick by how much."
4. Lock with tests: a known-mixed-source population produces the right CLV+ rate when filtered.

**What it produces.** The 29.7% CLV+ rate gets recomputed over the source-filtered subset. We see the real model-market alignment number.

**No threshold tunes.** No calibration changes.

### Phase C — Capture pre-Elo Health Score baseline + verify shadow data *(do this third)*

**Goal.** Have the evidence base ready for the Elo cutover decision.

**Concrete deliverables.**
1. On production: `python manage.py capture_health_snapshot --notes "pre-elo baseline"`.
2. Verify shadow data: query `BettingRecommendation` rows for `shadow_active_mode='static'` with `shadow_alt_data.elo_available=True`; confirm `count >= 30`.
3. Visit `/analytics/shadow-review/?days=14` on production; read the three structural confirmations from `docs/phase_2a_task3_shadow_analysis_2026_05_14.md` §7.
4. Capture the Phase A clean evaluation numbers in a follow-up snapshot.

**What it produces.** A frozen pre-Elo Health Score row + frozen shadow comparison + frozen clean-population evaluation. Together they form the rollback baseline for Phase D.

**No code changes.** This phase is operator action.

### Phase D — Activate Elo *(only if Phases A–C all pass their gates)*

**Goal.** Flip `USE_DYNAMIC_RATINGS=True` on Railway. Monitor.

**Gates that must pass.**
- Phase A clean-population evaluation has been read; model performance is poor enough to justify intervention.
- Phase B-corrected CLV+ rate is also poor (confirming the misalignment isn't a calculation artifact).
- Phase C shadow-review confirmations passed (the three structural tests from Task 3 §7).
- Pre-Elo Health Score baseline captured.

**Concrete deliverables.**
1. Set `USE_DYNAMIC_RATINGS=True` in Railway env vars. Railway auto-redeploys.
2. Day 1 post-flip: `python manage.py capture_health_snapshot --notes "post-elo day 1"`.
3. Week 1: same.
4. Week 4: same.
5. Compare Health Score trajectory. Compare CLV+ rate, edge-bucket distribution, short-fav segment ROI.

**Rollback trigger.** Any of:
- Health Score declines by ≥ 10 points sustained over a full week.
- CLV+ rate (model_clean + odds_api filtered) drops further from baseline.
- 8+pp bucket concentration *increases* rather than decreases.

**Rollback action.** `USE_DYNAMIC_RATINGS=False`. One env var change.

### Phase E — Observe; defer all other changes *(post-cutover, weeks 1-4)*

**Goal.** Let Elo stabilize. Per Law 4 "no optimization stacking," no other changes ship in this window.

**Concrete deliverables.** None. This is the stabilization window.

**What's deferred:**
- Phase 2B: pitcher recent form, team recent form, bullpen (all bounded-signal additions).
- Phase 2C: edge realism compression.
- Phase 1D: any calibration retunes (sigmoid divisor, clamp, blend weight).

The framework permits returning to these AFTER Elo has accumulated ≥ 4 weeks of post-cutover data AND only if the Health Score / per-segment evidence supports them.

---

## 8. Do Now / Do Not Do Yet

### Do now (this session, my next turn, on your authorization)

- [ ] **Phase A implementation.** Add `model_clean` scope and the population audit sub-section to the Moneyline Evaluation page. Adds tests. No threshold tunes, no calibration changes. This is the smallest fix that restores evaluation integrity for model-quality decisions.

- [ ] **Phase B implementation.** Apply the `odds_source='odds_api'` filter to `_group_stats` so the CLV+ rate matches the framework specification and the backtest service. Add tests. Pure calculation-correctness fix.

(Phase A and B can ship in one commit since they're both purely evaluation-truth, but I'd separate them so the diff is clean and reviewable.)

### Do now (operator, after Phase A + B deploy)

- [ ] Visit `/mockbets/moneyline-evaluation/?scope=model_clean` and read the corrected numbers.
- [ ] Compare to the topline 65-bet numbers from this diagnosis.
- [ ] Verify the population audit accounts for every bet (Law 3 alignment contract).
- [ ] Capture pre-Elo Health Score baseline: `python manage.py capture_health_snapshot --notes "pre-elo baseline"`.
- [ ] Verify shadow data has ≥ 30 rows with `elo_available=True`.
- [ ] Visit `/analytics/shadow-review/?days=14` and read the three structural confirmations.

### Do NOT do yet

- ❌ **No threshold tunes.** `MIN_EDGE`, `MIN_PROBABILITY_FOR_RECOMMENDED`, `MAX_ABS_ODDS_FOR_RECOMMENDED`, `HEAVY_FAVORITE_ODDS`, `EXTREME_DISAGREEMENT_GAP` — all stay where they are.
- ❌ **No calibration retunes.** `MARKET_BLEND_WEIGHT`, `PROB_MIN`, `PROB_MAX`, sigmoid divisor — all stay.
- ❌ **No new predictive signals.** No pitcher form, team form, bullpen, recent form, anything.
- ❌ **No edge realism compression.** Phase 2C, deferred.
- ❌ **No Elo activation yet.** Phase 2A Task 4 awaits the evidence base.
- ❌ **No emotional reaction.** The 65-bet sample does not justify any constant change per Law 4. A bad slate is data; the response is to *measure*, not to *tune*.

---

## 9. The exact next implementation prompt

Below is the prompt the user should paste back to me (or another agent) to execute the smallest evaluation-integrity fix. Self-contained; no context needed beyond reading this doc.

> **Implement Phase A + Phase B of `docs/model_quality_diagnosis_2026_05_16.md`. Strict scope:**
>
> **Phase A — Model Clean scope + Population Audit:**
> 1. Add `SCOPE_MODEL_CLEAN = 'model_clean'` to `apps/mockbets/services/moneyline_evaluation.py` alongside the existing scopes. Filter: `is_system_generated=True` AND `recommendation_status` non-empty AND `recommendation_tier` non-empty AND `expected_edge` not None AND `recommendation_confidence` not None AND `placed_at >= 2026-05-06`.
> 2. Extend `_build_scope_summary` to surface the per-category exclusion counts from §3.1 of the diagnosis doc.
> 3. Update the eval template: add `Model Clean` to the scope dropdown; add a "Population Audit" sub-section showing the categorical breakdown.
> 4. Add the scope to `VALID_SCOPES`, `DEFAULT_SCOPE` unchanged (`actual`).
> 5. Add tests: `model_clean` excludes manual / pre-rules / incomplete; alignment contract (Law 3 transparency); UI renders the audit table.
>
> **Phase B — CLV source filter:**
> 1. In `apps/mockbets/services/recommendation_performance.py::_group_stats`, apply the same `odds_source='odds_api'` filter to CLV math that `apps/core/services/backtesting_service.py::evaluate_game` already applies. Specifically: a bet's `clv_cents` contributes to `clv_count` / `clv_positive` only when the bet's `odds_source` is `odds_api` (matches the framework spec).
> 2. Add `clv_excluded_by_source` to the summary so the operator sees how many bets were dropped from CLV math for non-primary-source reasons.
> 3. Add tests: ESPN-source bets do not contribute to CLV; the filtered rate matches the framework.
>
> **Constraints (strict):**
> - No recommendation behavior changes.
> - No threshold / gate / calibration constants modified.
> - No Elo activation.
> - No new predictive signals.
> - No emotional response to short-term outcomes.
>
> **Out of scope (do not start):**
> - Elo activation (waiting on Phase C evidence).
> - Phase 2B signal additions (deferred per the diagnosis).
> - Calibration retunes (deferred per Law 4).
>
> Update `docs/changelog.md`. Commit + push to `main`. Report back the corrected numbers when the operator returns from `/mockbets/moneyline-evaluation/?scope=model_clean` on production.

---

## 10. What this diagnosis is NOT

- ❌ Not a tuning prescription. No constants change.
- ❌ Not a verdict on the model's true quality. The 65-bet sample is mixed-population; the conclusion requires the Phase A clean-subset numbers.
- ❌ Not a verdict on Elo activation. The case for Elo is structurally strong but the empirical readiness requires Phase A and Phase B output, plus Phase C shadow verification.
- ❌ Not an emotional reaction. The framework explicitly forbids constant changes from 65-bet samples; this diagnosis follows the framework.
- ❌ Not a panic.

The system is showing exactly the failure mode it was architected to address. The architecture's response is being followed: measure, isolate, fix transparency first, intervene only with full evidence and clear rollback.

---

## 11. Closing note on the framework

This is the framework working as designed. Two weeks ago, a result like this would have triggered:
- "Tighten MIN_EDGE"
- "Drop the blend weight"
- "Suppress underdogs"
- "Flip Elo to True immediately"

Today, with `docs/architecture_laws.md` Laws 3 and 4 in place plus `docs/recommendation_quality_framework.md` published, the disciplined response is:
- "Audit the population first."
- "Verify the math second."
- "Capture the baseline third."
- "Activate the pre-designed structural fix fourth."
- "Observe."

That sequence is what separates a stable, learning system from a fragile, churn-driven one. The diagnostic itself is evidence that the governance layer is functioning. The fact that we *can* refuse to retune in the face of bad numbers — and instead investigate the data first — is the operational maturity Phase 2 was built to install.

---

*Diagnosis written 2026-05-16. Analysis only. Implementation awaits authorization per the §9 prompt.*
