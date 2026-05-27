# Method Replay — Lane-Corrected (2026-05-22)

**Status:** the tool is built and tested. **The actual production-data answers (Phase 1 corrected numbers, Phase 2 overlap matrix, Phase 3 verdict) cannot be produced from this dev worktree — there is no production game data here.** The operator must run the corrected replay on Railway after this commit deploys; the answers fall out from `/analytics/method-replay/` at that point.

This document covers:

1. What changed in this commit (the tool).
2. What the operator runs to get the actual answers.
3. What the math says we should expect, given the structural properties.
4. The Phase 3 verdict framework — what answer to give based on which numbers come back.

---

## 1. Tool changes

### 1.1 Lane-corrected `_simulate_recommendation`

`apps/analytics/services/method_replay.py::_simulate_recommendation` now mirrors the live `_moneyline_candidate` flow exactly:

1. `compute_status` → status, status_reason
2. `_raw_tier` → tier
3. **NEW:** `_pregame_movement_signal(game, pick_side)` — pre-game-anchored movement signal.
4. **NEW:** `_lane_classify(...)` — produces `lane`, `risk_flags`, `risk_score`.
5. **NEW:** Tier='blocked' override (defense in depth).
6. **NEW:** `is_lane_corrected_recommended = (status == 'recommended' AND lane == 'core')` — the production-equivalent recommendation indicator.

### 1.2 `_pregame_movement_signal` — leakage-safe movement helper

The live `movement_signal_for_pick` cutoff is `timezone.now() - HISTORY_MAX_HOURS`. For live use this is correct (recommendations are generated before first_pitch; all in-window snapshots are pre-game by temporal context). For replay on historical games where NOW is days past first_pitch, calling it directly returns empty (no snapshots in last 24h).

The new helper anchors the time window on `game.first_pitch` instead of `now`:

```python
cutoff = game.first_pitch - timedelta(hours=HISTORY_MAX_HOURS)
snaps = OddsSnapshot.objects.filter(
    game=game,
    captured_at__gte=cutoff,
    captured_at__lt=game.first_pitch,  # L1 safeguard: pre-game only
).order_by('-captured_at')[:HISTORY_MAX_SNAPSHOTS * 3]
```

Uses the SAME math (`_per_market_signal`, `classify_score`, `_direction_for_attr` from `apps.core.services.odds_movement`) — only the time anchor differs. The `captured_at__lt=game.first_pitch` filter is explicit defense in depth (even though the cutoff range is already pre-game-anchored, this filter excludes any pathological snapshot at or after first_pitch).

**Locked by test:** `test_movement_signal_pregame_filters_post_game_snapshots` adds a post-game snapshot with extreme values, computes the signal with and without it, and asserts byte-equal results.

### 1.3 Output shape — both uncorrected and corrected metrics per variant

`run_replay()` now emits, per variant:

```python
{
    'label': 'Replay 0.55',
    'blend_weight': 0.55,
    'simulations': [SimulatedRecommendation, ...],
    'recommended_count': N_uncorrected,       # status='recommended' only
    'lane_corrected_count': N_corrected,      # AND lane='core'
    'metrics': {...},                         # over uncorrected set
    'metrics_corrected': {...},               # over lane-corrected set
    'demoted_count': N_uncorrected - N_corrected,
    'demoted_by_flag': {                      # which flags caused demotions
        'market_conflict': X,
        'sanity_mismatch': Y,
        'thin_edge': Z,
        ...
    },
}
```

And cross-variant:

```python
'diff_first_two': diff_recommendations(...),               # uncorrected
'diff_first_two_corrected': diff_recommendations(..., use_lane_corrected=True),
```

### 1.4 Template

`/analytics/method-replay/` now shows TWO ROWS per variant in the comparison table — Uncorrected and Lane-corrected, with the corrected row highlighted blue. The lane-demotion breakdown card surfaces which risk flags caused the demotions (operator-readable).

### 1.5 Tests

9 new tests in `LaneCorrectedReplayTests` + `ProductionEquivalenceAssertionTests`:

- `test_simulation_now_emits_lane_and_corrected_flag` — schema check.
- `test_short_fav_thin_cannot_demote_replay_recommended_picks` — locks the §1 audit math (this flag is structurally unreachable on edge ≥ 6pp picks).
- `test_movement_signal_pregame_filters_post_game_snapshots` — L1 safeguard for the new helper.
- `test_lane_corrected_count_is_subset_of_uncorrected` — corrected ⊆ uncorrected, always.
- `test_demoted_by_flag_categorizes_excluded_picks` — alignment math (demoted_count = recommended - lane_corrected).
- `test_metrics_corrected_uses_lane_filtered_population` — `metrics_corrected.count` = lane_corrected_count.
- `test_diff_first_two_corrected_uses_lane_filter` — corrected diff structure.
- `test_lane_classification_uses_pregame_movement_signal` — integration of the pre-game signal into lane classification.
- `test_lane_corrected_passes_is_bulk_moneyline_eligible_predicate` — production-equivalence assertion: every corrected sim passes the canonical bulk-eligibility predicate.

Plus the 18 prior method-replay tests still pass. 27 total in this module. 1063 across phase-relevant modules. Zero regressions. `manage.py check` clean.

---

## 2. Operator: how to run the actual replay on production

After Railway redeploys this commit:

### 2.1 Phase 1 — Corrected replay

Visit, in order:

```
/analytics/method-replay/?range=7d&weights=0.40,0.55
/analytics/method-replay/?range=14d&weights=0.40,0.55   (manual date_from/date_to)
/analytics/method-replay/?range=30d&weights=0.40,0.55
```

The comparison table for each window shows two rows per variant (uncorrected + lane-corrected). Compare the corrected rows against each other and against the user's reported uncorrected numbers (108 / 72-36 / +16.2% for 0.40; 67 / 48-19 / +24.4% for 0.55 at 30 days).

The expected delta table to fill in:

| Variant | Uncorrected (old) | Corrected (new) | Δ count | Δ win % | Δ ROI |
|---|---|---|---|---|---|
| 30d Replay 0.40 | 108 recs, 66.7%, +16.2% | _operator fills_ | _negative_ | _likely small_ | _likely small_ |
| 30d Replay 0.55 | 67 recs, 71.6%, +24.4% | _operator fills_ | _negative_ | _likely small_ | _likely small_ |

### 2.2 Phase 2 — Production overlap audit

The replay shows the simulations on the LEFT side of the comparison. The operator needs to compare against the RIGHT side (actual bets + actual recommendations). Surfaces to use:

- `/mockbets/moneyline-evaluation/?range=30d&scope=actual` — all bets placed
- `/mockbets/moneyline-evaluation/?range=30d&scope=model_clean` — system_generated + complete snapshot + post-rules
- `/mockbets/moneyline-evaluation/?range=30d&scope=recommended` — system_generated only

For a proper game-by-game overlap matrix (what the user asked for), there is no current surface that produces that table directly. The Phase 2 question requires a one-off query on production. The operator could run on Railway:

```python
# Pseudocode for the operator:
from apps.analytics.services.method_replay import run_replay
from apps.core.models import BettingRecommendation
from apps.mockbets.models import MockBet

# Replay
result = run_replay(date_from, date_to, [0.55])
replay_sims = {s.game_id: s for s in result['variants'][0]['simulations']}

# Production recommendations (what live engine emitted)
prod_recs = BettingRecommendation.objects.filter(
    sport='mlb', mlb_game__first_pitch__date__range=(date_from, date_to),
)

# Actual bets (what user placed)
actual_bets = MockBet.objects.filter(
    sport='mlb', bet_type='moneyline',
    placed_at__date__range=(date_from, date_to),
)

# Build matrix per game:
# - replay_sims[g_id].is_lane_corrected_recommended
# - prod_rec.status == 'recommended' AND prod_rec.lane == 'core'
# - actual_bet exists AND is_system_generated AND lane_at_placement == 'core'
```

Building this as a UI in the same commit would have expanded scope significantly. **If the Phase 1 corrected numbers warrant deeper investigation, the Phase 2 matrix is the next commit's deliverable.**

### 2.3 What to look for

Compare Phase 1 corrected 0.55 (30d) to Phase 2 Model Clean (30d):

| If you see this | Verdict |
|---|---|
| Corrected replay 0.55 ≈ +15-25% ROI, ≈ 65-72% win rate, ≈ 50-60 recs AND Model Clean ≈ similar over same window | **Methodology is sound. Production execution matches.** No further methodology work needed; continue Roadmap B Step 1 observation. |
| Corrected replay 0.55 ≈ +15-25% ROI, ≈ 65-72% win rate AND Model Clean is materially worse (e.g., -15% ROI, 40% win rate) on the SAME games | **Methodology is sound. Production execution has diverged.** Phase 2 game-by-game matrix needed to identify divergence cause (UX bugs, manual placements, lane-misclassification at placement time, etc.). |
| Corrected replay drops sharply from uncorrected (e.g., 30 recs instead of 67, ROI near zero) | **Methodology is overstated, lane filter was the explanation after all.** Reopen earlier "replay is inflated" hypothesis; consider reverting blend weight. |
| Corrected replay stays similar to uncorrected (e.g., 60 recs vs 67, ROI similar) | **Lane filter is a small correction.** My re-audit math is confirmed. Methodology has real signal. |

---

## 3. What the math predicts (the §1 re-audit, restated for record)

The re-audit established:

- `short_fav_thin` cannot fire on replay-recommended picks (math: edge ≥ MIN_EDGE = 6pp → edge_decimal ≥ 0.06 → not strictly less than 0.06).
- `sanity_mismatch` chalk branch cannot fire (probability < 0.55 fails MIN_PROBABILITY = 0.60).
- `sanity_mismatch` dog branch can fire rarely.
- `thin_edge` rarely fires on edge ≥ 6pp picks with standard vig.
- `insight_conflict` always False.
- `market_conflict` is the only flag with meaningful frequency (~10–15% of picks).

**Prediction:** the corrected replay will show ~10–20% demotion from uncorrected count. 67 → ~55–60 recommendations under 0.55 for the 30-day window. Win rate likely stable or marginally better (demoting `market_conflict` picks is mildly negative-correlated with win rate — the market is right more often than not). ROI similar.

**If this prediction holds**, the methodology has real signal and the production-vs-replay gap is fully explained by population contamination (Phase 2 audit pending).

**If demotion is much higher than 10–20%** (say, 67 → 30), my re-audit math was wrong and the prior critical audit was right. The replay overstated, the methodology is weaker than it looks. That requires a different action — likely a Roadmap B Step 1 rollback.

---

## 4. Phase 3 verdict framework

Per the user's mission: state directly whether methodology is sound or whether production is broken. The framework below maps outcomes to verdicts.

### Verdict A — "Methodology appears fundamentally sound. The remaining problem is production execution."

**Trigger:** Corrected replay 0.55 over 30 days shows ≥ 50 recommendations, ≥ 60% win rate, ≥ +10% ROI. Production Model Clean over the same window shows materially worse numbers (ROI gap ≥ 20%).

**Action:** Phase 2 game-by-game matrix to find execution divergence. Likely candidates: pre-2026-05-22 trust-repair UX bugs that mis-labeled which bets were Recommended; manual placements on Potential games; pre-rules-tightening bets that should have been excluded by Phase A scope.

### Verdict B — "Methodology has real signal but is moderately predictive, not strongly."

**Trigger:** Corrected replay 0.55 shows ≥ 50 recommendations, 55–62% win rate, +5 to +12% ROI. Lower than uncorrected but still positive.

**Action:** Continue Roadmap B Step 1 observation. The methodology works at the moderate end. No rebuild justified.

### Verdict C — "Methodology is barely signal; replay was overstating."

**Trigger:** Corrected replay 0.55 shows ≤ 35 recommendations or win rate ≤ 55% or ROI ≤ +3%.

**Action:** Roadmap B Step 1 rollback candidate. Reopen methodology design discussion. The prior critical audit was right.

### Verdict D — "Insufficient evidence."

**Trigger:** Corrected replay 0.55 shows < 20 recommendations (sample too small for binomial CIs to be informative).

**Action:** Extend window to 60 days. If still under-sampled, methodology evaluation is sample-limited; default to Roadmap B Step 1 prospective evidence (Day 14 decision gate, ~2 weeks out).

---

## 5. What this commit does NOT do

- ❌ Does not actually run the corrected replay on production data (no production data available in dev worktree).
- ❌ Does not build the game-by-game overlap matrix (Phase 2). That requires either operator running on Railway or a follow-up commit.
- ❌ Does not change any production constants, thresholds, or recommendation logic.
- ❌ Does not change Elo, calibration, or any model component.
- ❌ Does not affect live behavior in any way. `/analytics/method-replay/` is a staff-only diagnostic surface; new fields render only on that page.

What it DOES do:

- ✅ Builds the lane-corrected replay tool the prior audit recommended.
- ✅ Uses pre-game-anchored movement signal (no leakage).
- ✅ Surfaces both uncorrected and corrected metrics so the operator can see the delta directly.
- ✅ Categorizes lane demotions by risk flag for operator readability.
- ✅ Locks the behavior with 9 new tests + the existing 18 = 27 tests.
- ✅ Stages the Phase 3 verdict framework so the operator's evidence falls cleanly into one of four buckets.

---

## 6. Architecture law compliance

- **Law 3** (scope transparency): satisfied — page documents safeguards inline, surfaces lane demotions explicitly.
- **Law 4** (do not overfit): satisfied — no constants changed; tool extends the existing replay to apply more of the production filter set. If the corrected numbers warrant a constant change, that change goes through Law 4 governance (sample, window, mechanism, rollback trigger) before shipping.

---

## 7. Honest acknowledgment

I cannot answer the user's mission ("prove or disprove whether production can match replay") from this worktree. The data needed is on production. The tool that produces the answer is now in the worktree (and about to be in production after this commit deploys).

The honest verdict is: **the test is now possible. The answer comes from running the tool on Railway.** Before this commit, the corrected replay didn't exist. After this commit, one URL visit produces the data the user is asking for.

If the user wants a stronger guarantee — e.g., committing seed data + running the replay against a known-outcome fixture — that's a deferred follow-up. The current commit is the necessary precondition.

---

*Lane-corrected method replay shipped 2026-05-22. The Phase 1 numbers come from operator running `/analytics/method-replay/?range=30d` on Railway. The Phase 3 verdict follows from those numbers per §4's framework.*
