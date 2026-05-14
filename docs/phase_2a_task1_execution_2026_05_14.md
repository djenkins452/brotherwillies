# Phase 2A Task 1 — Production-Safe Elo Backfill Execution

**Status:** structural work complete; awaiting next Railway deploy to
populate MLB Elo state in production. The live recommendation engine
is unaffected by this work (`USE_DYNAMIC_RATINGS=False` until the
Phase 2A Task 4 cutover decision).

---

## What ships in this commit

- `apps/datahub/management/commands/ensure_elo_backfilled.py` —
  idempotent Railway-safe wrapper around `rebuild_elo_ratings --sport mlb`.
- `apps/datahub/management/commands/ensure_seed.py` — calls
  `ensure_elo_backfilled` on every deploy (after the existing
  `backfill_loss_reasons` step, before the odds diagnostic).
- `apps/datahub/test_ensure_elo_backfilled.py` — 6 tests locking
  idempotence, force flag, detection edge cases, and failure isolation.
- `docs/architecture_laws.md` — codifies "signals are nudges, not
  drivers" and "no signal without its evaluation slice" as permanent
  architecture laws.

## Detection semantics

A sport is considered "already backfilled" when **both** hold:

- ≥ 20 MLB teams have a non-null `elo_rating` (MLB has 30 teams; 20
  allows for partial state at season start while rejecting the
  "single test team" false positive).
- ≥ 1 `TeamEloHistory` row exists for `sport='mlb'`.

If either condition fails, `rebuild_elo_ratings --sport mlb` runs.
The `--force` flag bypasses the guard for operator-initiated rebuilds
(after a K-factor change, HFA tune, or data correction).

## Expected runtime

| Environment | Final-game count | Expected duration |
|---|---|---|
| Local SQLite | ~30 games (test fixtures) | < 1 s |
| Local SQLite | ~2,000 games (season replay) | 1–3 s |
| Railway PostgreSQL | ~2,000 games | 5–15 s |

The inner `rebuild_elo_ratings` wraps the work in `transaction.atomic`
— a partial failure rolls back; nothing partial lands. Memory usage is
O(1) because the rebuild iterates with `.iterator()` and writes one
game at a time.

After the first deploy completes the backfill, subsequent deploys
detect the populated state, print a short skip log line, and add
essentially zero overhead to the deploy duration.

## Failure handling

`ensure_elo_backfilled` wraps the inner call in try/except:

1. On exception inside `rebuild_elo_ratings`:
   - The exception message is printed to the deploy log as a `WARNING`.
   - The command exits 0 — the Railway deploy continues.
   - The next deploy retries automatically (idempotent detection still
     sees an under-populated state).
2. On exception above the try/except (import error, etc.):
   - `ensure_seed`'s outer try/except catches it (already in place for
     all other backfill commands).
   - Same deploy-continues-cleanly behavior.

**Why this is the right failure mode.** The live recommendation engine
runs on `Team.rating` (static) until the Phase 2A Task 4 cutover. A
failed Elo backfill cannot change anything users see. The cost of a
backfill failure is "shadow-mode comparison data is unavailable for
this slate" — a diagnostic loss, not a product loss. Hard-failing the
deploy in response would be a self-inflicted product outage with no
upside.

## Rollback procedure

**To force a re-backfill on the next deploy** (after a data correction
or a K-factor / HFA change):

```python
# Run via shell or a one-off command. Railway lacks shell, so this
# would be deployed as a one-off management command if needed in prod;
# locally, just open a Django shell.
from apps.core.services.elo_service import reset_sport
reset_sport('mlb')
```

This sets every MLB team's `elo_rating = None` and deletes the sport's
`TeamEloHistory` rows. The next deploy detects the empty state and
triggers a fresh rebuild.

**To manually trigger a backfill without redeploying** (developer
workflow):

```bash
python manage.py ensure_elo_backfilled            # idempotent
python manage.py ensure_elo_backfilled --force    # rebuild anyway
python manage.py rebuild_elo_ratings --sport mlb  # the underlying call
```

**To verify state at any time:**

```bash
python manage.py shell -c "
from apps.mlb.models import Team
from apps.analytics.models import TeamEloHistory
print(f'teams_with_elo={Team.objects.filter(elo_rating__isnull=False).count()}')
print(f'history_rows={TeamEloHistory.objects.filter(sport=\"mlb\").count()}')
top = Team.objects.filter(elo_rating__isnull=False).order_by('-elo_rating')[:5]
print('top:', [(t.name, round(t.elo_rating, 1)) for t in top])
"
```

## Production-safety guarantees

Each is locked by code structure or by tests:

| Guarantee | Mechanism |
|---|---|
| Idempotent | Detection guard + `process_game`'s history-row guard inside `rebuild_elo_ratings`. Locked by `RebuildIdempotenceTests` and `IdempotentDetectionTests`. |
| Atomic | `_rebuild_sport` wraps the work in `transaction.atomic`. |
| Deterministic | Pure function of input games + module constants (`K_FACTORS`, `HFA_ELO`, `INITIAL_RATING`). No randomness, no time, no network. |
| Reversible | `reset_sport('mlb')` returns state to pre-backfill. |
| Live behavior unaffected | `team_rating_for_model` reads `team.rating` while `USE_DYNAMIC_RATINGS=False`. Verified by `FeatureFlagFallbackTests`. |
| Deploy-safe | Inner failure caught + logged; deploy continues. Verified by `FailureIsolationTests`. |

## What is NOT in this commit

Strictly excluded per the user's Phase 2A scope direction:

- `USE_DYNAMIC_RATINGS` remains `False`. The flag is not flipped.
- No new predictive signals (pitcher form, team form, bullpen).
- No edge-realism compression.
- No sigmoid / clamp / blend retunes.
- No new evaluation breakdowns beyond what Phase 1A already shipped.

Variable isolation: this commit's blast radius is exactly the
deploy-time backfill behavior. Every other axis is held still so that
post-cutover empirics (Phase 2A Tasks 3–4) cleanly attribute any
observed shift to the rating system swap.

## Phase 2A Tasks 2 / 3 / 4 (NOT this commit)

The remaining tasks are sequential and data-gated:

- **Task 2 — Shadow data collection.** Passive. After the next Railway
  deploy runs `ensure_elo_backfilled`, every new MLB `BettingRecommendation`
  carries meaningful `shadow_alt_data` (Phase 1B Task 5 plumbing).
  Wait at least one full MLB slate. Multiple slates are better.
- **Task 3 — Shadow review analysis.** Once `/analytics/shadow-review/`
  has a meaningful `sample` count (target: ≥ 30 rows), I produce the
  six-question analysis: edge shrinkage, recommendation distribution,
  believability, short-favorite aggressiveness, model/market alignment,
  CLV direction. Most importantly: did Elo reduce overconfidence
  naturally?
- **Task 4 — Activation decision.** GO / NO-GO recommendation based on
  Task 3 evidence, with rollback triggers and post-activation monitoring
  plan. The decision is a one-env-var change (`USE_DYNAMIC_RATINGS=True`).

Each subsequent task waits for explicit "go" before proceeding. No
intermediate behavioral changes are introduced between deploys.

---

*First written: 2026-05-14. Execution strategy for the Phase 2A Task 1
production-safe backfill hook.*
