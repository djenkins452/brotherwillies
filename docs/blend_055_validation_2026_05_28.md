# Blend 0.55 — Controlled Production Validation

**Status:** ACTIVE. Single-variable change. Methodology frozen.
**Owner decision:** Move MLB moneyline blend 0.40 → 0.55.

---

## 1. Exact production change

| Item | Value |
|---|---|
| File | `apps/core/services/probability_calibration.py` |
| Constant | `MARKET_BLEND_WEIGHT` (and `MARKET_BLEND_WEIGHT_CAP`) |
| From → To | `0.40` → `0.55` |
| Commit | `411f78d` — "Roadmap B Step 1: MARKET_BLEND_WEIGHT 0.40 → 0.55" |
| Deployed | **2026-05-26** (Railway auto-deploy on push to `main`) |

**IMPORTANT — the change is already live.** `MARKET_BLEND_WEIGHT = 0.55` has been
in production since 2026-05-26. The live recommendation path reads it via
`finalize_win_prob → blend_with_market` (`probability_calibration.py:140`).
There is **no env-var or SiteConfig override** — the constant is the single
source of truth. No second edit was required or made; making one would be a
no-op. The observation window therefore **started 2026-05-26**, not today.

This was verified before acting (read of the constant + git blame + grep for
overrides) rather than blindly re-applying a change that was already present.

---

## 2. Verification completed

- `python manage.py check` — clean (0 issues).
- **690 tests** across `apps.mockbets.tests + apps.core.tests + apps.mlb.tests
  + apps.analytics.test_method_replay` — all pass.
- Recommendation generation: `apps.core.tests` + `apps.mlb.tests` (which
  exercise `get_recommendation` / `finalize_win_prob` / `compute_status`) pass
  under 0.55; calibration-history tests assert the 0.55 value and its
  weighted-average math.
- MLB hub + bulk placement + snapshot completeness: covered by
  `apps.mlb.tests` and `apps.mockbets.tests` (hub render, `is_bulk_moneyline_eligible`,
  snapshot copy) — pass.
- Method Replay: `apps.analytics.test_method_replay` (41 tests incl. the
  production-shape hardening) — pass.

---

## 3. Rollback (one-line, reversible, no migration)

Edit `apps/core/services/probability_calibration.py`:

```python
MARKET_BLEND_WEIGHT = 0.40       # revert from 0.55
MARKET_BLEND_WEIGHT_CAP = 0.40   # revert from 0.55
```

Then commit + push to `main` (Railway redeploys). No DB migration, no data
backfill — the constant only affects probabilities computed *after* deploy;
historical `BettingRecommendation` / `MockBet` snapshots are immutable.

**Rollback verification steps:**
1. Confirm both constants read `0.40`.
2. `python manage.py check`.
3. Hit `/mockbets/audit/three-populations/?detail=scorecard` after the next
   slate to confirm recommendation volume returns toward the 0.40 baseline.
4. Capture a post-rollback scorecard snapshot for the record.

---

## 4. Weekly scorecard

**URL (staff-only, plaintext):**
```
/mockbets/audit/three-populations/?detail=scorecard
```
- Default window: **last 7 days**. Override with `?days=N` (1–90) or
  `?since=YYYY-MM-DD`.
- Scope is **SYSTEM-APPROVED ONLY** and not user-selectable:
  `status='recommended'` AND `lane='core'` AND system/linked AND post-rules
  date (2026-05-06) AND complete snapshot.

**Metrics emitted:** total bets, recommendation count, W-L-P, win%, ROI,
net P/L, settled stake; CLV beat/matched/lost mix, beat-market %, avg CLV
(primary-source `odds_api` only); favorite vs underdog (favorite = priced
< +100); odds buckets — heavy fav (≤ -200) / mid fav (-150..-199) / short
fav (-149..+99) / underdog (≥ +100).

**CLV caveat (carried from the integrity audit):** production CLV is the
trustworthy measure (real placement vs real closing), but is still subject
to snapshot-cadence and "matched=0 counted as not-beat" effects. Read CLV+
as directional until sample ≥ 30.

---

## 5. 30-day observation plan

**Window:** 2026-05-26 (deploy) → ~2026-06-25. We are already ~Day 2.

**FREEZE.** Methodology is frozen for the full window. NO changes to:
blend (beyond this one), thresholds, confidence, recommendation rules, gates,
calibration, lane logic, EV assumptions, Elo. No favorites-only rule. No
optimization stacking. One clean measurement period, one moving part.

**Cadence:** pull the weekly scorecard once per week (Days 7 / 14 / 21 / 30).
Record total bets, W-L-P, ROI, win%, CLV+ %, and the odds-bucket table each
week. Do not act on Week-1 numbers — sample will be ~10–20 settled bets.

**Break-glass — intervene ONLY on:**
- **(A) Catastrophic performance failure** — e.g. ROI sustained well below
  the 0.40 baseline with an adequate sample, or a clear structural collapse.
- **(B) Trust / data bug** — a measurement or settlement defect (the kind we
  already caught: P/L unit error, edge ×100, CLV capture). Fix the *bug*, not
  the methodology.

**Pre-registered numeric rollback triggers (from the calibration evidence
block, Law 4):**
- CLV+ rate (production, primary-source) **below 33% sustained for 7 days**, OR
- Recommendation Health Score composite **drops > 5 points** from the
  pre-change baseline.
Either → revert to 0.40 in one commit and capture a post-rollback snapshot.

Anything short of (A)/(B) or a pre-registered trigger: **do nothing and let
it run.** The whole point is a stationary target.

---

## 6. Recommendation count expectations under 0.55

From the lane-corrected counterfactual replay (production-equivalent set):

| Window | 0.40 | 0.55 | Δ |
|---|---|---|---|
| 30-day | 103 recs | 65 recs | −38 (~37% fewer) |
| 60-day | 105 recs | 66 recs | −39 (~37% fewer) |

**Expect roughly:**
- ~**2 system-approved recommendations per day** (vs ~3.4 under 0.40),
- ~**10–16 per week**, ~**60–70 per month**,
- a **~35–40% reduction** in recommendation volume vs the 0.40 regime,
- skew toward **short favorites** (-149..+99), with **underdogs near zero**
  (the blend suppresses dog picks structurally — confirmed by the favorites
  experiment showing 0.55 produces ~0 underdog recommendations).

Volume is slate-dependent (≈15 MLB games/day); thin or odds-less slates
produce fewer. A week materially below ~6–8 recs is more likely a
data/ingestion issue than a model change — check `LIVE_MLB` + snapshot
freshness before drawing conclusions.

---

*Goal: collect clean production evidence on whether 0.55 improves REAL
performance, not replay performance. Frozen methodology, weekly scorecard,
pre-registered triggers. Stop theorizing; let it run.*
