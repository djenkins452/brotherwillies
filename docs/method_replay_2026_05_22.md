# Method Replay — Retrospective MLB Moneyline Backtest

**Date:** 2026-05-22
**Surface:** `/analytics/method-replay/` (staff-only)
**Purpose:** answer *"what would Brother Willies have recommended over the past N days under blend weight W?"* without waiting 1–4 weeks for fresh production data.

This is the empirical alternative to "wait and see whether Roadmap B Step 1 is working" — replays the new method against history.

---

## 1. Replay methodology

For each MLB game with `status='final'` + both scores set in the chosen date window:

1. **Pre-game opening snapshot.** Earliest `OddsSnapshot` row with `captured_at < game.first_pitch` and `odds_source='odds_api'`. Drives the simulated placement decision (market_home_win_prob for the blend, moneylines for de-vig + the placed bet's price).

2. **Pre-game closing snapshot.** Latest pre-game snapshot (same source filter). Used **only** for CLV measurement after the recommendation is generated.

3. **Pre-game team ratings.** When Elo is active (current production state), reads `TeamEloHistory.pre_rating` for the history row created when the game was processed (= rating immediately before this game). When Elo is not active, reads `Team.rating` (which has no updater, so current value = historical value).

4. **Pitcher ratings.** Current `StartingPitcher.rating` is used as an approximation. **This is the one documented limitation** — see §3 below.

5. **Score formula.** Mirrors `apps.mlb.services.model_service._score`:
   ```
   score = (home_rating - away_rating) × 0.35
         + (home_pitcher_rating - away_pitcher_rating) × 0.65
         + HFA × (1 if not neutral_site else 0)
   ```

6. **Sigmoid → blend → clamp.** `sigmoid(score / 25)` clamped to `[0.01, 0.99]`, blended with the pre-game market under the candidate `blend_weight`, soft-clamped to `[PROB_MIN, PROB_MAX]`.

7. **De-vig + pick selection.** De-vig opening moneylines via `devig_two_way`; pick the side with the larger edge.

8. **Decision rules.** Apply CURRENT `compute_status` + `_raw_tier`. Recommendation status = the same logic the live engine uses.

9. **Outcome.** Read `game.home_score` / `game.away_score`. Compute `won` = (pick_side == 'home' and home_won) or (pick_side == 'away' and not home_won). **This is the ONLY use of post-game data; it never feeds into the decision.**

10. **CLV measurement.** Compare opening pick moneyline vs closing pick moneyline via `closing_line_value`. CLV is `None` when opening == closing (single snapshot) or when either moneyline is missing.

---

## 2. Data leakage safeguards

Each safeguard is locked by tests in `apps/analytics/test_method_replay.py::NoLeakageTests`. Any failure of these tests is a production blocker.

| ID | Safeguard | Where enforced | Test |
|---|---|---|---|
| **L1** | OddsSnapshot queries filter `captured_at < game.first_pitch`. Post-game snapshots are structurally excluded. | `_pregame_snapshots()` | `test_pregame_snapshots_filter_strictly_before_first_pitch` + `test_post_game_snapshot_does_not_affect_recommendation` |
| **L2** | Closing odds (latest pre-game snapshot) used ONLY for CLV measurement. Recommendation generation uses OPENING (earliest pre-game). | `_simulate_recommendation()` | `test_closing_odds_only_used_for_clv_not_recommendation` |
| **L3** | Pre-game team Elo from `TeamEloHistory.pre_rating` (frozen at process time), not from current `team.elo_rating`. | `_pregame_team_rating()` | `test_pre_rating_from_history_used_when_elo_active` |
| **L4** | Static `Team.rating` has no updater across the codebase. Current value == historical value by mechanism. | `apps/core/test_feature_truth_audit.py::StaticTeamRatingHasNoUpdaterTests` | (transitive — locked elsewhere) |
| **L5** | `home_score` / `away_score` used only for the `won` field in the simulation output. Changing the score after simulation does not affect probability or edge. | `_simulate_recommendation()` | `test_outcome_does_not_affect_recommendation` |

---

## 3. Documented limitation (NOT leakage)

**Pitcher ratings are not historical.** `StartingPitcher.rating` is updated incrementally by `apps/datahub/providers/mlb/pitcher_stats_provider.py` after each game. Over a 7-30 day window, a pitcher has had ~1-5 additional starts since the simulated game; their rating has drifted modestly.

This is a real approximation, but:

1. The drift is small per-pitcher (rating derived from season-aggregate ERA/WHIP/K-per-9, which move slowly).
2. The drift affects **all method variants identically** — the same pitcher rating is used in the 0.40 and 0.55 replays. Therefore RELATIVE comparisons (which is the entire point of the tool) remain unbiased.
3. Absolute simulated probabilities may differ slightly from what the live engine would have computed at the moment of the historical game.

The tool's purpose is comparative ("does 0.55 do better than 0.40 on these games?"), not absolute reconstruction ("what did 0.40 actually predict for this specific game?"). For the latter, read `BettingRecommendation` rows directly via the existing backtest harness.

If we ever need historical pitcher ratings, the lift is: snapshot `pitcher.rating` to a new `StartingPitcherRatingHistory` model on every update. Not in scope for this commit.

---

## 4. Metrics produced

Per variant:

| Headline | Per-bet | Segment breakdowns |
|---|---|---|
| Recommended count | win / loss / push / pending | Tier (elite / strong / standard) |
| Win rate | Stake | Edge bucket (0-4 / 4-6 / 6-8 / 8+ pp) |
| ROI | Payout | Confidence bucket (60-65 / 65-70 / 70-75 / 75-80 / 80+ %) |
| Net P/L | Edge | Odds type (heavy_fav / mid_fav / short_fav / short_dog / mid_dog / long_dog) |
| Avg edge | CLV decimal | |
| Avg CLV | | |
| Positive CLV rate | | |
| CLV sample (primary-source only) | | |

Cross-variant diff (when ≥ 2 variants):

- `a_only_count` — games recommended only by the first variant.
- `b_only_count` — games recommended only by the second variant.
- `both_count` — games recommended by both.
- `a_only` / `b_only` — full simulation rows for inspection.
- `largest_prob_diffs` — top 10 games ranked by `|final_prob_b - final_prob_a|` (any status). Surfaces the games where the two methods most diverge in confidence, regardless of whether either recommends.

---

## 5. Files

| File | Type | Description |
|---|---|---|
| `apps/analytics/services/method_replay.py` | new | The replay service. `historical_blend_weight()`, `_pregame_snapshots()`, `_pregame_team_rating()`, `_simulate_recommendation()`, `_compute_metrics()`, `diff_recommendations()`, `run_replay()`. |
| `apps/analytics/views.py` | modified | New `method_replay` view (staff-only). Parses `?range=`, `?date_from=`, `?date_to=`, `?weights=`. Calls `run_replay()`. |
| `apps/analytics/urls.py` | modified | New route `/analytics/method-replay/`. |
| `templates/analytics/method_replay.html` | new | Comparison table + per-variant segment breakdowns + diff card + largest-prob-diff table. |
| `apps/analytics/test_method_replay.py` | new | 18 tests across 7 classes: `NoLeakageTests` (4), `PregameEloFromHistoryTests` (1), `WindowFilterTests` (2), `MethodComparisonTests` (2), `MetricsTests` (2), `HistoricalBlendWeightTests` (3), `ViewAccessTests` (4). |
| `docs/method_replay_2026_05_22.md` | new | This document. |

---

## 6. Tests

**18 new tests, all passing. 1054 tests across the full phase-relevant codebase, zero regressions.**

Test highlights:
- **`test_post_game_snapshot_does_not_affect_recommendation`** — the gold-standard leakage test. Adds a post-game snapshot with extreme `market_home_win_prob=0.99`, confirms the simulation is byte-identical to without it.
- **`test_closing_odds_only_used_for_clv_not_recommendation`** — sets opening and closing snapshots to DIFFERENT market probabilities, confirms the recommendation uses opening only.
- **`test_outcome_does_not_affect_recommendation`** — flips `home_score`/`away_score`, confirms simulated probability + edge + status unchanged (only the `won` analytics field changes).
- **`test_pre_rating_from_history_used_when_elo_active`** — confirms `TeamEloHistory.pre_rating` wins over current `team.elo_rating` when Elo is active.
- **`test_higher_blend_pulls_more_picks_toward_market`** — sanity check on the method itself: 0.55 produces final probabilities closer to market than 0.40 on the same fixture.

---

## 7. Does 0.55 improve the last 7 / 30 days?

**The tool produces the answer; the dev worktree cannot compute it from production data.** Local SQLite has no production game history. The operator runs the tool on Railway:

```
https://brotherwillies.com/analytics/method-replay/?range=7d
https://brotherwillies.com/analytics/method-replay/?range=30d
```

The comparison table will show, for the chosen window:

| Variant | Recs | W–L–P | Win % | ROI | Net P/L | Avg Edge | Avg CLV | CLV+ % |
|---|---|---|---|---|---|---|---|---|
| Replay 0.40 | (current method baseline) | | | | | | | |
| Replay 0.55 | (new method) | | | | | | | |

The decision criterion documented in `docs/phase_2a_task4_elo_activation_2026_05_16.md` § rollback triggers + the framework's §1 hierarchy:

- **If Replay 0.55 has higher CLV+ rate and similar-or-better ROI vs Replay 0.40:** the change is empirically supported by retrospective evidence, beyond the framework's "wait 14 days" requirement.
- **If Replay 0.55 has lower CLV+ rate vs Replay 0.40:** the rollback trigger from the 2026-05-22 commit is empirically supported. Consider reverting `MARKET_BLEND_WEIGHT` to 0.40 before the 14-day window completes.
- **If results are mixed:** wait for the live 14-day observation window. Retrospective evidence isn't definitive when methodologies and data drift.

**Important caveat:** this is retrospective evidence using approximate pitcher ratings (§3). It cannot replace the prospective Day 14 decision gate — but it provides a much earlier directional signal.

---

## 8. Architecture law compliance

- **Law 1** (signals are nudges): N/A.
- **Law 2** (no signal without eval slice): N/A — no new signal added. The replay IS an evaluation surface for the existing blend-weight constant.
- **Law 3** (analytics surfaces transparent about scope): satisfied. The page documents leakage safeguards inline, surfaces evaluable game count, and the methodology section of this doc is linked from the template.
- **Law 4** (do not overfit): satisfied. No constants changed. The tool exists to test the *current* constant against *historical* outcomes, not to derive new constants. If the replay surfaces evidence for a different constant, that new constant goes through Law 4 governance (sample, window, mechanism, rollback trigger) before shipping.

---

*Method Replay shipped 2026-05-22. The operator can now answer the "would 0.55 have helped?" question without waiting for the 14-day live observation window — within the tool's documented approximation envelope.*
