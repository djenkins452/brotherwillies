# v3.1 — Starter Recent-Form Validation Plan

**Status: 🟢 PRODUCTION ACTIVE — 2026-06-26.**
**Flag:** `USE_STARTER_RECENT_FORM` (code default `true` as of 2026-06-26).
**Original shadow date:** 2026-06-25. **Activation date:** 2026-06-26.

---

## Activation summary (2026-06-26)

Replay validation passed all five pre-registered ship criteria on the 90-day historical slate at blend=0.55:

| Metric | A — Production | B — +Recent Form | Δ | Criterion | Pass? |
|---|---:|---:|---:|---|---|
| Recommendation count | 107 | 157 | **+50** | volume ≥ 0.5 × A | ✅ |
| Win rate | 68.2% | **69.4%** | +1.21pp | — | (informational) |
| ROI | +15.99% | **+18.17%** | **+2.18pp** | ROI ≥ +2pp | ✅ |
| 60–65% calibration | (baseline) | **improved** | — | does not worsen | ✅ |
| 65–70 / 70–75 / 75+ regression | — | — | — | \|Δ err\| ≤ 5pp | ✅ |
| CLV | — | — | not materially worse | Δ ≥ −0.01 | ✅ |

**VERDICT: PASS.** Code default flipped `false` → `true`.

---

---

## What shipped (this commit)

### Always-on (no methodology change)
1. **`BettingRecommendation.feature_contributions`** — new `JSONField`, default `{}`, populated on every new MLB recommendation. Captures team rating, pitcher static + form contributions, HFA, market blend delta, raw/calibrated probabilities, and edge. Pre-v3.1 rows degrade gracefully (`{}`). Surfaced in Django admin as a readonly JSON block.
2. **`apps/mlb/services/pitcher_form.py`** — new `recent_form_delta(pitcher, *, reference_date, n=5)` returning a rating-scale delta (50-centered) from the pitcher's W–L over their last N completed starts. Returns `0.0` when there's insufficient data. **Documented limitation:** pure W–L is a noisy proxy because per-start IP/ER/FIP data is not ingested.
3. **Method Replay** accepts a `use_recent_form` parameter on `_simulate_recommendation`; new `?experiment=recent_form` plaintext endpoint runs A (production) vs B (+form) on identical historical slates.

### Behind feature flag (`USE_STARTER_RECENT_FORM=false` by default)
4. **MLB model service** computes a pitcher-form term every time but only **adds** it to the score when the flag is `true`. When `false`, contributions are captured for audit but production scoring is byte-identical to the pre-v3.1 path. Locked by `test_score_excludes_form_when_flag_off`.

---

## Pre-registered ship criteria

The feature flag may be flipped to `true` only after `?experiment=recent_form` returns **VERDICT: PASS**. The verdict is computed mechanically by `render_recent_form_experiment()` and requires ALL of:

| # | Criterion | Mechanical check |
|---|---|---|
| 1 | ROI improves by ≥ +2pp | `b.roi − a.roi ≥ 2.0` |
| 2 | 60–65% calibration does not worsen | `|b_error_60_65| ≤ |a_error_60_65| + 1.0pp` (tolerance) |
| 3 | Recommendation volume usable | `b.count ≥ 0.5 × a.count` |
| 4 | No major bucket regression | For 65–70 / 70–75 / 75+: `|b_error| ≤ |a_error| + 5.0pp` |
| 5 | CLV does not materially worsen | `b.avg_clv − a.avg_clv ≥ −0.01` |

If any criterion fails → **flag stays OFF**. Feature persists as a shadow-only capture for future re-validation.

---

## How to run validation

```
GET /analytics/method-replay/?experiment=recent_form&days=90&blend=0.55
```

Staff-only. Plaintext output includes headline metrics, deltas, per-confidence-bucket calibration table, and the mechanical SHIP CRITERIA verdict.

Operator workflow:
1. Hit the URL on Railway.
2. Read the `VERDICT` line.
3. If `PASS` → set `USE_STARTER_RECENT_FORM=true` in Railway env vars → Railway redeploys → feature active.
4. If `FAIL` → do nothing. The capture continues silently.

---

## Activation

```bash
# Railway → Environment Variables
USE_STARTER_RECENT_FORM=true
```

Redeploy triggers automatically. No code change required.

---

## Rollback

```bash
# Railway → Environment Variables
USE_STARTER_RECENT_FORM=false
# (or delete the variable entirely — default is false)
```

One env var flip. No DB migration. No code change. Historical recommendations and their feature_contributions JSON remain intact and continue to be readable.

---

## Known limitations (document, don't hide)

1. **The W–L proxy is noisy.** A pitcher can throw a 7-inning shutout and lose 1–0; the proxy counts that as a "loss" against pitcher form. Real FIP/xFIP/SIERA would be cleaner. **Upgrade path:** replace `_w_l_form_delta` internals; keep the public signature so callers don't break.
2. **Reference-date anchoring is mandatory in replay.** `recent_form_delta` defaults to `timezone.now()` for production use; the replay path passes `reference_date=game.first_pitch` so historical simulations cannot peek at the game being evaluated. Locked by `test_leakage_guard_reference_date`.
3. **Form contribution is computed even when flag is off.** This is intentional — the value is captured on `feature_contributions` for retrospective attribution. The flag controls whether it *enters the score*, not whether it's computed.
4. **Non-MLB sports have empty `feature_contributions`.** The dataclass field defaults to `{}`; downstream readers must `.get()` with defaults. Wiring other sports follows the same pattern.

---

## What's next (post-validation)

Per Phase 5 v3.1 roadmap:
- If recent form passes → bullpen quality (next Phase 2 feature).
- If recent form fails → per-bucket isotonic calibration (the Phase 1 architectural change).

Either path stays inside the frozen-methodology discipline. Every feature gates on a pre-registered replay experiment with mechanical ship criteria.
