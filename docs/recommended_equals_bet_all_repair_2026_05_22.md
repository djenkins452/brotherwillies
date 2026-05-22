# Recommended-Card / Bet-All Count Alignment Repair — 2026-05-22

**Production issue:** MLB hub displayed `Recommended (4)` cards and `Bet All Moneyline Plays (2)` button — visible 2-game divergence with no explanation. Master-prompt RULE 2 violation: *"If a game appears in Recommended, it MUST be bettable by Bet All."*

**Verdict:** root cause identified. The 2026-05-16 trust repair aligned the **button count** with the **placement set**; this 2026-05-22 repair aligns the **visible Recommended bucket** with both. Single source of truth now spans all three surfaces by construction. 5 new regression tests lock the invariant.

---

## 1. Root cause

The bucket-assignment block in `apps/mlb/views.py::mlb_hub` was using a **different predicate** for the visible Recommended bucket than for the Bet All button count:

| Surface | Predicate (pre-fix) | What it checked |
|---|---|---|
| Visible **Recommended** bucket | `decision_sections['elite'] + decision_sections['recommended']` (via `partition_games_by_decision`) | `status='recommended'`, tier ≠ blocked, source ≠ secondary. **No lane check.** |
| **Bet All** button count | `is_bulk_moneyline_eligible(rec, source_filter='verified')` | status + tier + **lane=='core'** + probability + longshot + blocked + source |

A game with `status='recommended'` + `lane='qualified'` (because a risk flag like `short_fav_thin` or `market_conflict` fired) appeared in **Recommended** (status passes) but was **excluded from Bet All** (lane fails).

**Concretely** for the operator's slate: 4 cards had `status='recommended'`, but 2 of them carried risk flags pushing them to `lane='qualified'`. Hub view rendered 4 cards (status only); button counted 2 (status + lane). The operator saw the divergence with no way to explain it.

This is the same root-cause class as the 2026-05-16 bug (single predicate vs split inline checks), at a different surface layer.

---

## 2. Why it happened again

The 2026-05-16 repair correctly extracted `is_bulk_moneyline_eligible` as a predicate and aligned:

```
button count ←→ placement set
```

…via that predicate. But the **visible Recommended bucket** continued to use `partition_games_by_decision` directly, which checks status/tier but not lane. That bucket fed the operator-visible UI; the predicate-aligned button count rendered alongside it. The two had no enforced relationship.

**The architectural lesson:** when a system has N surfaces that need to agree on "what is recommended," extracting a predicate is necessary but not sufficient. **Every surface must consume the predicate.** If one surface uses a different predicate path, the invariant is broken there even if the other surfaces are aligned.

---

## 3. Fix — Recommended bucket consumes `is_bulk_moneyline_eligible`

In `apps/mlb/views.py::mlb_hub` (replaced the bucket assignment block):

```python
def _is_visible_recommended(tile):
    """The canonical Recommended-bucket predicate. Identical to the
    bulk-eligibility predicate so the visible count and the Bet All
    count cannot diverge by construction."""
    return is_bulk_moneyline_eligible(
        getattr(tile, 'recommendation', None), source_filter='verified',
    )

_recommended_candidate_pool = (
    decision_sections['elite'] + decision_sections['recommended']
)
recommended_tiles = _take([
    tile for tile in _recommended_candidate_pool
    if _is_visible_recommended(tile)
])

# Carry-overs: status='recommended' games that failed bulk-eligibility
# for any reason (lane, probability gate, longshot, etc.) flow into
# Potential rather than vanishing.
_recommended_carry_overs = [
    tile for tile in _recommended_candidate_pool
    if not _is_visible_recommended(tile)
]
potential_tiles = _take(
    lane_sections['qualified']
    + decision_sections.get('value', [])
    + _recommended_carry_overs
)
```

And the button count is now sourced **directly from `recommended_tiles`** — eliminating any possibility of computing it from a different filter path:

```python
verified_bulk_game_ids = [str(tile.game.id) for tile in recommended_tiles]
verified_bulk_count = len(verified_bulk_game_ids)
```

**By construction:** `len(recommended_tiles) == verified_bulk_count`. The two cannot diverge.

---

## 4. Defensive divergence detection (RULE 3)

Even though the construction makes a mismatch impossible, the view emits a defensive warning log if the count derived from `recommended_tiles` ever differs from the predicate applied across `all_tiles`. This catches any future refactor that re-introduces a separate filter path on the first slate it processes:

```python
_alt_count = sum(
    1 for tile in all_tiles
    if _is_visible_recommended(tile)
)
if _alt_count != verified_bulk_count:
    _div_log.warning(
        'mlb_hub bulk count divergence: recommended_tiles=%d '
        'predicate_over_all_tiles=%d. visible_ids=%s diverged_ids=%s',
        ...
    )
```

The divergence will never fire under the current implementation, but it would catch a future regression at the first impacted page render — before the operator notices a count mismatch.

---

## 5. UI contract (RULE 2) — proof

**Before the fix:**

```
Recommended bucket:  [A, B, C, D]   ← rendered from decision_sections (no lane check)
Bet All button:      (2)            ← computed from is_bulk_moneyline_eligible (with lane)
Potential bucket:    [..., other]   ← C and D NOT here, lost in Recommended
```

**After the fix:**

```
Recommended bucket:  [A, B]         ← filtered by is_bulk_moneyline_eligible
Bet All button:      (2)            ← derived directly from recommended_tiles
Potential bucket:    [C, D, ...]    ← carry-overs caught by potential_tiles
```

The visible count and the Bet All count are now sourced from the **same list** (`recommended_tiles`). There is no separate computation path.

---

## 6. Tests added (5)

`apps.mlb.tests.MLBHubRecommendedEqualsBetAllTests`:

| # | Test | Spec scenario |
|---|---|---|
| A | `test_scenario_a_four_recommended_cards_match_button_count` | 4 distinct eligible games → 4 cards visible → button (4) → bulk places 4 |
| B | `test_scenario_b_risk_flagged_games_land_in_potential_not_recommended` | Risk-flagged game (lane='qualified') moves to Potential, Recommended count drops, button count drops in lock-step |
| C | `test_scenario_c_qualified_lane_not_in_recommended_is_in_potential` | Direct lane semantics: qualified-lane game appears in Potential, NOT in Recommended |
| D | `test_scenario_d_visible_recommended_count_equals_bulk_count_always` | **THE INVARIANT** — for any mixed slate, `len(recommended_tiles) == verified_bulk_count` AND the emitted `verified_bulk_game_ids_json` matches the visible game IDs AND every visible Recommended tile passes `is_bulk_moneyline_eligible` |
| + | `test_recommended_carry_overs_land_in_potential_not_lost` | A `status='recommended'` game that fails bulk-eligibility for a non-lane reason (longshot odds) still appears in some bucket — never vanishes silently |

**Test totals:** 772 across phase-relevant modules. Zero regressions. `python manage.py check` clean.

---

## 7. Files touched

| File | Change |
|---|---|
| `apps/mlb/views.py::mlb_hub` | Bucket-assignment block now filters Recommended via `_is_visible_recommended` (== `is_bulk_moneyline_eligible`). Carry-overs that fail the predicate but had `status='recommended'` flow into Potential. Button count derived directly from `recommended_tiles`. Defensive divergence-detection warning added. |
| `apps/mlb/tests.py` | Imports updated (`User`, `Client`, `Decimal`, `uuid`); new `MLBHubRecommendedEqualsBetAllTests` class with 5 tests. |
| `docs/recommended_equals_bet_all_repair_2026_05_22.md` | This document. |

---

## 8. What this fix does NOT change

- ❌ No threshold tunes.
- ❌ No recommendation logic changes.
- ❌ No Elo changes.
- ❌ No calibration changes.
- ❌ No model behavior changes.

The fix is strictly UI/bucket-assignment trust repair. Recommendations are computed identically; only the visible-bucket placement of certain recommendations changes (specifically, status='recommended' + lane='qualified' games move from Recommended to Potential, which matches the design intent of the Two-Lane System).

---

## 9. Single Source of Truth — full chain after this fix

```
Recommendation engine produces rec for each game
                    │
                    ▼
   is_bulk_moneyline_eligible(rec, source_filter='verified')
                    │
        ┌───────────┴───────────┐
        │                       │
   True (eligible)         False (not eligible)
        │                       │
        ▼                       ▼
recommended_tiles            potential_tiles
        │                  (or other bucket)
        │
        ▼
verified_bulk_game_ids ── (JSON list) ──► JS button data-attribute
        │                                       │
        ▼                                       ▼
verified_bulk_count                     POST body: {game_ids: [...]}
        │                                       │
        ▼                                       ▼
Button label "(N)"            place_bulk_recommended_bets(game_ids=[...])
                                                │
                                                ▼
                                       Per-game placement:
                                       placed/skipped/failed
```

Every layer reads from the same predicate. **No surface can diverge from another by construction.**

The 2026-05-16 fix locked the bottom half (button → placement). The 2026-05-22 fix locks the top half (visible bucket → button). Together they make the entire chain immutable.

---

## 10. Architecture law compliance

- **Law 3** (analytics surfaces transparent about scope): satisfied. The Recommended bucket name now means exactly what the UI implies: "bettable by Bet All." Surfaces no longer have hidden eligibility rules.
- **Law 4** (do not overfit): satisfied. No constants changed, no thresholds tuned, no model behavior modified. Pure architectural repair.

---

*Trust contract restored 2026-05-22. The Recommended bucket is what the UI says it is.*
