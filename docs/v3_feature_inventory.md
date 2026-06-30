# Brother Willie v3 — Feature Inventory (Live Status)

**Last updated:** 2026-06-26

This is the running ledger of every predictive signal in the engine, its activation status, and where it falls in the v3 roadmap. The single source of truth for "what is BW actually using right now."

---

## Predictive features (enter the score)

| # | Feature | Status | Flag | Default | Weight in score | Notes |
|---|---|---|---|---|---|---|
| 1 | Team rating differential | 🟢 **Production active** | `USE_DYNAMIC_RATINGS` (Elo source) | `true` | × 0.35 | Elo when active; static rating fallback. |
| 2 | Starter rating differential (static) | 🟢 **Production active** | — | — | × 0.65 | Dominant predictive input. |
| 3 | Home-field advantage | 🟢 **Production active** | — | — | `+2.5` constant | Suppressed on neutral_site. |
| 4 | **Starter recent form** (W-L proxy) | 🟢 **Production active — 2026-06-26** | `USE_STARTER_RECENT_FORM` | **`true`** | × 0.65 (paired with starter) | v3.1 first new predictive feature. Passed replay validation; ROI +2.18pp, win +1.21pp, 60–65% bucket calibration improved. |

## Calibration layer

| # | Feature | Status | Flag | Default | Notes |
|---|---|---|---|---|---|
| 5 | Market blend (0.55) | 🟢 Production active | — | — | Empirically validated across 3 windows. |
| 6 | Soft probability clamp `[0.52, 0.85]` | 🟢 Production active | — | — | Caps extremes. |
| 7 | Sigmoid divisor (25) | 🟢 Production active | — | — | Softens score → probability mapping. |

## Decision gates (filter, not predictive)

| # | Gate | Status | Threshold | Notes |
|---|---|---|---|---|
| 8 | `HARD_MIN_PROBABILITY` | 🟢 Active | 0.50 | Hard floor. |
| 9 | `MIN_PROBABILITY_FOR_RECOMMENDED` | 🟢 Active | 0.60 | **Calibration concern flagged in Phase 3.** Audit in v3.x. |
| 10 | `MIN_EDGE` | 🟢 Active | 6.0pp | |
| 11 | `MAX_ABS_ODDS_FOR_RECOMMENDED` | 🟢 Active | 300 | |
| 12 | Heavy-fav juice gate | 🟢 Active | `≤ -150` requires `STRONG_EDGE` | |
| 13 | Source trust gate | 🟢 Active | ESPN-fallback rejected | |

## Lane risk flags

| # | Flag | Status | Notes |
|---|---|---|---|
| 14 | `market_conflict` | 🟢 Active | Sharp money against pick → HOLD in Game Timing. |
| 15 | `sanity_mismatch` | 🟢 Active | Model/market extreme disagreement. |
| 16 | `thin_edge` | 🟢 Active | `prob − raw_implied < 0.04`. |
| 17 | `short_fav_thin` | 🟢 Active | Short fav with thin edge. |
| 18 | `insight_conflict` | 🔴 **Disabled de-facto** | Docs say "not reliably parsed." Phase 3 recommended removal. |

## Audit / capture infrastructure

| # | Feature | Status | Notes |
|---|---|---|---|
| 19 | `BettingRecommendation.feature_contributions` | 🟢 **Active 2026-06-25** | v3.1 audit artifact. Stores team rating / pitcher static / pitcher form / HFA / market blend contributions per recommendation. |
| 20 | Shadow-capture for non-active features | 🟢 Active | Even when a flag is OFF, contributions are computed and stored — so future replay can attribute. |

## Phantom features (in code, not in score)

| # | Phantom | Status | Recommendation |
|---|---|---|---|
| 21 | `HOUSE_WEIGHTS['injury']` | 🔴 Unwired | Phase 3 recommended removal as dead-code cleanup. |

## Phase 2 — designed, NOT yet implemented

| # | Feature | Status | Design doc | Flag (planned) | Default (planned) |
|---|---|---|---|---|---|
| 22 | **Bullpen quality** | 📐 **Design complete — 2026-06-26** | `docs/v3_2_bullpen_design.md` | `USE_BULLPEN_QUALITY` | `false` (shadow first) |
| 23 | Bullpen fatigue | 📐 Folded into bullpen design (v3.2-B half) | Same doc | Same flag | Same default |

## Phase 3+ — identified, not designed

| # | Feature | Phase | Status | Notes |
|---|---|---|---|---|
| 24 | Confirmed lineup + lineup quality | Phase 3 | 🔍 Identified | Phase 5 strategic — highest CLV alpha if delivered through Game Timing window. |
| 25 | Per-bucket isotonic calibration | Phase 3 | 🔍 Identified | Architectural — pairs with feature additions. |
| 26 | Park factor + weather | Phase 4 | 🔍 Identified | Larger impact on totals than moneyline. |
| 27 | Sharp money / steam | Phase 4 | 🔍 Identified | Requires sharp-book ingestion (new provider). |
| 28 | Team wOBA / wRC+ | Phase 4 | 🔍 Identified | Replaces crude `team.rating`. |
| 29 | Pitcher velocity trend | Phase 4 | 🔍 Identified | Requires Statcast-level ingestion. |

---

## Rollback contract

Every flagged feature has the same rollback discipline:

```
<FLAG_NAME>=false   # Railway env var; one line; no migration
```

Captured `feature_contributions` are preserved across rollbacks (audit value).

---

## Activation discipline

A predictive feature is only ever activated when ALL of:

1. Its data path has shipped + run for ≥ 7 days (when new data is required).
2. A pre-registered replay experiment has been built behind a shadow flag.
3. The replay's mechanical SHIP CRITERIA return `VERDICT: PASS`.
4. A rollback path is documented + tested.

This is the contract that produced Recent Form's clean activation. It is the contract Phase 2 (Bullpen) will follow.

**One validated feature at a time.**
