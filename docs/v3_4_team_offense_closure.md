# v3.4 Team Offense — Formal Closure (NO-GO)

**Status**: CLOSED — NO_GO_OFFENSE. `USE_TEAM_OFFENSE` remains `false` in code and in Railway env.

**Date closed**: 2026-08-24.

**Preserved**: TeamBattingSnapshot history, backfill / reconciliation tooling, isolated-analysis tooling, research diagnostics. Zero production influence.

---

## Evidence trail

### 1. Phase-1 hypothesis + failure (30-day runs/game)

- **Hypothesis**: an explicit offensive-strength signal, computed leakage-safely from historical runs scored, contains predictive information beyond Elo (which captures aggregate W-L, not offensive volume vs win outcome).
- **Signal**: `apps/mlb/services/team_offense.py::team_offense_signal(team, reference_date)` — rolling 30-day runs-per-game, capped at ±10 rating units, contributed with weight 0.5 into `_score()`.
- **Replay result** (Railway, 180d window):
  - A — V3.2 baseline: n=236, 170-66, **72.03% win, +22.23% ROI**, CLV+ 55.6%
  - B — V3.2 + 30d runs/game × 0.50: n=247, 175-72, **70.85% win, +20.13% ROI**, CLV+ 56.1%
  - Deltas: win **−1.18pp**, ROI **−2.10pp**, CLV+ +0.5pp
- **Verdict**: NO-GO. Preserved as `candidate_a_runs_per_game` in the isolated analyzer for continuity. Not re-tuned.

### 2. Historical data foundation

- New `TeamBattingSnapshot(team, as_of_date)` model — season-to-date raw hitting counts (PA, AB, H, 2B, 3B, HR, BB, HBP, SF, K, R, games) + MLB's derived OBP/SLG/OPS.
- Data source: MLB Stats API `/v1/teams/{id}/stats?stats=byDateRange&group=hitting` — one API call per (team, as_of_date), ~1KB response. Rolling-30d windows emerge by subtraction from two snapshots (no separate window fetch).
- Ingestion: `TeamBattingBackfillRun` async orchestration + `ingest_team_batting` CLI, `run_team_batting_backfill` background thread using the canonical statsapi_client.
- Backfill result: **4,496 snapshots**, 2026-03-01 → 2026-08-22, 46 min elapsed, `completed_with_errors` (later reconciled).

### 3. Audit — 99.9% game coverage

- `apps/analytics/services/team_batting_audit.py` — read-only diagnostic reporting expected pairs vs present pairs, legitimate-empty vs suspect-missing classification (pre-season vs actual failures), game-level both-team coverage, per-candidate sample coverage, mechanical READY / HOLD verdict.
- Post-reconciliation: **1,652 evaluable games / 1,651 both-team covered (99.94%)**; candidate B/C/D sampled coverage 99.8%. Audit verdict **READY**.

### 4. Phase-2 OPS/OBP/SLG/blend isolated analysis

- `apps/analytics/services/team_offense_isolated_analysis.py` — READ-ONLY analyzer with pre-registered PROMOTE / MONITOR / REJECT rules chosen BEFORE seeing results.
- Per-candidate rules:
  - PROMOTE requires: coverage ≥60% AND bucket monotonicity ≥3pp AND past-market lift >3pp in ≥2/4 market-probability quartiles AND every correlation `|r|<0.6` (vs Elo / market / pitcher / recent-form).
  - MONITOR: fails ONE non-coverage rule.
  - REJECT: fails coverage or ≥2 rules.
- Sample: 1,652 games, 180-day window.
- Results:
  - **B — rolling 30-day OPS**: MONITOR. Bucket monotonicity passed. Redundancy passed. Past-market test **FAILED** — only 1/4 quartiles > 3pp lift; mean past-market lift **−0.78pp**.
  - **C — rolling OBP**: MONITOR. Bucket monotonicity passed. Redundancy passed. Past-market test **FAILED** — only 1/4 quartiles > 3pp lift; mean past-market lift **+2.40pp**.
  - **C — rolling SLG**: MONITOR. Bucket monotonicity passed. Redundancy passed. Past-market test **FAILED** — only 1/4 quartiles > 3pp lift; mean past-market lift **−0.69pp**.
  - **D — season + recent OPS blend**: MONITOR. Bucket monotonicity passed. Redundancy passed. Past-market test **FAILED** — only 1/4 quartiles > 3pp lift; mean past-market lift **+1.64pp**.
  - **A — 30-day runs/game (reference)**: REJECT. Previously degraded V3.2 in direct replay; isolated analysis also failed promotion criteria.

### 5. Conclusion — NO_GO_OFFENSE

Zero candidates earned PROMOTE. Selected candidate: **none**. Overall verdict: **NO_GO_OFFENSE**.

The observed Q1-market-only interaction on candidate C (OBP) and D (blend) is documented as a future research hypothesis but MUST NOT be optimized or promoted from this dataset — searching a post-hoc Q1-only rule would recapitulate the "artificial-edge pathology" pattern that killed bullpen.

### 6. Bounded integration replay — NOT RUN

Because no candidate earned promotion, the bounded integration replay was explicitly **NOT** executed. Discipline preserved — activation requires: isolated PROMOTE → bounded replay clean → walk-forward validation clean, in that order.

---

## Preserved tooling (zero production influence)

| Path | Purpose |
|---|---|
| `apps/mlb/models.py::TeamBattingSnapshot` | Historical hitting snapshots |
| `apps/analytics/models.py::TeamBattingBackfillRun` | Async backfill orchestration row |
| `apps/analytics/services/team_batting_backfill_service.py` | Background-thread backfill |
| `apps/datahub/management/commands/ingest_team_batting.py` | CLI wrapper |
| `apps/mlb/services/team_offense_v2.py` | Leakage-safe OPS/OBP/SLG consumer |
| `apps/analytics/services/team_offense_isolated_analysis.py` | Isolated predictive-value analyzer |
| `apps/analytics/services/offense_v2_replay.py` | Bounded integration replay (NEVER RUN post-NO-GO) |
| `apps/analytics/services/team_batting_audit.py` | Backfill audit |
| `apps/analytics/services/offense_replay.py` | Phase-1 direct replay (retained for reference) |
| `apps/mlb/services/team_offense.py` | Phase-1 runs/game signal (retained for reference) |

Any future research on offensive quality MUST introduce a fresh, pre-registered hypothesis — this dataset has been exhausted for the current mission.

---

## Production flag state (unchanged)

```
USE_V3_2_SELECTION      = true    (Railway)
USE_BULLPEN_QUALITY     = false
USE_BULLPEN_FATIGUE     = false
USE_LINEUP_QUALITY      = false
USE_TEAM_OFFENSE        = false
```
