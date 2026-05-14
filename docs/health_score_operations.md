# Recommendation Health Score — Operations Guide

**Promulgated:** 2026-05-14
**Audience:** operators consuming the Health Score for governance decisions.

---

## What the Health Score is

A composite 0–100 score across seven dimensions that compresses Brother Willies's current operating state into a single signal. The full design is in `docs/recommendation_quality_framework.md` §3. This document is the operator manual.

**What it answers:** *Is the engine behaving like a disciplined predictive system?*

**What it does NOT answer:** *Did we win yesterday? Should I bet today? What's my ROI this week?*

---

## What the Health Score is NOT

- ❌ Not a betting predictor.
- ❌ Not a profit-and-loss tracker.
- ❌ Not an auto-tuner. The score never modifies recommendation behavior, calibration constants, or any other tuning surface. Per `docs/architecture_laws.md` Law 4, *operators* propose tunes; *evidence* justifies them; the *score* is one input to that justification.
- ❌ Not a daily / weekly performance scorecard. A bad slate or losing week may not move the score at all because the score does not include win rate or short-window ROI.

The score is **the operational discipline layer**, not the engine.

---

## How to read the score

### The four bands

| Score | Band | Authorized action |
|---|---|---|
| ≥ 75 | **STRONG** | Do not touch. No constant changes regardless of individual segment fluctuation. |
| 50–74 | **HEALTHY** | Monitor. Sub-segment changes allowed only with full Tuning Governance evidence. No global retunes. |
| 25–49 | **WATCH** | Investigate. Identify the binding dimension. Plan a targeted change. Do not ship from this band without the §2 evidence block. |
| < 25 | **INTERVENE** | Act, narrowly. Targeted action on the binding dimension is justified. Do not retune everything. |

### The seven dimensions

| Dimension | Weight | What it tells you |
|---|---|---|
| **CLV Trend** | 25% | Are our picks moving with the market? Primary leading indicator. |
| **Calibration Accuracy** | 20% | Do our predicted probabilities match observed win rates? |
| **Edge Realism** | 15% | Are 8+ edge picks outperforming 4-6 edge picks? Detects the "fake giant edge" pathology. |
| **Recommendation Stability** | 10% | Is volume varying chaotically week over week? |
| **Market Alignment** | 10% | How far does the model drift from de-vigged market consensus? |
| **Stale-Odds Rate** | 10% | Provider-health signal. % of settled bets with no closing snapshot. |
| **Volume vs Target** | 10% | Is current week inside the historical envelope? |

Per-dimension scores carry their own status (strong / healthy / watch / intervene / no_data). The composite can be in `HEALTHY` while one dimension is in `INTERVENE` — that's exactly when the warnings panel on the Health Score page becomes important.

### Warnings

The page surfaces specific warnings when individual dimensions cross danger thresholds, regardless of the composite. Each warning carries a severity, a dimension, and a message describing what to investigate. Warnings require minimum samples to fire — they don't trigger from short-window noise.

---

## How operators should use the score

### Daily / weekly governance check

1. Open `/analytics/health-score/`.
2. Look at the headline score + band.
3. If **STRONG**: confirm no warnings present; close the tab. The system is operating within its design envelope. Don't tune.
4. If **HEALTHY**: scan the dimension breakdown for any `watch` or `intervene` status on individual dimensions. If a dimension is in `watch` or worse, read its warning (if present) and the supporting `value` field. Decide whether the situation warrants the full Tuning Governance investigation (§2 of the framework doc).
5. If **WATCH**: identify the binding dimension(s). Begin an investigation document outside the code (proposal + evidence). Do not ship code changes from this band without the structured Evidence block per Law 4.
6. If **INTERVENE**: address the specific binding dimension. Targeted action only — global retunes are forbidden by Law 4.

### Before any tuning commit

The Health Score is the gate referenced in Law 4. Before opening a PR that modifies a tuning constant:

1. Capture the **current** Health Score: `python manage.py capture_health_snapshot --notes "pre-tune <change description>"`.
2. Include the score + band in the PR's Evidence block:
   ```
   Health Score before: 67 (HEALTHY)
   Health Score after (predicted): 74 (HEALTHY, mid)
   ```
3. After the change ships, capture the post-change snapshot: `python manage.py capture_health_snapshot --notes "post-tune <change description>"`.
4. If the post-change score is materially worse, the rollback trigger from the PR's Evidence block applies.

### Before / after major model changes

Major changes (Elo activation, signal additions, calibration retunes) require **baseline snapshots**:

1. **Before:** `python manage.py capture_health_snapshot --notes "pre-elo baseline"`. (Or whatever the change is.)
2. After the change ships, run the cron a few times so post-change MockBets accumulate.
3. **After (Day 1):** `python manage.py capture_health_snapshot --notes "post-elo, day 1"`.
4. **After (Week 1):** `python manage.py capture_health_snapshot --notes "post-elo, week 1"`.
5. **After (Week 4):** `python manage.py capture_health_snapshot --notes "post-elo, week 4 stabilization"`.

The history table on `/analytics/health-score/` will then show the trajectory. Per Law 4, the change is rolled back if the score has materially declined and the supporting evidence has disappeared.

### When you should NOT touch anything

These situations look "wrong" but are normal:

| Situation | Why it's not actionable |
|---|---|
| Score = STRONG but yesterday lost 4 of 6 bets | Variance, not signal. Law 4 anti-pattern #1. |
| Score = STRONG but one short_fav bet lost a coin-flip | Single-bet anecdote. Sample = 1; no information. |
| Score = HEALTHY but the volume-vs-target dimension is `watch` for one week | Single-week variance. Wait for at least 2 consecutive weeks in `watch` before investigating. |
| Score = HEALTHY but a friend on Twitter says BW is "obviously broken" | Not evidence. Law 4 forbids emotional tuning. |
| Score moved from 78 to 73 | Within band; within typical variation. Don't touch. |

When the situation makes you anxious but the score is healthy, the framework's answer is **wait**.

---

## Running the management command

### Daily cron capture

Add to the Railway cron schedule (or the `refresh_data` cycle):

```bash
python manage.py capture_health_snapshot
```

Cost: ~100ms. Safe to run on any cadence.

### Pre-change baseline

```bash
python manage.py capture_health_snapshot --notes "pre-elo baseline"
```

Tagged snapshots are easy to find in the history table on the analytics page.

### Custom window

```bash
python manage.py capture_health_snapshot --window 30 --notes "30d window check"
```

Default window is 14 days. Larger windows produce smoother numbers (less variance, slower to react). Smaller windows produce noisier numbers (more variance, faster to react). Don't change the default mid-stream without documenting the reason; the snapshot historical comparison only works when windows match.

### Dry run (compute without persisting)

```bash
python manage.py capture_health_snapshot --dry-run
```

Outputs the current scores to stdout. Does not write a snapshot row. Useful for "is the score reasonable today before I commit a snapshot?"

---

## Reading the snapshot history

Each `RecommendationHealthSnapshot` row captures:
- `captured_at` — timestamp.
- `overall_score` + `band`.
- `dimension_scores` — per-dimension JSON with score, value, sample, status.
- `supporting_data` — raw aggregations (per-bucket counts, CLV+ rate, mean disagreement, etc.). Lets you re-derive the score without re-running the service.
- `rating_mode_active` — `static` or `elo` at capture time.
- `calibration_state` — snapshot of every tuning constant at capture time. Future calibration changes can be reasoned about against historical snapshots that captured the prior state.
- `notes` — operator-supplied tag.

The history table renders the most recent 20 snapshots. For programmatic access:

```python
from apps.analytics.models import RecommendationHealthSnapshot
# All snapshots since a date
snaps = RecommendationHealthSnapshot.objects.filter(
    captured_at__gte=some_date,
).order_by('captured_at')
# Compare static vs elo bands
static_avg = (
    RecommendationHealthSnapshot.objects
    .filter(rating_mode_active='static', overall_score__gt=0)
    .aggregate(avg=models.Avg('overall_score'))
)
```

---

## FAQ

**Q: The score is 80 but we lost money this week. What do I do?**
A: Nothing. The score doesn't include win rate or short-window ROI. CLV is the leading indicator; settled outcomes lag. If the score is STRONG, the system is operating within its design envelope. Variance is variance.

**Q: The score is 30 (WATCH). Should I tighten MIN_EDGE?**
A: Don't reflex-tune. First identify the binding dimension. If it's CLV Trend with sample ≥ 30, that's an investigation about model-market alignment — not necessarily a gate change. If it's Edge Realism with sample ≥ 20 each side, that's an edge-compression candidate, NOT a MIN_EDGE change. The Tuning Governance rules (§2 of the framework) specify which dimension justifies which intervention.

**Q: Two dimensions are in `intervene` but the composite is HEALTHY. Why?**
A: Composite weights matter. Two `intervene` dimensions at 10% weight each (-20 weighted-points-vs-100) can be masked by five healthy or strong dimensions in the heavier-weighted slots. The warnings panel exists exactly for this — it surfaces specific dimension problems regardless of composite band.

**Q: Should the Health Score include win rate?**
A: No. By design. Win rate is misleading on its own — 50% at -110 is breakeven, 50% at +130 is wildly profitable. Including it in the score would let win-rate variance pull the composite around without any underlying signal change. The framework explicitly excludes win rate from the score (§1 of the framework).

**Q: Can I change the dimension weights?**
A: Not without amending `docs/recommendation_quality_framework.md` §3.1 in the same commit. The weights are an architecture constant, not a tune. An amendment requires the process at the end of `docs/architecture_laws.md`.

**Q: Why was my snapshot's score 0?**
A: When `compute_health_score` returns `overall_score=None` (insufficient data across all dimensions), the snapshot persists with `overall_score=0.0` and `band=''`. The zero is a placeholder, not a real score; the empty band indicates the no-data state. Look at `dimension_scores` for each dimension's `status` to see what was missing.

**Q: How often should I capture snapshots?**
A: At least daily (via cron). Plus tagged captures before and after every meaningful change (Elo activation, signal additions, calibration retunes). The snapshot ledger is the governance audit trail — denser is better.

---

## Compliance checklist for every tuning commit

Before opening a PR that changes a tuning constant, the author confirms:

- [ ] Captured a pre-change snapshot (`capture_health_snapshot --notes "pre-<change>"`).
- [ ] The current band is in `WATCH` or `INTERVENE`, OR the change is a Tier-1 sub-segment gate with full framework §2 evidence.
- [ ] The PR message includes the structured Evidence block from `docs/architecture_laws.md` Law 4.
- [ ] The change is isolated — no other tuning is in flight in the same causal area.
- [ ] A rollback trigger is specified in the Evidence block.

PRs that fail any item violate Law 4. Reviewers should request the evidence or close the PR.

---

*First written 2026-05-14. Health Score is the operational discipline layer for Brother Willies.*
