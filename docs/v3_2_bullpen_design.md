# v3.2 — Bullpen Quality: Design Document

**Status:** DESIGN ONLY. No implementation yet.
**Author:** v3 architecture exercise — 2026-06-26
**Next Phase:** Phase 2 of the v3 roadmap (Recent Form was Phase 1).

This document follows the exact discipline that produced Recent Form: a pre-registered shadow → replay → mechanical ship criteria → activation flow, with the same flag-driven rollback path. No code is written here.

---

## 1. Why bullpen now

Per the Phase 5 strategic recommendation, **bullpens decide ~30% of MLB games** but contribute **zero** to the current Brother Willie score. Starting pitchers carry 65% of the predictive weight, yet the model has no concept of:

- Whether the team has a usable closer tonight
- Whether the top three relievers were thrown yesterday
- How the bullpen's recent run-prevention rate compares to season

This is the largest known coverage gap in BW's predictive layer.

---

## 2. Required data — the prerequisite

This is the most important section. **We do not currently have the data this feature requires.** Before any v3.2 implementation can begin, the following ingestion work is its hard prerequisite:

### 2A. What we have today
- `StartingPitcher` model: starter-only metadata + season stats.
- `Game` model: scores, dates, teams, starting pitchers.
- **No `Reliever` table.** No bullpen aggregate. No per-game reliever-appearance log.

### 2B. What v3.2 needs (rough)

| Data | Why | Source | New table? |
|---|---|---|---|
| Team-level bullpen aggregate (FIP/xFIP or RA/9 over a rolling window) | Bullpen "quality" baseline | MLB Stats API team-stats endpoint OR derived from per-game team pitching | Yes — `TeamBullpenStats` (team_id, as_of, last30_RA9, last30_FIP, etc.) |
| Per-game reliever appearances (who pitched, how many pitches, leverage) | Bullpen "fatigue" — closer used yesterday → unavailable today | MLB Stats API game-feed endpoint | Yes — `RelieverAppearance` (game_id, pitcher_id, pitches, leverage_index, days_ago) |

### 2C. Two-step data path

1. **v3.2-DATA-A:** Ingest team-level bullpen aggregates daily (a single per-team row updated each morning, sourced from MLB Stats API). Cheap. Enables the "bullpen quality" half of the feature.
2. **v3.2-DATA-B:** Ingest per-game reliever appearances (more expensive — requires walking the play-by-play / boxscore endpoint per game). Enables the "bullpen fatigue" half.

**It is acceptable for v3.2 to ship with only the v3.2-DATA-A half wired**, then add fatigue as v3.2-B once Phase 2A data ingestion is steady-state. This matches how Recent Form shipped with the W-L proxy first.

---

## 3. Feature engineering approach

### 3A. Two sub-features, additive in the score

The same architectural pattern as Recent Form: a small additive term in `_score()`, gated by a flag.

```
score += bullpen_term * 0.??? * weights['bullpen']
```

Where:

```
bullpen_term = (home_bullpen_signal − away_bullpen_signal)
bullpen_signal = α · bullpen_quality_delta + β · bullpen_fatigue_delta
```

`bullpen_quality_delta`: rolling team bullpen FIP/xFIP/RA9 normalized to a rating scale (50-centered), similar to how `StartingPitcher.rating` is normalized today.

`bullpen_fatigue_delta`: penalty term when the team's top relievers were used yesterday or threw heavy pitch counts.

### 3B. v3.2-A scope (ship-able with only team aggregate data)

```
bullpen_signal = bullpen_quality_delta
```

No fatigue term yet. Treat α = 1.0, β = 0.0 until v3.2-DATA-B lands.

### 3C. Weight calibration

Use the **same convention** as Recent Form: form on the same rating-scale, multiplied by `0.65 * weights['pitcher']` or a new `weights['bullpen']`. Open question for the design — let the replay decide whether the bullpen term should:
- Reuse the pitcher coefficient (`× 0.65`) — assumes bullpen and starter contribute equally
- Carry its own coefficient (`× 0.30` initial guess) — bullpens have less impact than starters but more than nothing
- Or be left at `1.0 × weights['bullpen']` with `weights['bullpen']=0.5` for symmetry with pitcher

**No tuning during this design phase.** The first replay experiment uses a single conservative coefficient (e.g. `× 0.30`) and the ship criteria decide whether it earns its place.

### 3D. Graceful degradation

- If no `TeamBullpenStats` row exists for the team → contribution = 0.0 (same pattern as Recent Form with insufficient history).
- If only one team has data, the bullpen term is still `home − away` but the missing side defaults to neutral.
- Audit capture lives in `feature_contributions.contributions_pp.bullpen_quality_score_units` and `…bullpen_fatigue_score_units`. Always populated when bullpen data is present, regardless of flag.

---

## 4. Replay experiment

### 4A. Endpoint

Mirror the Recent Form endpoint exactly:

```
GET /analytics/method-replay/?experiment=bullpen&days=90&blend=0.55
```

Staff-only. Plaintext. Returns A (production = Recent Form active) vs B (+bullpen) on identical historical slates.

### 4B. Variant logic

`_simulate_recommendation` gains an optional `use_bullpen` parameter. When True:

```
score += (home_bullpen_signal − away_bullpen_signal) * 0.30  # initial guess
```

Same leakage discipline: bullpen signals must anchor on `game.first_pitch` so the replay cannot peek at bullpen states that postdate the game being evaluated. The `TeamBullpenStats` table needs an `as_of` cutoff column for this reason.

### 4C. Output shape

Same as Recent Form experiment: headline metrics (A/B/Δ), per-bucket calibration table, mechanical SHIP CRITERIA, VERDICT line.

---

## 5. Pre-registered ship criteria

| # | Criterion | Mechanical check |
|---|---|---|
| 1 | ROI improves by ≥ +1.5pp | `b.roi − a.roi ≥ 1.5` |
| 2 | 60–65% calibration does not worsen | `\|b_err_60_65\| ≤ \|a_err_60_65\| + 1.0pp` |
| 3 | Recommendation volume stable | `0.7 × a.count ≤ b.count ≤ 1.5 × a.count` |
| 4 | No major bucket regression | For 65–70 / 70–75 / 75+: `\|b_err\| ≤ \|a_err\| + 5.0pp` |
| 5 | CLV does not materially worsen | `b.avg_clv − a.avg_clv ≥ −0.01` |
| 6 | **New for v3.2:** bullpen data coverage ≥ 80% | `≥ 80%` of evaluated games must have bullpen signal for both teams. Without it, the experiment is data-thin and the verdict is unreliable. |

Notes on the changes from Recent Form's criteria:

- **ROI threshold dropped from +2pp → +1.5pp.** Recent Form was a free first-step (used existing data). Bullpen requires new ingestion + maintenance, so the bar can be slightly lower if everything else is clean — but never below +1.5pp.
- **Volume window tightened.** Bullpen could mechanically suppress recs (a bad bullpen pulls a home favorite below threshold). We want it to nudge, not gut, the recommended set.
- **New criterion (#6) — data coverage.** Recent Form had data for every game (W/L is always available); bullpen needs reliever data. If coverage is thin the test is invalid.

---

## 6. Rollout strategy

### Phase 2A — DATA INGESTION (no model change)
1. Add `TeamBullpenStats` model + migration.
2. Add daily management command `ingest_mlb_bullpen_stats` to populate it from MLB Stats API.
3. Run in production for 7 days **before** any model code reads from it. Verify data freshness, coverage, and accuracy via a new staff endpoint `/analytics/bullpen-data-health/`.

**Ship gate:** ≥ 80% of teams have a row updated in the last 24 hours for 7 consecutive days.

### Phase 2B — SHADOW CAPTURE (no scoring change)
4. Add `apps/mlb/services/bullpen_quality.py` exposing `bullpen_quality_delta(team, *, reference_date)`. Returns rating-scale delta (same convention as `recent_form_delta`).
5. Add `use_bullpen=False` parameter to `_score()`. Compute the bullpen contribution **always** (so `feature_contributions` captures it for audit) but only **add** it to the score when the flag is on.
6. Add settings flag `USE_BULLPEN_QUALITY = os.environ.get(..., 'false') == true`. **Default `false`** (same default-OFF pattern Recent Form started with).
7. Add `?experiment=bullpen` view + plaintext output with mechanical ship criteria.

**At this point production is unchanged.** Bullpen contribution is captured silently.

### Phase 2C — REPLAY VALIDATION
8. Operator runs `?experiment=bullpen&days=90&blend=0.55` on Railway.
9. Reads the VERDICT line.
10. **PASS** → set `USE_BULLPEN_QUALITY=true` in Railway env vars → redeploy → activate.
   **FAIL** → leave flag off; shadow capture continues. Iterate on the coefficient (single variable change) or wait for v3.2-DATA-B (fatigue) to be available before re-testing.

### Phase 2D — POST-ACTIVATION OBSERVATION
11. After activation: 14-day observation with frozen methodology (mirrors the 0.55 blend observation discipline). Pre-registered rollback triggers:
    - 60–65% bucket calibration regresses by > 5pp vs pre-activation baseline.
    - System-approved ROI drops > 5pp below pre-activation 30-day trailing mean for 7+ consecutive days.
    - Either trigger → set `USE_BULLPEN_QUALITY=false` and capture a post-rollback scorecard.

---

## 7. Rollback

Same one-line discipline as Recent Form:

```bash
USE_BULLPEN_QUALITY=false   # Railway env var; no migration; no code change
```

Captured `feature_contributions` data is preserved (audit value).

---

## 8. Risks called out explicitly

1. **Ingestion fragility.** MLB Stats API team-stats endpoint changes shape occasionally; we'll need monitoring on the daily ingest cron.
2. **Coverage gaps early-season.** Bullpen aggregate stats need ~10+ games of data per team to stabilize. The ship criterion gate (#6, ≥80% coverage) handles this — but for the first ~2 weeks of an MLB season, the bullpen feature will be auto-suppressed via the missing-data graceful path.
3. **Confounding with starter recent form.** If a team's bullpen is poor AND the starter's recent W/L is bad, both signals will subtract from that team's win probability. This is correct behavior, but we should verify in replay that we're not double-counting via correlation. The mechanical ship criteria's "no major bucket regression" line catches this if it's severe.
4. **Reliever fatigue (v3.2-DATA-B) is the harder ingestion.** If v3.2-DATA-A passes ship criteria and we activate quality-only, we have a working bullpen feature for ~80% of the value. Fatigue can come in v3.3.
5. **Per-bucket isotonic calibration is still pending.** The Phase 5 strategic recommendation flagged calibration architecture as equal-priority to bullpen. The right sequence is: bullpen ships → 30-day observation → calibration architecture work. We do not stack feature additions during observation windows.

---

## 9. What NOT to do during v3.2

- ❌ Do NOT tune `MIN_PROBABILITY` or any other threshold.
- ❌ Do NOT touch the 0.55 blend weight.
- ❌ Do NOT add fatigue + quality together in a single replay. Test them in series, not parallel.
- ❌ Do NOT lower the calibration ship criterion to make the verdict easier. The pre-registered bar is the bar.
- ❌ Do NOT activate before the 7-day data-health gate passes.

---

## 10. Definition of done for v3.2

- [ ] `TeamBullpenStats` model + migration shipped.
- [ ] Daily ingest cron running in production for ≥ 7 days at ≥ 80% coverage.
- [ ] `apps/mlb/services/bullpen_quality.py` with `bullpen_quality_delta()` + unit tests.
- [ ] `_score()` accepts optional bullpen contribution, captures it on `feature_contributions` regardless of flag.
- [ ] `USE_BULLPEN_QUALITY` flag in settings, **default `false`**.
- [ ] `?experiment=bullpen` view + plaintext mechanical-verdict output.
- [ ] Tests: graceful no-data, leakage guard, flag-off invariance, flag-on adds term, staff 200, non-staff 403.
- [ ] `docs/v3_2_bullpen_validation_plan.md` with replay results + activation decision.
- [ ] Operator validation run on Railway → VERDICT line documented.
- [ ] If PASS: flag flipped, 14-day observation begins.
- [ ] If FAIL: shadow capture continues; design doc updated with iteration notes.

---

**One validated feature at a time. Bullpen earns its score weight or it stays in the shadow.**
