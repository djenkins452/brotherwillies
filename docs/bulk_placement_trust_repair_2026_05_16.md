# Bulk Placement Trust Repair — 2026-05-16

**Production issue:** MLB hub displayed "Bet All Moneyline Plays (5)"; only 3 bets placed; 2 retained "Bet This" with **no error, no warning, no explanation**. The operator could not tell what happened.

**Verdict:** root cause identified, surgical fix landed, 11 new regression tests lock the contract. Silent partial placement is no longer architecturally possible.

---

## 1. Root cause

Two compounding bugs:

### 1A. Count vs eligibility divergence (primary mechanism)

The button count came from `apps/mlb/views.py:148-150`:

```python
verified_bulk_count = (
    len(decision_sections['elite']) + len(decision_sections['recommended'])
)
```

…and `partition_games_by_decision` (`apps/mlb/services/prioritization.py:780-818`) buckets tiles by `rec.status` and `rec.tier` only:

```python
if rec.tier == 'elite' and not is_secondary:
    elite.append(s)
elif rec.status == 'recommended':
    recommended.append(s)
```

The placement filter `_eligible_games_for_user` (`apps/mockbets/services/bulk_actions.py:112-158` pre-fix) **also** required `rec.lane == 'core'`:

```python
if getattr(rec, 'lane', '') != 'core':
    continue
```

**The mismatch:** a game with `status='recommended'` + `lane='qualified'` (because the Two-Lane System fired a risk flag — `short_fav_thin`, `market_conflict`, `sanity_mismatch`, `thin_edge`, or `insight_conflict`) was counted by the button (it's in `elite` or `recommended` bucket) but excluded by placement (lane gate). The Pirates (-124) and Diamondbacks (-184) almost certainly carried `short_fav_thin` or `market_conflict`.

The button said "(5)" because 5 tiles met the count predicate. Placement processed 3 because 2 failed the additional lane gate. Neither the button nor the response acknowledged the divergence — the user saw a number, expected that number of bets, got fewer, and had no way to know why.

### 1B. Loop-wide atomic block (architectural fragility)

The placement loop was wrapped in a single `transaction.atomic()` block (`apps/mockbets/services/bulk_actions.py:252` pre-fix):

```python
with transaction.atomic():
    for game, rec in eligible:
        # ...
        bet = MockBet.objects.create(...)
        placed += 1
```

If any single `MockBet.objects.create` raised (e.g., `IntegrityError` from a race condition, `DataError` from bad odds, `OperationalError` from a DB hiccup), the **entire batch** would roll back. The exception would propagate to a 500. **No bets placed at all, no partial-success response.**

The user's observation of "3 placed" rules out atomic rollback being the active failure mode in this specific incident, but the architecture was one transient DB error away from a much worse failure surface.

### 1C. No surface for non-existing/duplicate categories

The legacy response carried `placed`, `skipped_existing`, `skipped_started`, `skipped_no_odds`. **No category for "lane drift" or "recommendation became ineligible since page render"** — even if a future bug fixed the count/placement mismatch perfectly, drift between page render and click would still produce silent skips.

---

## 2. Architectural fix — Single Source of Truth + per-game isolation

### 2A. Single eligibility predicate

New function `apps/mockbets/services/bulk_actions.py::is_bulk_moneyline_eligible(rec, *, source_filter, tier_filter)`. Returns True iff the recommendation passes every gate (status, lane, value tier, probability, odds, blocked, source, tier). **Both** the MLB hub view (button count) **and** `_eligible_games_for_user` (placement filter) call this predicate. They cannot diverge — that's the entire contract.

```python
verified_bulk_game_ids = [
    str(tile.game.id) for tile in all_tiles
    if is_bulk_moneyline_eligible(
        getattr(tile, 'recommendation', None), source_filter='verified',
    )
]
verified_bulk_count = len(verified_bulk_game_ids)
```

### 2B. Locked candidate set across the wire

The hub view also emits `verified_bulk_game_ids_json` — a JSON list of the exact UUIDs counted. The template stamps it onto the button as `data-bulk-game-ids`. The JS POSTs them as a JSON body to `/mockbets/bulk/place-recommended/`. The endpoint reads them from the body and passes them to `place_bulk_recommended_bets(..., game_ids=[...])`.

`place_bulk_recommended_bets` with `game_ids` set processes **exactly** those game IDs — no recomputation of the candidate set. If a game in that list is no longer eligible at placement time (drift), it surfaces explicitly as `skipped_recommendation_drift` with a human-readable reason.

The legacy path (`game_ids=None`) is retained for back-compat with internal callers and tests; it recomputes the candidate set the old way.

### 2C. Per-game isolation

The atomic block now wraps **a single bet**, not the loop:

```python
def _place_one_bet(user, game, rec, stake):
    with transaction.atomic():
        bet = MockBet.objects.create(...)
        # ... snapshot persistence (non-fatal) ...
    return bet

for gid, game in candidates:
    try:
        bet = _place_one_bet(user, game, rec, stake)
        placed_items.append({...})
    except Exception as exc:
        logger.exception(...)
        failed_items.append({'reason': str(exc), ...})
```

One bad bet does **not** roll back the others. Loop reaches every candidate.

### 2D. Structured per-game outcome response

Every candidate game lands in exactly one of three lists: `placed_items`, `skipped_items`, `failed_items`. Each item carries:

- `game_id` (string)
- `label` (`"Away Team @ Home Team"`)
- `outcome` (one of: `placed` / `skipped_duplicate` / `skipped_recommendation_drift` / `skipped_game_started` / `skipped_missing_odds` / `failed`)
- `reason` (human-readable string)
- `bet_id` (for placed items only)

Plus top-level counts (`requested`, `placed`, `skipped`, `failed`) and legacy counters for back-compat (`skipped_existing`, `skipped_started`, `skipped_no_odds`).

**Alignment contract:** `placed + skipped + failed == requested`. Locked by `test_every_game_lands_in_exactly_one_outcome_bucket`.

### 2E. Explicit JS summary

`_renderBulkSummary` in `templates/mlb/hub.html` builds an operator-readable multi-line summary from the structured items:

```
Moneyline: requested 5 · placed 3 · skipped 2 · failed 0
✓ Detroit Tigers @ Los Angeles Angels — Placed
✓ Toronto Blue Jays @ Boston Red Sox — Placed
✓ Houston Astros @ Texas Rangers — Placed
⚠ Pittsburgh Pirates @ Chicago Cubs — Skipped — recommendation changed since page load
⚠ Arizona Diamondbacks @ San Francisco Giants — Skipped — recommendation changed since page load
```

Dwell time before reload is extended from 1.2s → 4s when skipped or failed items exist so the operator can read the breakdown.

---

## 3. Why the bug happened

- **Two filtering layers, no canonical predicate.** The Two-Lane System (2026-04-28) added a new gate (`lane == 'core'`) but it was implemented inline in `_eligible_games_for_user` rather than promoted to a predicate that both surfaces consume. The count source pre-dated the Two-Lane work; it was not updated when the lane gate landed.
- **No alignment lock in tests.** The existing test suite asserted "bulk place creates one bet per recommended game" but never asserted "the count the operator sees equals the count placed." There was no test that caught the divergence.
- **No surface for new exclusion categories.** When the Two-Lane System added the `qualified` lane (visible but bulk-ineligible), the response shape was not extended to surface those exclusions. The legacy `skipped_existing` / `skipped_no_odds` vocabulary was the only language the response spoke; lane drift had no name.
- **Loop-wide atomic block.** Inherited from an earlier architecture where bulk was new and atomicity was the safest default. The cost (one bad bet kills the batch) became unacceptable once bulk became a frequent user action.

---

## 4. Files touched

| File | Change |
|---|---|
| `apps/mockbets/services/bulk_actions.py` | New `is_bulk_moneyline_eligible` predicate; refactored `_eligible_games_for_user` to use it; per-game isolation in `place_bulk_recommended_bets`; new outcome constants; `game_ids` parameter for locked-set path; structured `placed_items` / `skipped_items` / `failed_items` arrays. |
| `apps/mockbets/views.py::bulk_place_recommended` | Reads `game_ids` from JSON body when provided; passes to service. Falls back to legacy path when body absent. |
| `apps/mlb/views.py::mlb_hub` | Uses `is_bulk_moneyline_eligible` to compute the button count AND emits `verified_bulk_game_ids_json` for the JS. |
| `templates/mlb/hub.html` | Button carries `data-bulk-game-ids`; JS POSTs them as JSON body; renders structured per-game summary. |
| `apps/mockbets/tests.py` | New `BulkPlacementTrustRepairTests` class — 11 regression tests covering all 10 spec scenarios + JSON-body endpoint contract. |
| `docs/bulk_placement_trust_repair_2026_05_16.md` | This document. |

---

## 5. Tests added (11)

| # | Test | What it locks |
|---|---|---|
| 1 | `test_count_locked_set_all_placed` | Happy path: 5 requested → 5 placed |
| 2 | `test_partial_placement_succeeds_for_eligible_subset` | Drifted-out games skipped with reason; eligible subset still placed |
| 3 | `test_one_failed_game_does_not_terminate_loop` | Simulated `MockBet.objects.create` failure on game 3 — loop reaches 4 and 5; 4 placed, 1 failed |
| 4 | `test_duplicate_bet_surfaces_explicit_reason` | Pre-existing pending bet → `skipped_duplicate` with human-readable reason |
| 5 | `test_recommendation_drift_surfaces_explicit_reason` | Ineligible-at-placement-time game → `skipped_recommendation_drift` |
| 6 | `test_started_game_skip_surfaces_explicit_reason` | first_pitch in past → `skipped_game_started` |
| 7 | `test_missing_odds_skip_surfaces_explicit_reason` | No odds snapshot → `skipped_missing_odds` |
| 8 | `test_is_bulk_moneyline_eligible_consistent_across_callers` | Predicate behavior locked: status, lane, value, probability, longshot, blocked, source filters |
| 9 | `test_every_game_lands_in_exactly_one_outcome_bucket` | Alignment contract: `placed + skipped + failed == requested` for heterogeneous inputs |
| 10 | `test_endpoint_processes_locked_game_ids_via_json_body` | View reads `game_ids` from JSON body; processes EXACTLY those; ignores unlisted games |
| 11 | `test_endpoint_falls_back_to_legacy_path_without_body` | Old clients (no body) still work via legacy recompute path |

---

## 6. What this fix does NOT change

- ❌ No threshold tunes.
- ❌ No recommendation logic changes.
- ❌ No Elo changes.
- ❌ No calibration changes.
- ❌ No betting-engine changes.

The trust repair is strictly architectural: count + placement now share a predicate; placement now has per-game isolation; outcomes are now explicit. The model itself is untouched.

---

## 7. Proof: count == execution set

The contract is now enforced at three layers:

1. **Single predicate** (`is_bulk_moneyline_eligible`) — called by both the hub view and the placement service. They cannot diverge.
2. **Locked candidate set** — the hub emits the exact game IDs; the JS POSTs them; the endpoint processes exactly those. No server-side recomputation of the candidate set when `game_ids` is provided.
3. **Alignment contract** — `placed + skipped + failed == requested`, locked by `test_every_game_lands_in_exactly_one_outcome_bucket`.

If the button says (5), one of two things now MUST happen:
- (A) 5 bets placed, OR
- (B) The operator sees a structured summary showing exactly why each of the missing N bets was skipped or failed.

Silent partial execution is no longer architecturally possible.

---

## 8. Test totals

- **767 tests passing** across phase-relevant modules (mockbets + mlb + cfb + cbb + college_baseball + analytics + core + datahub). Zero regressions.
- `python manage.py check` clean.

---

## 9. Architecture law compliance

| Law | Compliance |
|---|---|
| **Law 1 — Signals are nudges** | N/A — bulk eligibility predicate is a pass/fail gate, not a signal weight. |
| **Law 2 — No signal without eval slice** | N/A — no new signal added. |
| **Law 3 — Analytics surfaces transparent about scope** | ✅ — every excluded game now carries a named outcome and human-readable reason. Silent exclusion is structurally impossible. |
| **Law 4 — Do not overfit** | ✅ — no constants changed; no thresholds tuned; no model behavior modified. |

---

*Trust repair shipped 2026-05-16. The framework's "no silent failures" obligation extends to bulk placement: every game an operator clicked on must come back with a documented outcome.*
