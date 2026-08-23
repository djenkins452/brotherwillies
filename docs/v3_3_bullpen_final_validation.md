# v3.3 Bullpen — Final Validation Record and NO-GO Decision

**Status:** FORMALLY CLOSED — NOT CURRENTLY PRODUCTION-VALUABLE
**Date of decision:** 2026-08-23
**Production impact:** ZERO. `USE_BULLPEN_QUALITY=false`, `USE_BULLPEN_FATIGUE=false` (code defaults). V3.2 remains frozen (0.62 / 7pp / blend 0.55 / Recent Form ON).

---

## Executive summary

Brother Willies invested substantial effort building a complete v3.3 bullpen feature stack — historical reconstruction pipeline, deterministic snapshot builder, daily forward maintenance, shadow contributions, A/B/C replay, attribution + salvage study, and pre-registered walk-forward validation. The feature was validated against evidence discipline at every stage.

**Every stage said no.**

- A/B/C replay showed bullpen NEARLY DOUBLED recommendation volume (238 → 415) while cutting win rate by ~7pp and ROI by ~11pp — the classic "artificial edge" signature.
- Attribution study confirmed bullpen contribution magnitudes were dramatically over-powered (median |Δ|=3.30pp, P90=12.85pp, max >50pp) and identified a single surviving formulation: veto V3.2 recommendations when picked-side bullpen quality diff ≤ -6 rating units. Exploratory improvement was small (+0.31pp win, +0.51pp ROI).
- **Final pre-registered walk-forward validation FAILED** two of six ship criteria — temporal consistency and vetoed-vs-retained-bet performance gap.

Per the evidence discipline: **bullpen has not earned the right to influence any Brother Willies recommendation**. All bullpen production flags remain false. V3.2 methodology remains frozen.

---

## Full validation evidence

### Stage 1: A/B/C replay (180-day, coverage 99.02%, populations 1504/1504/1504)

| Variant | n | W-L | Win | ROI | CLV+ |
|---|---:|---|---:|---:|---:|
| A — V3.2 baseline | 238 | 171-67 | 71.85% | +21.95% | 56.0% |
| B — V3.2 + bullpen quality | 415 | 269-146 | 64.82% | +11.17% | 55.3% |
| C — V3.2 + quality + fatigue | 416 | 271-145 | 65.14% | +11.82% | 55.0% |

**Result:** bullpen NEARLY DOUBLED recommendation volume while cutting win rate ~7pp and ROI ~11pp. Populations comparable; leakage safeguards clean; the degradation was real.

### Stage 2: Attribution + Salvage Study

Contribution magnitude distribution:
- Median |Δ probability| = 3.30pp
- P90 = 12.85pp, P95 = 19.15pp, max > 50pp
- 245 games crossed the 62% probability gate
- 385 games crossed the 7pp edge gate
- 277 recommendation-status changes
- 332 side changes

The bullpen contribution was dramatically over-powered relative to its evidence base. Salvage tests:
- **Bounded weights** (scale 0.10 / 0.25 / 0.50 / 0.75) — no configuration beat baseline with retained volume.
- **Probability caps** (±0.5 / ±1 / ±2 / ±3pp) — same result.
- **Veto architectures** (multiple thresholds and rules) — one surviving formulation: **veto V3.2 recommendation when picked-side bullpen quality diff ≤ -6 rating units** (small exploratory improvement: +0.31pp win, +0.51pp ROI).
- **Isolated feature analysis** (bet the bullpen-favored side across all covered games, bucketed by differential) — no meaningful directional signal.
- **Interaction cohorts** (11 cohorts: weak-starter + strong-pen, home / road picks, low / high baseline edge, etc.) — no narrow cohort where bullpen helps.

### Stage 3: Final walk-forward validation of the veto ≤ -6 rule

Pre-registered threshold. NO parameter search. 6 ship criteria required.

**Aggregate held-out results:**

| Variant | n | W-L | Win | ROI | CLV+ |
|---|---:|---|---:|---:|---:|
| A — V3.2 baseline | 218 | 155-63 | 71.10% | +21.12% | 57.01% |
| B — V3.2 + veto ≤ -6 | 178 | 127-51 | 71.35% | +21.48% | 55.17% |
| V — vetoed bets (removed by rule) | 40 | 28-12 | 70.00% | +19.51% | 65.00% |

**Fold consistency:** helped=3, neutral=1, hurt=4, no_data=2.

**Ship criteria evaluation:**

| # | Criterion | Verdict | Detail |
|---|---|---|---|
| 1 | B win rate ≥ A win rate | ✅ PASS | B=71.35% ≥ A=71.10% |
| 2 | B ROI ≥ A ROI | ✅ PASS | B=+21.48% ≥ A=+21.12% |
| 3 | CLV+ does not worsen materially (Δ ≥ -2pp) | ✅ PASS | Δ=-1.84pp within tolerance |
| 4 | Retained volume ≥ 70% | ✅ PASS | 178/218 = 81.65% retained |
| 5 | helped folds − hurt folds ≥ 1 | ❌ **FAIL** | helped=3, hurt=4 (difference = -1) |
| 6 | Vetoed bets ROI materially worse than retained bets (gap ≥ 2pp) | ❌ **FAIL** | Vetoed bets performed at +19.51% ROI — nearly identical to retained bets (+21.48%). Gap ≈ 2pp but arguably the vetoed bets performed AS WELL as anything else, meaning the veto is not identifying disproportionately bad bets. Additionally, vetoed bets had a HIGHER CLV+ rate (65%) than retained bets (55%), which is the opposite of what a working veto should do. |

**Overall verdict: NO-GO.** Two of six criteria failed. The veto is not consistently identifying bad recommendations — it is largely removing bets that would have performed similarly to (and by CLV+ arguably better than) the ones it lets through.

---

## Why this is the correct decision even given the small aggregate improvements

The aggregate ROI improvement (+0.36pp) and win-rate improvement (+0.25pp) are within the noise band of the sample. Criterion 5 (temporal consistency) is designed to catch exactly this failure mode — an aggregate that looks positive but only because a couple of good folds outweigh a slightly larger set of bad folds. When helped-vs-hurt is 3-4, the aggregate signal is one deep-runner-up fold away from flipping negative. That is not the shape of a robust production signal.

Criterion 6 (vetoed-vs-retained gap) is the theoretical foundation of the veto architecture — a veto only works if it removes disproportionately bad bets. This dataset shows the vetoed bets perform statistically indistinguishably from the retained ones, and outperform them on CLV+. The veto is functionally a random-ish filter, not a risk gate.

Loosening either criterion after the fact would abandon evidence discipline and re-open exactly the door that produced the initial B/C over-aggressive contribution. **Discipline is preserved.**

---

## What is preserved (infrastructure)

All bullpen infrastructure is retained for future research:

- `apps/mlb/models.py::TeamBullpenSnapshot` — append-only per-team snapshot table.
- `apps/mlb/models.py::RelieverAppearance` — append-only per-pitcher game appearances.
- `apps/mlb/services/bullpen.py` — team_bullpen_signal (shadow-only, gated by flags that stay false).
- `apps/mlb/services/bullpen_builder.py` — deterministic historical builder.
- `apps/datahub/management/commands/ingest_reliever_appearances.py` — boxscore ingestion.
- `apps/datahub/management/commands/backfill_bullpen_snapshots.py` — snapshot build.
- `apps/datahub/management/commands/bullpen_daily_refresh.py` — daily forward maintenance.
- `apps/datahub/providers/mlb/statsapi_client.py` — shared MLB Stats API client (used by multiple ingestion paths).
- `apps/analytics/services/bullpen_backfill_service.py` — in-app backfill orchestration.
- `apps/analytics/services/bullpen_replay.py` — A/B/C replay.
- `apps/analytics/services/bullpen_attribution.py` — salvage study.
- `apps/analytics/services/bullpen_veto_walkforward.py` — walk-forward validation harness.
- `apps/analytics/services/bullpen_api_check.py` — connectivity diagnostic.
- `apps/analytics/models.py::BullpenBackfillRun`, `BullpenExperimentRun` — run tracking.
- Historical `TeamBullpenSnapshot` and `RelieverAppearance` data on Railway Postgres.
- All UI (bullpen-backfill, bullpen-experiment, bullpen-integrity, bullpen-api-check status pages).

**Nothing is deleted.** If a future methodology or new data source (e.g., high-leverage inning tracking, sequential decision modeling) provides a defensible new bullpen hypothesis, the infrastructure is ready and the tests still enforce leakage / determinism / shadow-only discipline.

---

## What must NOT happen

- Bullpen must NEVER activate in production without a new, separately-validated hypothesis that clears its own pre-registered ship criteria on an independent time window.
- No lowering of the ship criteria — the criteria are the discipline; loosening them defeats their purpose.
- No re-tuning of the -6 threshold. This closure is FINAL for the current formulation.
- No revisiting bullpen quality / fatigue as a probability-input feature without new predictive evidence beyond the current information set.

The daily bullpen refresh cron may continue running (it costs little and preserves data). The staff-only pages remain accessible for continued research. But **no production recommendation is influenced by bullpen data** and none will be until a new hypothesis passes proper validation.

---

## Cross-reference

- Design doc: `docs/v3_2_bullpen_design.md`
- Full-stack changelog entries: `docs/changelog.md` (2026-08-22 shadow foundation → 2026-08-23 backfill fix → 2026-08-23 attribution study → 2026-08-23 progress_variant fix → 2026-08-23 walk-forward validation)
- Live feature ledger: `docs/v3_feature_inventory.md`

Bullpen closed. Moving to the next feature: **v3.4 — Confirmed Lineups + Lineup Quality.**
