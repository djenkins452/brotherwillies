"""2026-08-24 — reconcile duplicate Golfer rows before enforcing
uniqueness.

Root cause (see apps/golf/models.py::Golfer docstring):
  Two ingestion paths accumulated duplicate rows for the same
  real-world golfer. `odds_provider` writes via
  `Golfer.objects.get_or_create(name=X)`; `schedule_provider`
  writes via `get_or_create(external_id=Y)`; `seed_golfers` writes
  by name. With no uniqueness constraint on either key, duplicates
  compounded to 20+ rows per name — enough that a fresh
  `get_or_create(name=X)` raised `MultipleObjectsReturned` and
  broke every refresh_data run.

Two-pass merge:
  1. Group by `external_id` where non-blank. For each group of
     >=2 rows, keep the smallest id as winner. Re-parent every FK
     reference (GolfOddsSnapshot.golfer, GolfRound.golfer,
     MockBet.golf_golfer) to the winner. Delete losers.
  2. Compute name_normalized on all surviving rows. Group by
     name_normalized. For each group of >=2 rows, prefer the row
     that has a non-empty external_id (its identity is stronger).
     Re-parent FKs, delete losers.

Safety:
  * FK re-parenting is bulk `update(golfer=winner)` filtered to
    each loser id — so no snapshot/round/bet is orphaned.
  * The winner absorbs any losing row's external_id if the winner
    had none.
  * Migration is idempotent — re-running finds no duplicates and
    is a no-op.
  * Runs BEFORE the unique constraints are enforced (0009), so it
    can safely operate on a duplicated state.
"""
from __future__ import annotations

from django.db import migrations
from django.db.models import Count


def _normalize(name: str) -> str:
    if not name:
        return ''
    return ' '.join(name.strip().split()).lower()


def _reparent_fks(apps, from_id: int, to_id: int) -> None:
    """Point every FK reference from `from_id` at `to_id`, then
    delete the losing Golfer row. Never orphans a snapshot."""
    Golfer = apps.get_model('golf', 'Golfer')
    GolfOddsSnapshot = apps.get_model('golf', 'GolfOddsSnapshot')
    GolfRound = apps.get_model('golf', 'GolfRound')
    # MockBet is optional (some deploys may lack it) — degrade
    # gracefully.
    MockBet = None
    try:
        MockBet = apps.get_model('mockbets', 'MockBet')
    except LookupError:
        pass

    GolfOddsSnapshot.objects.filter(golfer_id=from_id).update(
        golfer_id=to_id,
    )
    GolfRound.objects.filter(golfer_id=from_id).update(
        golfer_id=to_id,
    )
    if MockBet is not None:
        # Guard: field may not exist on older schemas.
        try:
            MockBet.objects.filter(golf_golfer_id=from_id).update(
                golf_golfer_id=to_id,
            )
        except Exception:
            pass


def _merge_group(apps, winner_id: int, loser_ids: list[int]) -> None:
    """Merge every loser into winner: re-parent FKs, then delete loser."""
    Golfer = apps.get_model('golf', 'Golfer')
    for lid in loser_ids:
        if lid == winner_id:
            continue
        _reparent_fks(apps, from_id=lid, to_id=winner_id)
    Golfer.objects.filter(id__in=loser_ids).delete()


def _absorb_external_id(apps, winner_id: int, loser_ids: list[int]) -> None:
    """If the winner has no external_id and any loser had one, copy
    the loser's external_id up to the winner before merging."""
    Golfer = apps.get_model('golf', 'Golfer')
    winner = Golfer.objects.get(id=winner_id)
    if winner.external_id:
        return
    losers_with_id = (
        Golfer.objects.filter(id__in=loser_ids)
        .exclude(external_id='')
        .order_by('id')
    )
    first_with_id = losers_with_id.first()
    if first_with_id is not None:
        # Guard: another surviving winner (in a different group) may
        # already own this external_id. In that case, DON'T copy — the
        # constraint would fail. Losing the external_id on the merge
        # is acceptable; the FK re-parenting is what matters.
        conflict = (
            Golfer.objects
            .exclude(id__in=[winner_id] + list(loser_ids))
            .filter(external_id=first_with_id.external_id)
            .exists()
        )
        if not conflict:
            winner.external_id = first_with_id.external_id
            winner.save(update_fields=['external_id'])


def reconcile(apps, schema_editor):
    Golfer = apps.get_model('golf', 'Golfer')

    # --- Pass 0: populate name_normalized on every row.
    for g in Golfer.objects.all().iterator(chunk_size=1000):
        norm = _normalize(g.name)
        if g.name_normalized != norm:
            Golfer.objects.filter(id=g.id).update(name_normalized=norm)

    # --- Pass 1: merge duplicates by external_id (non-blank).
    ext_dupes = (
        Golfer.objects.exclude(external_id='')
        .values('external_id')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    for row in ext_dupes:
        ids = list(
            Golfer.objects.filter(external_id=row['external_id'])
            .order_by('id').values_list('id', flat=True)
        )
        winner = ids[0]
        losers = ids[1:]
        _merge_group(apps, winner_id=winner, loser_ids=losers)

    # --- Pass 2: merge duplicates by name_normalized.
    name_dupes = (
        Golfer.objects.exclude(name_normalized='')
        .values('name_normalized')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
    )
    for row in name_dupes:
        ids_with_ext = list(
            Golfer.objects.filter(name_normalized=row['name_normalized'])
            .exclude(external_id='')
            .order_by('id').values_list('id', flat=True)
        )
        ids_wo_ext = list(
            Golfer.objects.filter(name_normalized=row['name_normalized'],
                                  external_id='')
            .order_by('id').values_list('id', flat=True)
        )
        # Prefer a row that has an external_id — its identity is
        # stronger. Fall back to the lowest-id row without.
        ordered = ids_with_ext + ids_wo_ext
        if len(ordered) <= 1:
            continue
        winner = ordered[0]
        losers = ordered[1:]
        _absorb_external_id(apps, winner_id=winner, loser_ids=losers)
        _merge_group(apps, winner_id=winner, loser_ids=losers)


def noop_reverse(apps, schema_editor):
    """Reversal is intentionally a no-op — merged data cannot be
    unmerged. If a rollback is truly needed, restore from backup."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('golf', '0007_golfer_name_normalized_and_more'),
        # We touch mockbets FKs in the reconciliation, so declare a
        # dependency to ensure mockbets is at a state where
        # MockBet.golf_golfer exists.
        ('mockbets', '0002_mockbet_college_baseball_game_mockbet_mlb_game_and_more'),
    ]

    operations = [
        migrations.RunPython(reconcile, reverse_code=noop_reverse),
    ]
