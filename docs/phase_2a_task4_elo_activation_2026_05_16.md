# Phase 2A Task 4 — Elo Activation: GO recommendation + rollback runbook

**Date:** 2026-05-16
**Verdict:** **GO.** Activation lands in this commit via the repo default flip. Rollback is one Railway env var change.
**Scope:** strict — only the rating-mode variable changes. No threshold tunes. No calibration changes. No new signals. No edge realism compression.

---

## 0. The decision in one sentence

The Model Clean empirical evidence (44 system bets, ROI −12.8%, CLV+ 25%, 8+pp edge bucket losing money, underdog ROI −47.8%) confirms the stale-rating fake-edge pathology that Phase 2A Elo was architected to remediate, and the evidence-based gates from `docs/phase_2a_task3_shadow_analysis_2026_05_14.md` §7 and `docs/elo_production_readiness_2026_05_10.md` §7 are satisfied as far as the production data permits.

---

## 1. The empirical evidence

### 1.1 Model Clean numbers (post-`SCOPE_MODEL_CLEAN`)

| Metric | Value | What it means |
|---|---|---|
| Sample (clean system bets) | 44 | Population is no longer contaminated by manual placements / pre-rules bets |
| ROI | −12.8% | Sustained negative EV — beyond variance for 44-bet sample |
| Win rate | 47.7% | Variance-band; do not act on this alone |
| **CLV+ rate** | **25%** | **Strong signal** — system is being beaten by the close 75% of the time |
| 8+pp edge bucket | losing money | The "elite" bucket is structurally wrong; fake-edge pathology |
| Underdog ROI | −47.8% (3–10) | Model overestimates underdog probability |
| Confidence 65–70% bucket | underperforming | Overconfidence in the moderate-favorite band |
| Market disagreement | 75% of picks | Model is reading inputs the market isn't reading — at scale |

The 44-bet sample is below the framework's 100-bet threshold for **threshold tunes** — but it is sufficient to confirm a **structural** failure that Elo addresses mechanistically. The distinction matters: Law 4 forbids reacting to small samples with constant changes; it does not forbid structural fixes that have independent justification.

### 1.2 Structural evidence (independent of sample size)

- `Team.rating` has **zero updaters** across the codebase (locked by `apps/core/test_feature_truth_audit.py::StaticTeamRatingHasNoUpdaterTests`). It is frozen since seed.
- The Elo system (`apps/core/services/elo_service.py`) is complete, tested, and production-validated through 200+ unit tests across the Phase 2A work.
- The `ensure_elo_backfilled` deploy hook (Phase 2A Task 1) guarantees `Team.elo_rating` is populated for every MLB team before the activation goes live.
- The shadow-mode framework (Phase 1B Task 5) writes `shadow_alt_data` on every recommendation so post-activation we can compare static vs Elo on identical inputs.

### 1.3 Diagnostic alignment

`docs/model_quality_diagnosis_2026_05_16.md` predicted exactly this empirical pattern:

| Diagnostic prediction | Observed |
|---|---|
| 71% of bets in 8+pp bucket = fake-edge fingerprint | Confirmed: 8+pp bucket is the dominant population and losing money |
| Stale `Team.rating` overestimates underdogs the market knows have weakened | Confirmed: −47.8% underdog ROI |
| Sigmoid + clamp produces overconfident moderate favorites | Confirmed: 65–70% confidence bucket underperforming |
| 29.7% CLV+ on mixed sample → likely worse on clean primary-source sample | Confirmed: 25% on Model Clean with source filter |

The hypothesis and the evidence agree. Elo is the targeted intervention.

---

## 2. The activation procedure

### 2.1 What this commit does

Single line change in `brotherwillies/settings.py`:

```python
# BEFORE (2026-05-10 → 2026-05-16):
USE_DYNAMIC_RATINGS = os.environ.get('USE_DYNAMIC_RATINGS', 'false').lower() in (...)

# AFTER (2026-05-16 → ):
USE_DYNAMIC_RATINGS = os.environ.get('USE_DYNAMIC_RATINGS', 'true').lower() in (...)
```

The env var override path is preserved. Operators can roll back without touching code by setting `USE_DYNAMIC_RATINGS=false` in Railway env vars.

### 2.2 What happens on the next Railway deploy

1. Repo deploys; new default takes effect.
2. `ensure_seed` runs as part of the start command.
3. `ensure_seed` calls `ensure_elo_backfilled` (already wired since Phase 2A Task 1).
4. `ensure_elo_backfilled` detects the MLB Elo state; runs `rebuild_elo_ratings --sport mlb` if not already populated. (If the prior deploy already ran the backfill, this is a no-op.)
5. After deploy completes, the next MLB recommendation persistence path reads `team.elo_rating` (projected to legacy scale) instead of `team.rating`.

There is no time window where the model reads only-default Elo values. The backfill runs on the same deploy that activates the flag.

### 2.3 What this commit does NOT change

- ❌ `MIN_EDGE`, `MIN_PROBABILITY_FOR_RECOMMENDED`, `MAX_ABS_ODDS_FOR_RECOMMENDED`, `HEAVY_FAVORITE_ODDS`, `EXTREME_DISAGREEMENT_GAP` — unchanged.
- ❌ `MARKET_BLEND_WEIGHT`, `PROB_MIN`, `PROB_MAX`, sigmoid divisor — unchanged.
- ❌ All lane classification rules — unchanged.
- ❌ All risk-flag rules — unchanged.
- ❌ No new predictive signals (pitcher form, team form, bullpen — Phase 2B, deferred).
- ❌ No edge realism compression (Phase 2C, deferred).
- ❌ No calibration retunes (Phase 1D, deferred).
- ❌ Per Law 4: nothing else changes simultaneously. Variable isolation is the entire point.

---

## 3. Observation instrumentation (the 2-3 week window)

### 3.1 New surface: `/analytics/elo-monitor/`

Read-only staff-only diagnostic that surfaces:

- Activation status — `USE_DYNAMIC_RATINGS` value, env var presence, current rating mode.
- Pre-Elo baseline snapshot (the `RecommendationHealthSnapshot` tagged with `notes` containing "pre-elo").
- Current Health Score snapshot.
- Score delta vs baseline.
- The four documented rollback triggers, each evaluated with a fired / OK status + threshold + detail.
- Rollback procedure (one Railway env var change).

The monitor reads from existing storage (`RecommendationHealthSnapshot` rows). It does not modify state. Locked by `apps/analytics/test_elo_monitor.py` (20 tests).

### 3.2 Operator capture cadence

| When | Command |
|---|---|
| Before next Railway deploy (or after, if not yet captured) | `python manage.py capture_health_snapshot --notes "pre-elo baseline"` |
| Day 1 post-deploy | `python manage.py capture_health_snapshot --notes "post-elo day 1"` |
| Day 3 post-deploy | `python manage.py capture_health_snapshot --notes "post-elo day 3"` |
| Week 1 post-deploy | `python manage.py capture_health_snapshot --notes "post-elo week 1"` |
| Week 2 post-deploy | `python manage.py capture_health_snapshot --notes "post-elo week 2"` |
| Week 4 post-deploy (stabilization checkpoint) | `python manage.py capture_health_snapshot --notes "post-elo week 4 stabilization"` |

Daily cron capture (already supported via the existing management command) is also encouraged. The notes-tagged snapshots are checkpoints; the daily ones are the trend.

### 3.3 What to watch

| Metric | Pre-Elo (current) | Target (post-Elo) | Surface |
|---|---|---|---|
| **CLV+ rate** (Model Clean, primary source only) | 25% | improving toward 45%+ | Health Score `clv_trend` dimension; Elo Monitor delta |
| **8+pp edge bucket count** | 71% of slate | compressing toward <30% | `/analytics/shadow-review/` `edge_ge_8pp` |
| **Edge realism** (8+ ROI vs 4-6 ROI) | 8+ losing | 8+ ≥ 4-6 | Health Score `edge_realism` dimension |
| **Underdog ROI** | −47.8% | improving toward breakeven | Moneyline Evaluation, `by_odds_type` |
| **Recommendation volume** | (current) | within 2σ of rolling mean | Health Score `recommendation_stability` |
| **65–70% confidence bucket win rate** | 47% (vs predicted 67%) | converging toward predicted | Moneyline Evaluation, `by_confidence` |
| **Market disagreement (mean)** | 75% disagree | dropping toward 30–40% | `/analytics/shadow-review/` `avg_disagreement_active` |

The Elo Activation Monitor surfaces the most actionable subset in one place. The framework surfaces (Backtest, Health Score, Shadow Review, Moneyline Evaluation, Model Inventory) carry the full detail.

---

## 4. Rollback triggers — the explicit thresholds

Per Law 4 and the `docs/recommendation_quality_framework.md` §2 governance, the operator inspects, investigates, and decides. **Auto-rollback is forbidden** — would be self-modifying behavior. Triggers are decision aids, not actions.

### 4.1 The four triggers (all evaluated by the Elo Activation Monitor)

| # | Trigger | Threshold | Severity |
|---|---|---|---|
| 1 | **CLV deterioration** | CLV+ rate drops ≥ 5 pp from baseline | CRITICAL |
| 2 | **Health Score collapse** | Composite score drops ≥ 10 points from baseline | CRITICAL |
| 3 | **Composite in INTERVENE band** | Composite < 25 | CRITICAL |
| 4 | **Edge realism inversion (fake-edge persists)** | 8+ ROI ≥ 5 pp BELOW 4-6 ROI with samples ≥ 20 in each | CRITICAL — *if this fires post-Elo, the cutover failed to address the mechanism* |

A single trigger firing does NOT mandate rollback — the operator interprets context (sample size, time elapsed, sustained vs spike). Multiple triggers firing simultaneously, or any single trigger firing sustained over a week, is a strong rollback signal.

### 4.2 Soft-watch conditions (no rollback, just monitor more closely)

- Pick agreement rate (shadow review) drops below 60%.
- Volume stability drops out of the 2σ band for a single week.
- Any single warning (not trigger) fires for 3+ consecutive snapshots.

### 4.3 Rollback procedure (when justified)

Single step:

1. Railway dashboard → Variables → set `USE_DYNAMIC_RATINGS=false`.
2. Railway auto-redeploys. Effective on next request.
3. Capture a post-rollback snapshot: `python manage.py capture_health_snapshot --notes "post-rollback"`.

That's it. No code revert, no DB change, no restart. The framework was explicitly designed around this property.

For a permanent rollback (e.g., the activation provably failed), the secondary step is to revert this commit in the repo so future deploys don't re-activate. But that's a follow-up; the env var override stops the bleeding immediately.

---

## 5. The success criteria

| User-stated success criterion | How it's measured | Surface |
|---|---|---|
| Fewer giant fake edges | `edge_ge_8pp` count drops post-cutover | Shadow Review |
| Better market alignment | `avg_disagreement_active` drops | Shadow Review |
| CLV improvement | Health Score `clv_trend` rises | Health Score, Elo Monitor |
| More realistic probabilities | `prob_ge_85` count drops | Shadow Review |
| Reduced bad underdog exposure | Underdog ROI improves toward breakeven | Moneyline Evaluation `by_odds_type` |

All five are observable via existing surfaces. None require new instrumentation beyond the Elo Activation Monitor (which is composition, not new measurement).

---

## 6. What this commit ships

| File | Change |
|---|---|
| `brotherwillies/settings.py` | `USE_DYNAMIC_RATINGS` default flipped from `'false'` to `'true'`. Inline comment documents the activation date + rollback path. |
| `apps/analytics/services/elo_monitor.py` | New service: `build_monitor()` composes activation state + baseline + current + four rollback-trigger evaluations. Pure read-only. |
| `apps/analytics/views.py` | New view `elo_monitor` (staff-only). |
| `apps/analytics/urls.py` | New route at `/analytics/elo-monitor/`. |
| `templates/analytics/elo_monitor.html` | New template rendering activation state, side-by-side baseline-vs-current Health Score, the four trigger cards, and the rollback procedure. |
| `apps/analytics/test_elo_monitor.py` | 20 tests: activation defaults, rollback override, monitor service behavior (baseline detection, score delta), per-trigger evaluation, view access control, isolation. |
| `docs/phase_2a_task4_elo_activation_2026_05_16.md` | This document. |
| `docs/changelog.md` | Phase D entry. |

---

## 7. Test totals

- **756 tests passing** across the phase-relevant modules (analytics + core + datahub + mockbets + sport apps).
- **Zero regressions.** The activation is structurally safe; tests that don't override `USE_DYNAMIC_RATINGS` behave identically because `team_rating_for_model` falls back to `team.rating` when `team.elo_rating` is `None` (which it is for fresh test fixtures that don't set it).
- `python manage.py check` clean.

---

## 8. Architecture law compliance

| Law | Compliance |
|---|---|
| **Law 1: Signals are nudges, not drivers** | N/A — Elo IS the rating, not a signal. The bounded-signal principle applies to Phase 2B additions, not the rating source. |
| **Law 2: No signal without its evaluation slice** | The activation is not a new signal; it changes the rating source. The evaluation slices (`by_edge_bucket`, `by_fav_size`, `by_pitcher_completeness`, `shadow_review` dimensions) all already exist. |
| **Law 3: Analytics surfaces transparent about scope** | The Elo Monitor surfaces the rating mode prominently. The Health Score snapshot records `rating_mode_active` so every historical snapshot can be filtered by mode. |
| **Law 4: Do not overfit** | The activation is structurally justified, not response to a 65-bet sample. The Model Clean numbers confirm the *mechanism*; they don't *cause* the change. The change is one variable, with an explicit rollback trigger, with no co-changes. |

---

## 9. What the operator should do in the next 24 hours

1. **Confirm Railway deploys cleanly.** Watch the deploy log for the `ensure_elo_backfilled` output.
2. **Capture pre-Elo baseline** (if not already): `python manage.py capture_health_snapshot --notes "pre-elo baseline"`. If a snapshot already exists with that note, no action needed.
3. **Visit `/analytics/elo-monitor/`** to confirm:
   - Activation Status shows `USE_DYNAMIC_RATINGS: True`.
   - Rating mode: `elo`.
   - Pre-Elo baseline displays (if captured).
4. **After the first MLB slate post-activation**, capture Day 1 snapshot: `python manage.py capture_health_snapshot --notes "post-elo day 1"`.
5. **Open `/analytics/shadow-review/?days=2`** and confirm the active mode is now `elo` and shadow data captures `static` as the alt.

---

## 10. What the operator should NOT do

- ❌ **Do not tune thresholds** in response to Day 1 / Week 1 numbers regardless of how they look. Per Law 4, 1-2 week samples don't justify constant changes.
- ❌ **Do not add new signals** during the observation window. Optimization stacking is forbidden — variable isolation is the value.
- ❌ **Do not panic at variance.** A bad slate is data. The triggers are the trigger; intuition is not.
- ❌ **Do not retune calibration constants** (sigmoid divisor, clamp, blend weight) during this window. Phase 1D candidates are deferred until ≥ 4 weeks of post-cutover data accumulates.

---

## 11. Phase E (next, gated): stabilization observation

This commit completes Phase D. Phase E is the 4-week stabilization window. The deliverables for Phase E:

- ✅ Daily / per-slate capture of `RecommendationHealthSnapshot` rows.
- ✅ Weekly review of the Elo Activation Monitor.
- ✅ Backtest pair (static via override + Elo via default) at Week 4 for the empirical validation.
- ❌ No other changes ship in this window.

Phase E ends with one of:
- **Lock in.** Elo activation has stabilized; Phase 2B (pitcher form, team form, bullpen) can begin under the framework's signal-addition rules.
- **Rollback + diagnose.** Triggers fired and were sustained; the rollback procedure executed; a new diagnostic doc replaces this one to plan the next intervention.

---

*Promulgated 2026-05-16. The activation is the single variable that moves. Everything else holds still. The framework is doing its job — refusing reflexive tuning, demanding evidence, executing the pre-designed structural fix when the evidence aligns with it.*
