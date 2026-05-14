# Evaluation Integrity Audit — 2026-05-14

**Trigger:** operator observed `My Bets` showed **6 placed MLB bets**; Moneyline Evaluation showed only **2** for the same date. This violates the evaluation-truth contract: the analytics layer must not silently exclude actual placed bets.

**Verdict:** root cause identified, fix landed, contract restored, tests in place.

---

## 1. Query path trace

### 1.1 `My Bets` page — `apps/mockbets/views.py::my_bets`

| Filter | Applied |
|---|---|
| Base model | `MockBet` |
| User scope | `user=request.user` |
| `bet_type` | conditional `='moneyline'` when MONEYLINE_ONLY_MODE; else optional `?bet_type=` |
| `placed_at` (date) | **NONE — shows entire history** |
| `is_system_generated` | **NEVER filtered** |
| `result` | optional `?result=pending/win/loss/push` |
| `sport` | optional `?sport=mlb/cfb/cbb/golf/college_baseball` |
| `confidence_level` | optional `?confidence=low/medium/high` |
| `model_source` | optional `?model_source=house/user` |

`My Bets` is the user's authoritative betting ledger. Default view is "every bet I've ever placed."

### 1.2 `Moneyline Evaluation` page — `apps/mockbets/views.py::moneyline_evaluation_view` → `services/moneyline_evaluation.py::_filter_bets`

| Filter | Applied (pre-fix) |
|---|---|
| Base model | `MockBet` |
| User scope | **ALL users** (engine-performance evaluation, not user behavior) |
| `bet_type` | **always `='moneyline'`** |
| `placed_at` (date) | **`placed_at__date` between `[date_from, date_to]`** (defaults to yesterday) |
| `is_system_generated` | **`=True` UNLESS `?include_manual=1`** ← silent exclusion |
| `result` | NEVER filtered (intentional — wants pending too for "did the cron settle?") |
| `sport` | NEVER filtered |

The single silent-exclusion mechanism: `_filter_bets` defaults `include_manual=False`, which adds `is_system_generated=True` to the queryset.

### 1.3 `Shadow Review` page — `apps/analytics/views.py::shadow_review`

Different data source — reads `BettingRecommendation` rows (engine output), not `MockBet` rows (user/system placements). Not the source of the discrepancy. Already documents `sample_total` vs `sample` (the `elo_available=False` exclusion is shown on-page).

---

## 2. Why "6 vs 2"

For any date `D`:

```
My Bets count(user=U, sport=mlb, placed_at__date=D)
  =  N_system_bets  +  N_manual_bets

Eval count(date_from=D, date_to=D)         # pre-fix default
  =  N_system_bets          # manual silently dropped
```

For the operator's observed case: `N_system_bets = 2`, `N_manual_bets = 4`. The 4 manual placements were silently invisible to evaluation.

The silent-exclusion failure mode is the bug. Even if the operator wanted system-only evaluation, the page should *say so* in clear terms — "Scope: Recommended System Bets, 4 manual bets excluded." That's the transparency contract.

---

## 3. Fix — surgical, behavior-preserving for old URLs

The fix lives entirely in `apps/mockbets/services/moneyline_evaluation.py` plus thin view/template plumbing. Score formula, recommendation logic, calibration constants, Elo shadow framework — none touched. This was an evaluation-truth-source repair, not a model change.

### 3.1 Canonical scopes

Four explicit values; one default:

```python
SCOPE_ACTUAL = 'actual'              # all placed moneyline bets in window
SCOPE_RECOMMENDED = 'recommended'    # is_system_generated=True only
SCOPE_MANUAL = 'manual'              # is_system_generated=False only
SCOPE_ALL = 'all'                    # alias for SCOPE_ACTUAL (clarity)

DEFAULT_SCOPE = SCOPE_ACTUAL
```

### 3.2 Service API — extended, not replaced

`build_evaluation_report(bets_qs, date_from, date_to, include_manual=None, scope=None)`:

- New `scope` kwarg — explicit, four-valued, deterministic.
- Legacy `include_manual` kwarg retained for back-compat. Mapping:
  - `include_manual=True` → `scope='all'` (matches historical behavior)
  - `include_manual=False` → `scope='recommended'` (matches historical default — preserved for callers that explicitly opt in)
  - Both `None` (new default) → `scope='actual'`

The four existing callers (Command Center home, eval view, two test classes) all keep working with no changes required.

### 3.3 Scope summary in the report payload

Every report now carries:

```python
report['scope'] = {
    'scope': 'actual',
    'scope_label': 'Actual Bets',
    'total_placed_in_window': 6,
    'included': 6,
    'excluded': 0,
    'exclusion_reasons': {},          # e.g., {'manual_bets': 4}
    'has_exclusions': False,
}
```

Templates render this above the Executive Summary. Markdown copy-packet includes it in the "Date Range" section. **No surface that consumes the report can be ignorant of which bets it's analyzing.**

### 3.4 UI changes

- Old `include manual` checkbox → replaced by a four-option scope dropdown (`Actual Bets (default)` / `Recommended System Bets` / `Manual Bets` / `All Bets`).
- New scope-summary box at the top of the page surfaces:
  - The chosen scope label.
  - `X placed moneyline bet(s) in window · Y included · Z excluded` (counts).
  - Per-reason exclusion breakdown (`4 manual bets`, etc.).
  - A guidance note when the user is on `Recommended` scope and bets were excluded: "Switch to Actual Bets scope to see your full ledger."

### 3.5 URL compatibility

- New canonical URL: `?scope=actual` (or `recommended` / `manual` / `all`).
- Legacy URL: `?include_manual=1` → still works (maps to `scope='all'`).
- Legacy URL: `?include_manual=0` (or any non-`1` value with the key present) → maps to `scope='recommended'`.
- No URL: defaults to `scope='actual'`.

---

## 4. Tests — 11 new locks in `apps.mockbets.tests.EvaluationScopeIntegrityTests`

| Test | What it locks |
|---|---|
| `test_actual_scope_includes_all_placed_bets` | The discrepancy fix — `actual` includes system + manual. |
| `test_recommended_scope_excludes_manual_bets_with_count` | `recommended` scope surfaces the excluded count + reason. **Silent exclusion is now impossible.** |
| `test_manual_scope_excludes_system_bets_with_count` | Mirror — `manual` scope reports system exclusions. |
| `test_all_scope_is_alias_for_actual` | The `all` alias produces identical counts to `actual`. |
| `test_default_scope_is_actual` | No-args call defaults to `actual`. |
| `test_legacy_include_manual_false_maps_to_recommended` | Back-compat. |
| `test_legacy_include_manual_true_maps_to_all` | Back-compat. |
| `test_explicit_scope_wins_over_include_manual` | Resolution precedence. |
| `test_my_bets_count_matches_actual_eval_for_same_window` | **The alignment contract** — for any date range, `My Bets` count = Actual-scope eval `included` count. |
| `test_view_renders_scope_summary_with_counts` | UI transparency — scope label + counts + exclusions all render. |
| `test_view_default_is_actual_scope` | Visiting the page with no `?scope=` defaults to `actual`. |

Existing tests (`MoneylineEvaluationFiltersTests`, `MoneylineEvaluationSummaryMathTests`) all still pass — back-compat verified.

**Test totals:** 270 across mockbets + relevant Phase modules. Zero regressions.

---

## 5. What was NOT changed

Per direction ("evaluation truth-source repair only"):

- ❌ No changes to recommendation logic.
- ❌ No Elo activation. `USE_DYNAMIC_RATINGS` still `False`.
- ❌ No calibration retunes (sigmoid divisor, clamp, blend weight).
- ❌ No new predictive signals.
- ❌ No changes to model probabilities, score formula, edge math.
- ❌ No changes to shadow-review conclusions (Phase 2A Task 3 still stands; the empirical placeholder section there gets filled in from production data unchanged).

The fix touches three files:
1. `apps/mockbets/services/moneyline_evaluation.py` (canonical scopes, scope summary, back-compat shim).
2. `apps/mockbets/views.py` (read `?scope=`, default `actual`).
3. `templates/mockbets/moneyline_evaluation.html` (scope dropdown + scope summary box).

Plus the test file. That's it. The recommendation pipeline is untouched.

---

## 6. Operator next steps

1. After this commit ships (Railway auto-deploy), visit `/mockbets/moneyline-evaluation/`. Confirm:
   - The page now defaults to **Actual Bets** scope.
   - The scope summary box surfaces "X placed moneyline bets in window · Y included · Z excluded".
   - For the date that triggered the discrepancy: total placed should now match `My Bets` count for that date.
2. Verify the alignment manually: open `My Bets` filtered to MLB + the same date; the count there should match the `Actual` scope `included` count on the evaluation page.
3. To do model evaluation specifically (the old default behavior), select `Recommended System Bets` from the scope dropdown. The exclusion count + reasons make it explicit which bets you're filtering out.

---

## 7. Phase 2A status

**Phase 2A Task 4 (Elo activation decision) is paused until you confirm the evaluation-truth issue is resolved.** Once you've verified the My Bets / Actual-eval alignment, we can resume Phase 2A Task 4 with confidence that the shadow-review numbers we're going to analyze are the actual bet population, not a silently filtered subset.

The Phase 2A Task 3 analysis doc (`docs/phase_2a_task3_shadow_analysis_2026_05_14.md`) is unchanged because the shadow review reads `BettingRecommendation` rows, not `MockBet` rows — a different data source from the one this audit fixed. But the principle ("never silently exclude actual placed bets / actual recommendations") is now a permanent architecture commitment that should be applied to any future analytics surface.

---

*First written: 2026-05-14 — in response to operator-observed My Bets/Evaluation discrepancy.*
